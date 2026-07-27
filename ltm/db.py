"""SQLite access layer.

All database access goes through this module.  Two tables:

    user_history (run_date DATE, dn TEXT, data JSON, data_hash TEXT)
        One row per (date, entry) holding the full directory record as JSON.
        ``data_hash`` is an MD5 of the JSON string.  The pipeline itself
        never reads it — the diff compares parsed records — but it lets
        external analysis find identical rows without parsing JSON, e.g.
        measuring how much of the table deduplication would save.
        PRIMARY KEY (run_date, dn).

    changes (run_date DATE, dn TEXT, attribute TEXT, old_val TEXT, new_val TEXT)
        One row per attribute change detected on a run_date.  A unique index
        (changes_unique) prevents the duplicates a same-day re-run would
        otherwise create.

    run_summary (run_date DATE PRIMARY KEY, record_count INTEGER)
        How many records each snapshot held.  Redundant with user_history, and
        maintained anyway: deriving it costs a GROUP BY over every row ever
        stored, which is what the trend graph used to do on every run.

Performance notes
-----------------
The pragmas in ``connect_db`` follow the measured guidance for single-writer
SQLite workloads: WAL plus ``synchronous=NORMAL`` is roughly 13x faster than
the rollback-journal default on per-row commits and never corrupts the file;
``mmap_size`` is the single biggest read lever (~+50% on point reads) whereas
a large ``cache_size`` measurably does nothing once warm; ``temp_store=MEMORY``
buys ~25% on the large sorts the aggregate tables perform.

Query shapes matter more than pragmas here.  Three deliberate choices:

* ``get_snapshot_dns`` reads only the ``dn`` column, which the (run_date, dn)
  primary-key index covers completely — no table access at all.  The leaver
  window compares seven days of *key sets*, so loading seven days of JSON
  blobs to do it would be pure waste.
* ``get_attribute_counts`` extracts every attribute the report needs in one
  pass over a date, rather than one scan per attribute.
* Writes go through ``write_transaction``, which issues ``BEGIN IMMEDIATE``.
  A plain ``BEGIN`` starts as a read transaction and the upgrade to a write
  lock cannot wait on busy_timeout — it fails instantly with SQLITE_BUSY.

``connect_db`` and ``init_db`` take an optional db_path so tests and dry runs
can point at a throwaway database; every other function operates on the open
connection it is given and leaves transaction management to the caller.
"""

import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager

from .config import DB_BUSY_TIMEOUT_SECONDS, DB_FILE, UNKNOWN_RATIO_LIMIT
from .ldif_parser import clean_val

# Memory-mapped I/O window.  256 MiB is the measured knee — enough to hold the
# hot pages of a multi-GB database without reserving real memory (mmap pages
# are backed by the page cache and evicted under pressure).
MMAP_SIZE_BYTES = 256 * 1024 * 1024

# Per-connection page cache, as a negative number meaning KiB rather than
# pages.  This is a single-connection cron process, so 64 MiB is affordable.
CACHE_SIZE_KIB = -64_000

# Cap the WAL after a checkpoint so it cannot grow without bound between runs.
JOURNAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

# SQLite's own limit is 999 bound parameters on older builds; stay well under.
_PARAM_CHUNK = 500

# Thresholds for the operational warnings in log_database_health.
_WAL_WARN_BYTES = 100 * 1024 * 1024
_WAL_CRIT_BYTES = 500 * 1024 * 1024
_FREELIST_VACUUM_RATIO = 0.20


def connect_db(db_path=None):
    """Open and configure a SQLite connection for the pipeline.

    ``isolation_level=None`` puts the connection in autocommit mode so that
    transactions are managed explicitly by ``write_transaction`` — without it,
    the driver would open its own implicit transaction and ``BEGIN IMMEDIATE``
    would fail with "cannot start a transaction within a transaction".

    Does not create tables; call ``init_db`` for that.

    Args:
        db_path: Optional path override.  Defaults to DB_FILE from config.

    Returns:
        An open sqlite3.Connection.
    """
    path = db_path if db_path is not None else DB_FILE
    conn = sqlite3.connect(path, timeout=DB_BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_SECONDS * 1000}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA cache_size = {CACHE_SIZE_KIB}")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute(f"PRAGMA mmap_size = {MMAP_SIZE_BYTES}")
    conn.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT_BYTES}")
    return conn


@contextmanager
def write_transaction(conn):
    """Run a block inside a ``BEGIN IMMEDIATE`` transaction.

    Takes the write lock up front instead of starting read-only and upgrading.
    The upgrade path is the one that cannot honour busy_timeout, so it fails
    immediately under contention rather than waiting — this project only ever
    has one writer, but the failure mode is nasty enough, and the fix cheap
    enough, that it is not worth relying on that.

    Commits on clean exit, rolls back on any exception.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(db_path=None):
    """Create tables and indexes if absent; migrate the unique index if needed.

    Safe to call on every run — all DDL uses IF NOT EXISTS.  The migration
    path handles databases created before ``changes_unique`` existed: it
    deduplicates first so the index creation cannot fail on legacy rows.

    The database file is set to mode 0600 afterwards.  That chmod is
    best-effort: it is logged at DEBUG and ignored on failure, because the
    permissions may already be right or the filesystem may not support it.

    Args:
        db_path: Optional path override.  Defaults to DB_FILE from config.

    Returns:
        An open sqlite3.Connection.
    """
    path = db_path if db_path is not None else DB_FILE
    conn = connect_db(db_path=path)
    c = conn.cursor()
    logging.debug("Initializing database: %s", path)
    c.execute("""CREATE TABLE IF NOT EXISTS user_history
                 (run_date DATE, dn TEXT, data JSON, data_hash TEXT,
                 PRIMARY KEY (run_date, dn))""")
    c.execute("""CREATE TABLE IF NOT EXISTS changes
                 (run_date DATE, dn TEXT, attribute TEXT, old_val TEXT, new_val TEXT)""")
    # One row per run: how many records that snapshot held.
    #
    # Derivable from user_history, but deriving it means GROUP BY over every
    # row ever stored — which the trend graph did on every single run. At 28M
    # rows that measured 53 seconds, and it grows without bound. Maintaining
    # the count at write time makes the graph O(days) instead of O(rows).
    c.execute("""CREATE TABLE IF NOT EXISTS run_summary
                 (run_date DATE PRIMARY KEY, record_count INTEGER NOT NULL)""")

    if not _has_changes_unique_index(c):
        # One-time migration: deduplicate first so CREATE UNIQUE INDEX cannot
        # fail on pre-existing duplicate rows.
        _dedupe_changes_table(c)
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS changes_unique
                     ON changes (run_date, dn, attribute, old_val, new_val)""")
        logging.info("Ensured unique index on changes table.")

    # (dn, run_date DESC) covers the "every date this DN appeared" query used
    # by flapper detection — both columns come from the index, so those scans
    # never touch the table.
    c.execute("""CREATE INDEX IF NOT EXISTS user_history_dn_run_date
                 ON user_history (dn, run_date DESC)""")
    # Serves per-DN history lookups. The window scans lead with run_date and
    # are already covered by changes_unique.
    c.execute("""CREATE INDEX IF NOT EXISTS changes_dn_attribute_run_date
                 ON changes (dn, attribute, run_date)""")

    # Reconcile the summary before anything reads it. On a database created
    # by this version it is already in sync and costs two counts; on an older
    # one it pays a single full scan here rather than surprising a later step.
    backfill_run_summary(conn)

    try:
        os.chmod(path, 0o600)
    except OSError:
        logging.debug("Unable to chmod database file", exc_info=True)
    return conn


def _has_changes_unique_index(cursor):
    """Return True if the changes_unique index already exists."""
    cursor.execute("PRAGMA index_list(changes)")
    return any(row[1] == "changes_unique" for row in cursor.fetchall())


def _dedupe_changes_table(cursor):
    """Remove duplicate rows from changes before creating the unique index.

    Keeps the lowest rowid for each unique
    (run_date, dn, attribute, old_val, new_val) tuple.

    This is the one large DELETE in the codebase that is not batched.  Batching
    exists to bound how long a write lock is held against *other* writers; this
    runs once, during startup migration, while the process lock guarantees
    there is no other writer.  Splitting it would trade a simpler correctness
    argument for a latency guarantee nobody is waiting on.
    """
    cursor.execute("SELECT COUNT(*) FROM changes")
    total = cursor.fetchone()[0]
    if total == 0:
        return

    cursor.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT 1 FROM changes
            GROUP BY run_date, dn, attribute, old_val, new_val
        )
        """
    )
    unique_rows = cursor.fetchone()[0]
    duplicates = total - unique_rows
    if duplicates <= 0:
        return

    logging.warning("Deduplicating changes table: removing %d rows", duplicates)
    cursor.execute(
        """
        DELETE FROM changes
        WHERE rowid NOT IN (
            SELECT MIN(rowid)
            FROM changes
            GROUP BY run_date, dn, attribute, old_val, new_val
        )
        """
    )


def get_snapshot_state(conn, run_date):
    """Return the full snapshot for a run_date as {dn: record_dict}.

    Parses every stored JSON blob for that date, so prefer
    ``get_snapshot_dns`` when only the set of DNs is needed.

    Returns {} when no data exists for that date — a first run, a weekend gap,
    or a future date.
    """
    c = conn.cursor()
    c.execute("SELECT dn, data FROM user_history WHERE run_date = ?", (run_date,))
    return {row[0]: json.loads(row[1]) for row in c.fetchall()}


def get_snapshot_dns(conn, run_date):
    """Return just the set of DNs present on a run_date.

    Reads only indexed columns, so the primary-key index answers this without
    touching the table or parsing a single JSON blob.  The leaver window runs
    this once per day in the confirmation period; doing it with
    ``get_snapshot_state`` instead would deserialise tens of thousands of
    records to compute a set difference.
    """
    c = conn.cursor()
    c.execute("SELECT dn FROM user_history WHERE run_date = ?", (run_date,))
    return {row[0] for row in c.fetchall()}


def get_records_for_dns(conn, run_date, dns):
    """Return {dn: record_dict} for specific DNs on a run_date.

    Fetches in chunks to stay under SQLite's bound-parameter limit.  Used to
    hydrate the handful of records that survive a set-difference, instead of
    loading a whole day.
    """
    dns = list(dns)
    if not dns:
        return {}
    out = {}
    c = conn.cursor()
    for start in range(0, len(dns), _PARAM_CHUNK):
        chunk = dns[start : start + _PARAM_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        c.execute(
            "SELECT dn, data FROM user_history "
            f"WHERE run_date = ? AND dn IN ({placeholders})",
            [run_date, *chunk],
        )
        for dn, data in c.fetchall():
            out[dn] = json.loads(data)
    return out


def get_run_dates_for_dns(conn, dns):
    """Return {dn: [run_date, ...]} for each DN, ascending.

    One chunked query rather than one query per DN.  Both columns live in the
    ``user_history_dn_run_date`` index, so this is an index-only scan.
    """
    dns = list(dns)
    if not dns:
        return {}
    out = {dn: [] for dn in dns}
    c = conn.cursor()
    for start in range(0, len(dns), _PARAM_CHUNK):
        chunk = dns[start : start + _PARAM_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        c.execute(
            f"SELECT dn, run_date FROM user_history WHERE dn IN ({placeholders}) "
            "ORDER BY dn, run_date",
            chunk,
        )
        for dn, run_date in c.fetchall():
            out[dn].append(run_date)
    return out


def get_latest_run_date(conn):
    """Return the most recent run_date in user_history, or None if empty."""
    c = conn.cursor()
    c.execute("SELECT MAX(run_date) FROM user_history")
    row = c.fetchone()
    return row[0] if row and row[0] else None


def get_window_run_dates(conn, end_date_str, window_days):
    """Return the distinct run_dates within a trailing calendar window.

    Covers the inclusive range [end_date - (window_days - 1), end_date].  Only
    dates that actually have rows are returned, so weekend and holiday gaps
    are skipped automatically.

    Returns:
        A list of 'YYYY-MM-DD' strings ascending; possibly empty.
    """
    c = conn.cursor()
    c.execute(
        "SELECT DISTINCT run_date FROM user_history "
        "WHERE run_date BETWEEN date(?, ?) AND ? ORDER BY run_date",
        (end_date_str, f"-{window_days - 1} days", end_date_str),
    )
    return [row[0] for row in c.fetchall()]


def get_previous_run_date(conn, start_date_str):
    """Return the most recent run_date strictly before start_date_str, or None.

    Establishes the "before" baseline for joiner and leaver windows.
    """
    c = conn.cursor()
    c.execute(
        "SELECT MAX(run_date) FROM user_history WHERE run_date < ?",
        (start_date_str,),
    )
    row = c.fetchone()
    return row[0] if row and row[0] else None


def backfill_run_summary(conn):
    """Populate run_summary from user_history when it is missing entries.

    Runs once on a database created before run_summary existed, or after rows
    have been loaded directly. It pays the full GROUP BY that this table exists
    to avoid — but pays it a single time rather than on every run.

    Returns the number of dates written.
    """
    c = conn.cursor()
    stored = c.execute("SELECT COUNT(*) FROM run_summary").fetchone()[0]
    actual = c.execute("SELECT COUNT(DISTINCT run_date) FROM user_history").fetchone()[
        0
    ]
    if stored == actual:
        return 0

    logging.info(
        "Backfilling run_summary (%d of %d dates present); this is a one-time "
        "scan of the full history.",
        stored,
        actual,
    )
    start = time.time()
    with write_transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO run_summary (run_date, record_count) "
            "SELECT run_date, COUNT(*) FROM user_history GROUP BY run_date"
        )
    logging.info(
        "run_summary backfilled: %d dates in %.1fs", actual, time.time() - start
    )
    return actual


def record_run_summary(conn, run_date, record_count):
    """Store the record count for a run date, replacing any previous value."""
    conn.execute(
        "INSERT OR REPLACE INTO run_summary (run_date, record_count) VALUES (?, ?)",
        (run_date, record_count),
    )


def get_run_date_counts(conn):
    """Return [(run_date, count), ...] ascending, for the trend graph.

    Reads the maintained summary rather than aggregating history, so the cost
    is proportional to the number of days recorded, not the number of rows.

    Reconciles first. ``init_db`` already did so at the start of the run, but
    repeating the check here costs one count and makes the graph correct no
    matter how the history arrived — a bulk import, a restore, or a future
    write path that forgets to maintain the summary. A silently truncated
    trend graph is a bad way to discover that.
    """
    backfill_run_summary(conn)
    c = conn.cursor()
    c.execute("SELECT run_date, record_count FROM run_summary ORDER BY run_date ASC")
    return c.fetchall()


def _nearest_run_date(conn, target_date_str):
    """Return the latest run_date on or before the target, or None."""
    c = conn.cursor()
    c.execute(
        "SELECT MAX(run_date) FROM user_history WHERE run_date <= ?",
        (target_date_str,),
    )
    row = c.fetchone()
    return row[0] if row and row[0] else None


def _suppress_mostly_unknown(counts, unknowns, total, limit):
    """Replace an attribute's counts with None when it is mostly unknown.

    Shared by the SQL and in-memory count paths so the suppression rule can
    never drift between them; the equivalence test then only has to hold the
    *counting* halves together.
    """
    return {
        attr: (None if total > 0 and (unknowns[attr] / total) > limit else values)
        for attr, values in counts.items()
    }


def get_attribute_counts(
    conn, target_date_str, attributes, unknown_ratio_limit=UNKNOWN_RATIO_LIMIT
):
    """Return {attribute: {value: count}} for several attributes in one pass.

    Finds the nearest run_date on or before the target, then scans that date
    once, extracting every requested attribute per row.  Running one query per
    attribute instead would re-scan the same rows and re-parse the same JSON
    documents once per attribute — six times over, for the six aggregate
    tables in the report.

    An attribute maps to None (rather than a dict) when more than
    ``unknown_ratio_limit`` of its values are unknown on that date.  That means
    the attribute was not being collected yet, and showing the counts as a
    trend delta would invent a cliff that never happened.

    Args:
        conn: An open sqlite3.Connection.
        target_date_str: Target date; the nearest run on or before it is used.
        attributes: Iterable of directory attribute names.
        unknown_ratio_limit: Suppression threshold, 0..1.

    Returns:
        {attribute: {value: count} | None}.  Every requested attribute is a
        key.  All values are None when no snapshot exists on or before the
        target date.
    """
    attributes = list(dict.fromkeys(attributes))
    if not attributes:
        return {}

    counts = {attr: {} for attr in attributes}
    unknowns = dict.fromkeys(attributes, 0)
    total = 0
    # The whole query path is guarded, including resolving the date: a report
    # section rendering "N/A" is a far better outcome than a database hiccup
    # aborting a run that has already written its snapshot.
    try:
        found_date = _nearest_run_date(conn, target_date_str)
        if not found_date:
            return dict.fromkeys(attributes)

        selects = ", ".join(["json_extract(data, ?)"] * len(attributes))
        params = [f"$.{attr}" for attr in attributes] + [found_date]

        c = conn.cursor()
        c.execute(
            f"SELECT {selects} FROM user_history WHERE run_date = ?",
            params,
        )
        for row in c:
            total += 1
            for attr, raw in zip(attributes, row):
                key = clean_val(raw) if raw else "Unknown"
                counts[attr][key] = counts[attr].get(key, 0) + 1
                if key == "Unknown":
                    unknowns[attr] += 1
    except sqlite3.Error as e:
        logging.error("Failed to query attribute counts for %s: %s", attributes, e)
        return dict.fromkeys(attributes)

    return _suppress_mostly_unknown(counts, unknowns, total, unknown_ratio_limit)


def counts_from_records(records, attributes, unknown_ratio_limit=UNKNOWN_RATIO_LIMIT):
    """Return {attribute: {value: count}} computed from in-memory records.

    The report needs today's aggregate counts moments after today's snapshot
    was written — from records the pipeline is already holding. Re-reading
    them through get_attribute_counts means scanning the day's rows and
    json_extract-ing every blob; at 50,000 people that scan profiled at
    ~130 ms for figures this computes in ~30 ms with no database touch.

    Semantics deliberately mirror get_attribute_counts — same "Unknown"
    substitution for missing/empty values, same mostly-unknown suppression —
    and a test holds the two functions to identical output for the same day.
    Only single-valued attributes should be passed: for lists the SQL path
    sees JSON array text where this sees a Python list, and the two would
    disagree on the key. Every current caller passes single-valued roles.

    Returns:
        {attribute: {value: count} | None}, one key per requested attribute,
        with None meaning "suppressed as mostly unknown".
    """
    attributes = list(dict.fromkeys(attributes))
    if not attributes:
        return {}

    records = list(records)
    total = len(records)
    counts = {attr: {} for attr in attributes}
    unknowns = dict.fromkeys(attributes, 0)
    for record in records:
        for attr in attributes:
            raw = record.get(attr)
            if isinstance(raw, list):
                # The SQL path sees a JSON array's text here; this path would
                # see a Python list, and the two would count different keys.
                # That is a programming error worth failing a test over, not
                # a data condition to paper over.
                raise ValueError(
                    f"counts_from_records got multi-valued attribute {attr!r};"
                    " only single-valued attributes may be counted in memory"
                )
            key = clean_val(raw) if raw else "Unknown"
            bucket = counts[attr]
            bucket[key] = bucket.get(key, 0) + 1
            if key == "Unknown":
                unknowns[attr] += 1

    return _suppress_mostly_unknown(counts, unknowns, total, unknown_ratio_limit)


def optimize(conn):
    """Refresh planner statistics and let SQLite tidy up before close.

    ``analysis_limit`` bounds ANALYZE so it samples rather than reading every
    index in full — a few seconds of insurance against the planner making a
    bad choice as the table grows.  ``PRAGMA optimize`` then applies whatever
    maintenance SQLite thinks is warranted.  Failures are logged and ignored:
    this is housekeeping, never a reason to fail a run.
    """
    try:
        conn.execute("PRAGMA analysis_limit = 1000")
        conn.execute("PRAGMA optimize")
        # Fold the WAL back into the main file and truncate it. Writing a day
        # into a multi-GB database dirties pages scattered across the whole
        # file, so the WAL can reach hundreds of MB; without this it stays that
        # size until some later checkpoint happens to catch up. End of run is
        # the quiet moment, and it means the next run starts clean.
        start = time.time()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logging.debug("WAL checkpoint took %.1fs", time.time() - start)
    except sqlite3.Error as e:
        logging.debug("Post-run maintenance skipped: %s", e)


def _wal_path_size(conn):
    """Return the size of this database's -wal sidecar in bytes, or 0."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        main_path = row[2] if row else None
        if not main_path:
            return 0
        wal = main_path + "-wal"
        return os.path.getsize(wal) if os.path.exists(wal) else 0
    except (sqlite3.Error, OSError):
        return 0


def log_database_health(conn):
    """Log size, row counts, and disk runway for operational monitoring.

    Emits one INFO line with enough to answer "is this growing sustainably?"
    without opening the database by hand, and escalates to WARNING on the two
    conditions that actually need attention: a WAL that is not being
    checkpointed, and a freelist large enough to be worth a VACUUM.

    Called before and after the write step, so growth per run is visible in
    the log.  Every failure path is caught — health logging must never be the
    reason a run fails.
    """
    try:
        c = conn.cursor()
        # Every figure here comes from run_summary, which holds one row per
        # day and is written in the same transaction as the snapshot it counts.
        #
        # Taking them from user_history instead means aggregating over every
        # row ever stored, twice per run. Measured on 28.6M rows: 17.2 s from
        # user_history versus 0.00 s from the summary — the health *logging*
        # was costing more than the entire rest of the run.
        #
        # The trade: these are derived numbers. They are exact for any database
        # this pipeline maintains, and could drift only if rows were deleted
        # from user_history by hand without touching run_summary. That is a
        # log line being slightly stale, against ~17 seconds on every run.
        rows, run_dates, first_run, latest_run = c.execute(
            "SELECT COALESCE(SUM(record_count), 0), COUNT(*), "
            "MIN(run_date), MAX(run_date) FROM run_summary"
        ).fetchone()
        changes = c.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
        page_count = c.execute("PRAGMA page_count").fetchone()[0]
        page_size = c.execute("PRAGMA page_size").fetchone()[0]
        freelist_count = c.execute("PRAGMA freelist_count").fetchone()[0]

        db_bytes = page_count * page_size
        # Measure free space on the filesystem holding *this* database, which
        # during a dry run is not the one holding the live database.
        db_dir = os.path.dirname(_database_path(conn)) or "."
        free_bytes = shutil.disk_usage(db_dir).free
        avg_bytes_per_day = db_bytes / run_dates if run_dates else 0
        runway_days = int(free_bytes / avg_bytes_per_day) if avg_bytes_per_day else 0
        wal_bytes = _wal_path_size(conn)

        logging.info(
            "Database health: size=%.2f GiB rows=%d changes=%d run_dates=%d "
            "range=%s..%s freelist_pages=%d wal=%.1f MiB "
            "estimated_growth=%.1f MiB/day disk_free=%.1f GiB runway=%d days",
            db_bytes / (1024**3),
            rows,
            changes,
            run_dates,
            first_run,
            latest_run,
            freelist_count,
            wal_bytes / (1024**2),
            avg_bytes_per_day / (1024**2),
            free_bytes / (1024**3),
            runway_days,
        )

        if wal_bytes >= _WAL_CRIT_BYTES:
            logging.warning(
                "WAL is %.0f MiB — checkpoints are not completing. A long-lived "
                "reader pins the WAL and prevents truncation.",
                wal_bytes / (1024**2),
            )
        elif wal_bytes >= _WAL_WARN_BYTES:
            logging.warning("WAL is %.0f MiB and growing.", wal_bytes / (1024**2))

        if page_count and (freelist_count / page_count) > _FREELIST_VACUUM_RATIO:
            logging.warning(
                "Freelist is %.0f%% of the file; schedule a VACUUM off-peak to "
                "return the space to the filesystem.",
                100.0 * freelist_count / page_count,
            )
    except Exception as e:
        logging.warning("Failed to log database health: %s", e)


def _database_path(conn):
    """Return the filesystem path backing the main database, or DB_FILE."""
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        if row and row[2]:
            return row[2]
    except sqlite3.Error:
        pass
    return DB_FILE
