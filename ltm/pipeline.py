"""Pipeline orchestration — the top-level coordinator of a daily run.

Steps, in order:

  1. Take a process-level lock so overlapping scheduled runs cannot corrupt
     each other's work.
  2. Connect to (and initialise) the SQLite database.
  3. Fetch the current directory snapshot, refusing empty or suspiciously
     small results before anything is written.
  4. Diff it against the previous snapshot for attribute changes,
     suppressing flaps.
  5. Write the new snapshot, the detected changes, and the day's summary
     row in one transaction.
  6. Compute the report windows: joiners against the pre-window baseline,
     leavers by the confirmation window, and known flappers among them.
  7. Hand off to report.send_email_report for rendering and delivery.

Callers:
  - ltm.cli calls run() — as the installed ldap-time-machine console script
    or via the repo-root ldap_time_machine.py shim.
  - ltm.cli --dry-run passes a throwaway db_path and send_email=False.
  - Tests call _run_pipeline directly with a temporary database.

Error handling philosophy:
  - Lock acquisition and the pipeline body are separated so the lock is always
    released in a finally block, even when the pipeline exits early.
  - sys.exit(1) is used for hard stops — an empty result, or a count low
    enough to suggest truncation — where continuing would persist wrong data
    that every subsequent run would then treat as truth.
  - Report sections isolate their own failures; a broken section does not
    abort the run.
"""

import fcntl
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

from . import records
from .analysis import (
    FlapIndex,
    get_changes_window,
    get_confirmed_leavers,
    get_joiners_window,
    get_known_flappers,
)
from .config import (
    DB_FILE,
    IMPORTANT_ATTRIBUTES,
    LEAVER_CONFIRM_DAYS,
    LOCK_FILE,
    MIN_LDAP_RESULT_RATIO,
    UNMAPPED_TRACKED_ROLES,
    VERSION,
    WINDOW_DAYS,
)
from .db import (
    connect_db,
    get_latest_run_date,
    get_snapshot_state,
    init_db,
    log_database_health,
    optimize,
    record_run_summary,
    write_transaction,
)
from .labels import window_label, window_label_short
from .ldap_client import fetch_directory_records
from .logging_utils import setup_logging
from .report import send_email_report

# Exit code 75 is EX_TEMPFAIL, the sysexits convention for "temporary,
# retry later".  The systemd unit in docs/operations.md declares it a
# success (SuccessExitStatus=75) so an overlap is not a failed unit; cron
# does not act on exit codes at all.
EXIT_ALREADY_RUNNING = 75


def format_duration(seconds):
    """Format an elapsed time so short steps stay legible.

    ``%.1fs`` renders anything under 50 ms as "0.0s", which reads like the step
    never ran — unhelpful in the one artifact you have when diagnosing a failed
    scheduled run. Sub-second durations are reported in milliseconds instead.
    """
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.1f}s"


def _log_record_list(title, users, marker, limit=10):
    """Log a capped list of people for the run's audit trail.

    Prints up to `limit` names with departments, then a count of the rest, so
    an operator can sanity-check a run from the log without querying the
    database — and without a reorg day writing thousands of lines.
    """
    if not users:
        return
    logging.info("%s (%d):", title, len(users))
    for user in users[:limit]:
        name = records.label(user, "Unknown")
        dept = records.value(user, "department") or "Unknown"
        logging.info("  %s %s (%s)", marker, name, dept)
    if len(users) > limit:
        logging.info("  ... and %d more", len(users) - limit)


def _acquire_run_lock():
    """Take a non-blocking exclusive file lock to prevent overlapping runs.

    Writes the current PID into the lock file so a stale lock can be traced
    back to a process.  Exits EX_TEMPFAIL if another run holds it.

    Returns:
        The open file descriptor, for _release_run_lock.
    """
    lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        logging.error("Another run is already active; exiting.")
        sys.exit(EXIT_ALREADY_RUNNING)

    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, str(os.getpid()).encode("ascii"))
    return lock_fd


def _release_run_lock(lock_fd):
    """Release the exclusive lock taken by _acquire_run_lock."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def run(db_path=None, send_email=True):
    """Run the full pipeline with lock management.

    Args:
        db_path: Optional database path override (used by --dry-run).
        send_email: When False, the report is archived but not emailed.
    """
    setup_logging()
    lock_fd = _acquire_run_lock()
    try:
        _run_pipeline(db_path=db_path, send_email=send_email)
    finally:
        _release_run_lock(lock_fd)


def _resolve_baseline(conn, yesterday_str):
    """Return (previous_state, comparison_date) for the diff.

    Prefers yesterday.  When yesterday is missing — a skipped run, a weekend,
    a holiday — falls back to the most recent snapshot on file rather than
    treating everyone as a joiner.
    """
    previous_state = get_snapshot_state(conn, yesterday_str)
    if previous_state:
        return previous_state, yesterday_str

    last_run = get_latest_run_date(conn)
    if not last_run:
        logging.info("First run - no previous state to compare against")
        return {}, yesterday_str

    logging.info("No data for %s, using last available: %s", yesterday_str, last_run)
    return get_snapshot_state(conn, last_run), last_run


def _diff_snapshots(current_state, previous_state, flap_index, today_str):
    """Compare two snapshots and return the changes worth recording.

    Args:
        current_state: Today's {dn: record}.
        previous_state: The baseline {dn: record}.
        flap_index: A loaded FlapIndex used to suppress oscillations.
        today_str: Today's date, stamped onto each change row.

    Returns:
        (change_rows, modifications, suppressed_flaps) where change_rows are
        tuples ready for executemany and modifications is the per-person view
        the report renders.
    """
    change_rows = []
    modifications = []
    suppressed_flaps = 0

    common_dns = set(current_state) & set(previous_state)
    logging.info("Comparing %d common records...", len(common_dns))

    for dn in common_dns:
        old_rec = previous_state[dn]
        new_rec = current_state[dn]

        # On a typical day almost nothing changes, and dict equality is a
        # single C-level comparison. Equal records cannot produce a diff, so
        # skipping them is pure fast path — profiled at 50,000 people it
        # removes the key-by-key loop for ~99% of the directory. (Unequal
        # records still go through the loop, which is what handles the
        # list-reordering case equality is too strict for.)
        if old_rec == new_rec:
            continue

        meaningful = []

        for key in set(old_rec) | set(new_rec):
            old_val = old_rec.get(key, "N/A")
            new_val = new_rec.get(key, "N/A")

            # Sort lists so a directory reordering a multi-valued attribute
            # does not register as a change.
            if isinstance(old_val, list):
                old_val = sorted(old_val)
            if isinstance(new_val, list):
                new_val = sorted(new_val)

            if old_val == new_val:
                continue

            if flap_index.is_flapping(dn, key, new_val):
                logging.debug(
                    "SUPPRESSED FLAP for %s [%s]: %s -> %s", dn, key, old_val, new_val
                )
                suppressed_flaps += 1
                continue

            change_rows.append((today_str, dn, key, str(old_val), str(new_val)))
            if key in IMPORTANT_ATTRIBUTES:
                meaningful.append({"attr": key, "old": old_val, "new": new_val})

        if meaningful:
            modifications.append(
                {
                    "name": records.display_name(new_rec, dn),
                    "username": records.username(new_rec),
                    "changes": meaningful,
                }
            )

    return change_rows, modifications, suppressed_flaps


def _persist(conn, today_str, current_state, change_rows):
    """Write today's snapshot and change rows in a single write transaction.

    Today's rows are deleted first so a re-run replaces the day rather than
    duplicating it.  Everything happens inside one BEGIN IMMEDIATE, so a
    failure part-way through cannot leave the day half-written.
    """

    def snapshot_rows():
        """Yield insert tuples one at a time.

        A generator rather than a list so the serialised JSON blobs — tens of
        megabytes at large directory sizes — stream into executemany instead
        of all being held alongside the two full snapshots already in memory.
        The compact separators are deliberate: the default ", " / ": " padding
        is pure whitespace inside a stored blob, and dropping it shrinks every
        row (and therefore every future read and backup) by roughly 6%.
        """
        for dn, data in current_state.items():
            json_str = json.dumps(data, separators=(",", ":"))
            digest = hashlib.md5(json_str.encode("utf-8")).hexdigest()
            yield (today_str, dn, json_str, digest)

    start = time.time()
    logging.info("Saving %d snapshots to database...", len(current_state))
    with write_transaction(conn):
        conn.execute("DELETE FROM user_history WHERE run_date = ?", (today_str,))
        conn.execute("DELETE FROM changes WHERE run_date = ?", (today_str,))
        conn.executemany(
            "INSERT INTO user_history (run_date, dn, data, data_hash) "
            "VALUES (?, ?, ?, ?)",
            snapshot_rows(),
        )
        if change_rows:
            logging.info("Saving %d changes to database...", len(change_rows))
            conn.executemany(
                "INSERT OR IGNORE INTO changes "
                "(run_date, dn, attribute, old_val, new_val) VALUES (?, ?, ?, ?, ?)",
                change_rows,
            )
        # Same transaction as the snapshot, so the count can never disagree
        # with the rows it describes.
        record_run_summary(conn, today_str, len(current_state))
    logging.info("Database save completed in %s", format_duration(time.time() - start))


def _run_pipeline(db_path=None, send_email=True):
    """Execute the pipeline after logging and the run lock are in place.

    Args:
        db_path: Optional database path override.
        send_email: Passed through to send_email_report.
    """
    start_time = time.time()
    today_str = datetime.now().strftime("%Y-%m-%d")
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    logging.info("=" * 60)
    logging.info("LDAP Time Machine v%s starting - %s", VERSION, today_str)
    logging.info("=" * 60)

    if UNMAPPED_TRACKED_ROLES:
        logging.warning(
            "report.tracked_roles includes role(s) this directory flavor "
            "leaves unmapped: %s — their change sections will not appear.",
            ", ".join(UNMAPPED_TRACKED_ROLES),
        )

    conn = init_db(db_path=db_path)
    logging.info("Database connection established")
    log_database_health(conn)

    # Fetch — abort on an empty result or a count below the safety threshold.
    ldap_start = time.time()
    current_state = fetch_directory_records()
    ldap_duration = time.time() - ldap_start

    if not current_state:
        logging.error("Directory fetch failed - no records returned. Aborting.")
        sys.exit(1)

    logging.info(
        "Directory fetch completed: %d records in %s",
        len(current_state),
        format_duration(ldap_duration),
    )

    previous_state, comparison_date = _resolve_baseline(conn, yesterday_str)
    logging.info(
        "Loaded %d records from previous run (%s)", len(previous_state), comparison_date
    )

    if previous_state:
        min_expected = int(len(previous_state) * MIN_LDAP_RESULT_RATIO)
        if len(current_state) < min_expected:
            logging.error(
                "Directory returned %d records, below %.0f%% of the previous "
                "snapshot of %d (minimum %d). Aborting to avoid storing "
                "partial data.",
                len(current_state),
                MIN_LDAP_RESULT_RATIO * 100,
                len(previous_state),
                min_expected,
            )
            sys.exit(1)

    new_dns = set(current_state) - set(previous_state)
    removed_dns = set(previous_state) - set(current_state)

    flap_index = FlapIndex.load(conn, today_str)
    change_rows, daily_modifications, suppressed_flaps = _diff_snapshots(
        current_state, previous_state, flap_index, today_str
    )

    logging.info(
        "Comparison complete: %d new, %d removed, %d modified",
        len(new_dns),
        len(removed_dns),
        len(daily_modifications),
    )
    logging.info(
        "Total attribute changes: %d, Suppressed flaps: %d",
        len(change_rows),
        suppressed_flaps,
    )

    _persist(conn, today_str, current_state, change_rows)

    report_window_days = WINDOW_DAYS
    # Joiners for the report are computed against the pre-window baseline,
    # not the raw diff above — with window_days > 1 those differ, and the
    # section heading advertises the window.
    daily_new_users = get_joiners_window(
        conn, current_state, today_str, report_window_days
    )

    confirmed_leavers, leaver_window_dates = get_confirmed_leavers(
        conn, today_str, LEAVER_CONFIRM_DAYS
    )
    if not leaver_window_dates:
        logging.info("Insufficient history for confirmed leaver window.")

    confirmed_leaver_dns = {user.get("dn", "") for user in confirmed_leavers}
    flapper_dns = get_known_flappers(conn, confirmed_leaver_dns)

    logging.info(
        "Daily window (%s): %d new, %d same-day absences",
        window_label_short(report_window_days),
        len(daily_new_users),
        len(removed_dns),
    )
    logging.info(
        "Confirmed leaver window (%s): %d leavers%s",
        window_label_short(LEAVER_CONFIRM_DAYS),
        len(confirmed_leavers),
        f" ({len(flapper_dns)} known flappers)" if flapper_dns else "",
    )

    _log_record_list(
        f"NEW JOINERS ({window_label(report_window_days)})", daily_new_users, "+"
    )
    _log_record_list(
        f"CONFIRMED LEAVERS ({window_label_short(LEAVER_CONFIRM_DAYS)})",
        confirmed_leavers,
        "-",
    )

    window_modifications, change_window_dates = get_changes_window(
        conn, today_str, report_window_days, IMPORTANT_ATTRIBUTES, current_state
    )
    if not change_window_dates:
        logging.info("Insufficient history for change window; using daily changes.")
        window_modifications = daily_modifications

    logging.info(
        "Change window (%s): %d records changed",
        window_label_short(report_window_days),
        len(window_modifications),
    )

    send_email_report(
        daily_new_users,
        confirmed_leavers,
        window_modifications,
        conn,
        today_str,
        current_state,
        window_days=report_window_days,
        leaver_window_days=LEAVER_CONFIRM_DAYS,
        flapper_dns=flapper_dns,
        send_email=send_email,
    )

    optimize(conn)

    # Health is logged after optimize() has truncated the WAL, not right
    # after the write. Immediately post-write the WAL sits at its transient
    # peak — 900+ MB on a multi-GB database — and the health check would
    # cry "checkpoints are not completing" about a file this same run
    # truncates seconds later. The warning thresholds are meant for the
    # between-runs steady state, so measure the between-runs state.
    log_database_health(conn)

    logging.info("=" * 60)
    logging.info(
        "Run completed successfully in %s",
        format_duration(time.time() - start_time),
    )
    logging.info(
        "Summary (%s): +%d new | -%d confirmed left%s | %d changed",
        window_label_short(report_window_days),
        len(daily_new_users),
        len(confirmed_leavers),
        f" ({len(flapper_dns)} known flappers)" if flapper_dns else "",
        len(window_modifications),
    )
    logging.info("=" * 60)
    conn.close()


def replay(date_str, db_path=None):
    """Re-render the report a past run date would have produced.

    Everything comes from stored history: the snapshot for the date, joiners
    against the pre-window baseline, the leaver confirmation window, known
    flappers, the change window, and a trend graph truncated at that date.
    Tenure is computed as of the replayed date, not today.

    The rendered HTML is archived to the dry-run bucket (``dryrun_`` prefix)
    and nothing is ever emailed — a replay is an investigation tool, not a
    second delivery.  No run lock is taken; the only writes are the trend
    graph PNG, the archive file, and, on a database predating ``run_summary``,
    the same one-time backfill of that cache the nightly run would perform.

    Args:
        date_str: The run date to replay, 'YYYY-MM-DD'.  Must be a date a
            snapshot actually exists for.
        db_path: Optional database path override.

    Returns:
        The path of the archived HTML.

    Exits with status 1 when the database or the requested snapshot does not
    exist — replay can only re-render what a run once stored.
    """
    path = db_path if db_path is not None else DB_FILE
    if not os.path.exists(path):
        logging.error("No database at %s — nothing to replay.", path)
        sys.exit(1)

    conn = connect_db(db_path=path)
    try:
        current_state = get_snapshot_state(conn, date_str)
        if not current_state:
            latest = get_latest_run_date(conn)
            logging.error(
                "No snapshot stored for %s (latest run on file: %s). "
                "Replay can only re-render a date a run recorded.",
                date_str,
                latest or "none",
            )
            sys.exit(1)

        logging.info(
            "Replaying %s: %d records in that snapshot", date_str, len(current_state)
        )

        joiners = get_joiners_window(conn, current_state, date_str, WINDOW_DAYS)
        leavers, _ = get_confirmed_leavers(conn, date_str, LEAVER_CONFIRM_DAYS)
        flapper_dns = get_known_flappers(conn, {user.get("dn", "") for user in leavers})
        modifications, _ = get_changes_window(
            conn, date_str, WINDOW_DAYS, IMPORTANT_ATTRIBUTES, current_state
        )

        archive_path = send_email_report(
            joiners,
            leavers,
            modifications,
            conn,
            date_str,
            current_state,
            window_days=WINDOW_DAYS,
            leaver_window_days=LEAVER_CONFIRM_DAYS,
            flapper_dns=flapper_dns,
            send_email=False,
        )
    finally:
        conn.close()

    logging.info(
        "Replay of %s complete: +%d joiners, -%d leavers, %d changed. Archived to %s",
        date_str,
        len(joiners),
        len(leavers),
        len(modifications),
        archive_path,
    )
    return archive_path


def main():
    """CLI entry point wrapper: log any unhandled exception, then re-raise."""
    try:
        run()
    except Exception as e:
        logging.exception("Unhandled error in pipeline: %s", e)
        raise
