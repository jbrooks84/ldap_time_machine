"""Change detection, flap suppression, and leaver confirmation.

This module answers the pipeline's core questions:

  - Which attribute changes are real, and which are oscillation noise?
  - Who has been absent long enough to be a confirmed leaver?
  - Which of those leavers have dropped out and come back before?

Terminology used throughout:

    flap / flapping
        An attribute oscillates between two values (A → B → A) within
        FLAP_LOOKBACK_DAYS.  Flaps are suppressed from the daily report
        because they are directory sync artefacts, not decisions anyone made.

    joiner
        A DN that appears in the directory for the first time in the window.

    leaver
        A DN present before the window and absent for the whole window.  The
        LEAVER_CONFIRM_DAYS delay is what stops a brief directory outage from
        reporting the entire company as having resigned.

    known flapper
        A confirmed leaver whose history already shows at least one gap — they
        have disappeared and returned before.  Flagged in the report so
        readers do not treat the departure as settled.

All functions accept an open sqlite3.Connection and leave transaction
management to the caller.
"""

import json
import logging

from . import records
from .config import FLAP_LOOKBACK_DAYS
from .db import (
    get_previous_run_date,
    get_records_for_dns,
    get_run_date_counts,
    get_run_dates_for_dns,
    get_snapshot_dns,
    get_window_run_dates,
)


def normalize_value(value):
    """Return the canonical string form used to compare attribute values.

    Lists are sorted first so that a directory reordering a multi-valued
    attribute does not read as a change.
    """
    if isinstance(value, list):
        return str(sorted(value))
    return str(value)


class FlapIndex:
    """Recently-seen attribute values, for O(1) flap checks.

    Detecting a flap means asking "have we seen this value for this
    (dn, attribute) recently?".  Asking the database that question once per
    changed attribute means thousands of queries on a reorganisation day, all
    against the same small slice of the changes table.  Loading that slice
    once and answering from memory is both faster and easier to test — the
    matching logic becomes a pure function over a dict.
    """

    def __init__(self, seen=None):
        self._seen = seen or {}

    def __len__(self):
        return len(self._seen)

    @classmethod
    def load(cls, conn, run_date, lookback_days=FLAP_LOOKBACK_DAYS):
        """Build an index from changes in [run_date - lookback, run_date).

        The upper bound is exclusive so a re-run on the same date cannot
        suppress its own changes by matching what it just wrote.

        A lookback of 0 or less disables suppression entirely: the index is
        empty, so every change is reported.  That is the right setting for a
        directory clean enough not to oscillate.

        Returns an empty index on any database error — failing open means a
        real change is reported rather than silently dropped.
        """
        if lookback_days <= 0:
            logging.debug("Flap suppression disabled (flap_lookback_days <= 0)")
            return cls({})

        seen = {}
        try:
            cursor = conn.execute(
                "SELECT dn, attribute, old_val, new_val FROM changes "
                "WHERE run_date >= date(?, ?) AND run_date < ?",
                (run_date, f"-{lookback_days} days", run_date),
            )
            for dn, attribute, old_val, new_val in cursor:
                bucket = seen.setdefault((dn, attribute), set())
                bucket.add(old_val)
                bucket.add(new_val)
        except Exception as e:
            logging.error("Failed to load flap index: %s", e)
            return cls({})
        logging.debug("Flap index loaded: %d (dn, attribute) pairs", len(seen))
        return cls(seen)

    def is_flapping(self, dn, attribute, new_value):
        """Return True if this change looks like an oscillation.

        A change flaps if the incoming value matches either side of any change
        already recorded for the same (dn, attribute) inside the lookback.
        That catches both "returning to a previous value" (A→B→A) and
        "arriving at a value seen recently" (A→B, later C→B), erring towards
        suppression — a genuine change that repeats will be reported the next
        time it moves.
        """
        bucket = self._seen.get((dn, attribute))
        if not bucket:
            return False
        return normalize_value(new_value) in bucket


def get_latest_record_for_dn(conn, dn):
    """Return the most recent stored record for a DN, or None.

    Used when a DN has left the directory but the report still needs their
    name for the leaver row.
    """
    c = conn.cursor()
    c.execute(
        "SELECT data FROM user_history WHERE dn = ? ORDER BY run_date DESC LIMIT 1",
        (dn,),
    )
    row = c.fetchone()
    return json.loads(row[0]) if row else None


def get_display_name_for_dn(conn, current_state, dn):
    """Return a human-readable name for a DN, falling back to history then the DN.

    Tries current_state first (no DB hit), then the latest historical record,
    then the raw DN.  Never returns None.
    """
    record = current_state.get(dn)
    if record:
        return records.display_name(record, dn)

    try:
        record = get_latest_record_for_dn(conn, dn)
    except Exception as e:
        logging.error("Failed to load latest record for %s: %s", dn, e)
        return dn

    return records.display_name(record, dn) if record else dn


def get_username_for_dn(conn, current_state, dn):
    """Return the login name for a DN, falling back to history then ''.

    Returns an empty string rather than None so callers can use the result in
    HTML without guarding.
    """
    record = current_state.get(dn)
    if record and records.username(record):
        return records.username(record)

    try:
        record = get_latest_record_for_dn(conn, dn)
    except Exception as e:
        logging.error("Failed to load latest record for %s: %s", dn, e)
        return ""

    return records.username(record) if record else ""


def get_changes_window(
    conn, end_date_str, window_days, important_attributes, current_state
):
    """Return tracked attribute changes within the reporting window.

    Queries the changes table for rows in the window whose attribute is
    tracked, groups them by DN, and attaches each person's current name and
    username.

    In-memory deduplication guards against rows that slip past the unique
    index, which a same-date re-run can produce.

    Args:
        conn: An open sqlite3.Connection.
        end_date_str: Last day of the window ('YYYY-MM-DD').
        window_days: How many calendar days to look back.
        important_attributes: Attribute names to filter on.
        current_state: The current {dn: record_dict} snapshot, for name lookups.

    Returns:
        (modifications, window_dates) where modifications is a list of
        {name, username, changes: [{date, attr, old, new}]}.
    """
    window_dates = get_window_run_dates(conn, end_date_str, window_days)
    if not window_dates or not important_attributes:
        return [], window_dates

    important_attributes = list(important_attributes)
    start_date = window_dates[0]
    placeholders = ",".join(["?"] * len(important_attributes))
    c = conn.cursor()
    c.execute(
        "SELECT run_date, dn, attribute, old_val, new_val "
        "FROM changes WHERE run_date BETWEEN ? AND ? "
        f"AND attribute IN ({placeholders}) ORDER BY run_date ASC",
        [start_date, end_date_str, *important_attributes],
    )

    changes_by_dn = {}
    seen = set()
    for run_date, dn, attr, old_val, new_val in c.fetchall():
        dedupe_key = (run_date, dn, attr, old_val, new_val)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        changes_by_dn.setdefault(dn, []).append(
            {"date": run_date, "attr": attr, "old": old_val, "new": new_val}
        )

    modifications = []
    for dn, changes in changes_by_dn.items():
        modifications.append(
            {
                "name": get_display_name_for_dn(conn, current_state, dn),
                "username": get_username_for_dn(conn, current_state, dn),
                "changes": changes,
            }
        )
    return modifications, window_dates


def get_joiners_window(conn, current_state, end_date_str, window_days):
    """Return the current records that appeared within the reporting window.

    A joiner is present now and absent from the last snapshot taken *before*
    the window opened. With the default one-day window this is exactly the
    diff against the previous run; with a longer window it is everyone who
    arrived since the pre-window baseline — which is what a "(7d)" section
    heading promises. Diffing only the latest two runs here would put a
    seven-day heading over a one-day list.

    Only DN sets are read — the baseline comes straight from the primary-key
    index — and the records themselves are taken from current_state, which
    the caller already holds.

    When no snapshot exists before the window (a first run, or a database
    whose whole history falls inside the window), everyone present is a
    joiner, which is both literally true and the previous behaviour.
    """
    window_dates = get_window_run_dates(conn, end_date_str, window_days)
    baseline_date = (
        get_previous_run_date(conn, window_dates[0]) if window_dates else None
    )
    if baseline_date is None:
        return list(current_state.values())
    baseline_dns = get_snapshot_dns(conn, baseline_date)
    return [rec for dn, rec in current_state.items() if dn not in baseline_dns]


def get_confirmed_leavers(conn, end_date_str, confirm_days):
    """Return leavers confirmed by continuous absence for confirm_days.

    A DN qualifies if it was present on the run immediately before the
    confirmation window and absent on every run within it.

    Only DN sets are compared across the window — those come straight from the
    primary-key index without reading a single JSON blob — and full records
    are loaded at the end for the few DNs that survive.  Comparing whole
    snapshots instead would deserialise a week of directory data to compute a
    set difference.

    Args:
        conn: An open sqlite3.Connection.
        end_date_str: Last day of the confirmation window ('YYYY-MM-DD').
        confirm_days: Consecutive absent days required.

    Returns:
        (confirmed_leavers, window_dates).  confirmed_leavers is a list of
        record dicts each carrying an added 'dn' key.
    """
    window_dates = get_window_run_dates(conn, end_date_str, confirm_days)
    if not window_dates:
        logging.warning(
            "No run dates found for leaver confirmation window (%d days)", confirm_days
        )
        return [], window_dates

    prev_date = get_previous_run_date(conn, window_dates[0])
    if not prev_date:
        return [], window_dates

    candidates = get_snapshot_dns(conn, prev_date)
    for run_date in window_dates:
        candidates -= get_snapshot_dns(conn, run_date)
        if not candidates:
            break

    prev_records = get_records_for_dns(conn, prev_date, candidates)
    confirmed = [{**record, "dn": dn} for dn, record in prev_records.items()]
    return confirmed, window_dates


def get_known_flappers(conn, dns):
    """Identify DNs that were absent from runs where they should have appeared.

    A DN "flapped" when a run happened between two of its consecutive
    appearances and it was not in that run — a real disappearance and return.
    The comparison is against runs that *actually happened*, not calendar
    days: a Monday-Friday schedule, a holiday, or a missed cron day is a gap
    for everyone and therefore evidence of nothing. Counting calendar gaps
    here instead would eventually mark every long-lived DN on a non-daily
    schedule a flapper, and the report's flapper asterisk would lose all
    meaning.

    This is a different question from ``FlapIndex.is_flapping``, which detects
    oscillating *attribute values*.  This detects presence/absence cycles.

    Args:
        conn: An open sqlite3.Connection.
        dns: DNs to check, typically the confirmed leaver set.

    Returns:
        {dn: flap_count} for DNs that have flapped at least once.  DNs with an
        unbroken history are omitted rather than returned with a zero.
    """
    dns = list(dns)
    if not dns:
        return {}

    # Position of every run that ever happened. Two consecutive appearances
    # more than one position apart means an intervening run existed without
    # this DN in it.
    position = {
        run_date: index
        for index, (run_date, _count) in enumerate(get_run_date_counts(conn))
    }

    flappers = {}
    for dn, dates in get_run_dates_for_dns(conn, dns).items():
        if len(dates) < 2:
            continue
        flap_count = 0
        for previous, current in zip(dates, dates[1:]):
            missed_runs = position[current] - position[previous] - 1
            if missed_runs > 0:
                flap_count += 1
                logging.debug(
                    "Known flapper: %s — absent from %d run(s) between %s and %s",
                    dn,
                    missed_runs,
                    previous,
                    current,
                )
        if flap_count > 0:
            flappers[dn] = flap_count
            logging.info("Flapper detected: %s has %d prior gap(s)", dn, flap_count)

    logging.info(
        "Flapper check complete: %d of %d leavers are known flappers",
        len(flappers),
        len(dns),
    )
    return flappers
