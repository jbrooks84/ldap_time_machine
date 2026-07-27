"""Highlights tile data helpers — pure functions returning dict | None.

Each function computes the data for one tile of the optional highlights strip
at the top of the daily report.  They return None when there is not enough
data to populate the tile — a database too young to have a 30-day baseline,
or an attribute role the configured directory does not map — and the renderer
in report.py substitutes a placeholder.

All DB-touching functions accept an open sqlite3.Connection; they do not
manage transactions or connections themselves.

``_ALLOWED_ATTRIBUTES`` restricts ``_counts_for_date`` to the two attributes
the tiles actually use.  The JSON path is passed as a bound parameter, so the
allowlist is a second line of defence — and it catches a mistyped role before
it turns into a silently empty tile.
"""

from datetime import datetime, timedelta

from . import records
from .config import (
    ATTR,
    HIGHLIGHTS_COUNTRY_MIN_BASELINE,
    HIGHLIGHTS_LOOKBACK_DAYS,
    HIGHLIGHTS_OFFICE_MIN_BASELINE,
)
from .dates import parse_join_date, tenure_str

_ALLOWED_ATTRIBUTES = frozenset(a for a in (ATTR.country, ATTR.office) if a)


def longest_tenured_leaver(leavers, today_dt):
    """Return tile data for the longest-tenured confirmed leaver, or None.

    Scans the leavers for the earliest parsed join date — the same
    parse_join_date logic the seniority table and leaver report use — and
    summarises that person.

    Args:
        leavers: List of leaver record dicts (from get_confirmed_leavers).
        today_dt: Reference datetime for the tenure calculation.

    Returns:
        ``{'name', 'tenure', 'country', 'count'}``, or None when the list is
        empty or no leaver has a parseable start date.
    """
    if not leavers:
        return None
    best = None
    best_start = None
    for record in leavers:
        start = parse_join_date(
            records.value(record, "start_date"),
            records.value(record, "created"),
        )
        if start is None:
            continue
        if best_start is None or start < best_start:
            best_start = start
            best = record
    if best is None:
        return None
    return {
        "name": records.display_name(best),
        "tenure": tenure_str(best_start, today_dt),
        "country": records.value(best, "country"),
        "count": len(leavers),
    }


def _latest_run_on_or_before(conn, target_date_str):
    """Return MAX(run_date) that is <= target_date_str, or None if none exists."""
    c = conn.cursor()
    c.execute(
        "SELECT MAX(run_date) FROM user_history WHERE run_date <= ?",
        (target_date_str,),
    )
    row = c.fetchone()
    return row[0] if row and row[0] else None


def _counts_for_date(conn, run_date, attribute):
    """Return {value: count} for a JSON attribute on a specific run_date.

    Null and empty-string values are excluded so a tile only reports on
    meaningful data points.

    Args:
        conn: An open sqlite3.Connection.
        run_date: Exact 'YYYY-MM-DD' date string.
        attribute: A directory attribute name that must be in
            ``_ALLOWED_ATTRIBUTES``.

    Returns:
        A dict of {str_value: int_count}.

    Raises:
        ValueError: if the attribute is not one the tiles are allowed to read.
    """
    if attribute not in _ALLOWED_ATTRIBUTES:
        raise ValueError(f"Disallowed attribute: {attribute!r}")
    c = conn.cursor()
    c.execute(
        "SELECT json_extract(data, ?), COUNT(*) "
        "FROM user_history WHERE run_date = ? GROUP BY 1",
        (f"$.{attribute}", run_date),
    )
    out = {}
    for key, count in c.fetchall():
        if key is None or key == "":
            continue
        out[key] = count
    return out


def _biggest_mover(now_d, past_d, min_baseline, exclude=None):
    """Return movements sorted by absolute percentage change.

    Only considers keys present in now_d whose past count is at least
    min_baseline.  Without that floor, a group that went from 1 person to 2
    reads as +100% and wins every time.

    Args:
        now_d: Current {key: count} dict.
        past_d: Historical {key: count} dict (may be missing keys from now_d).
        min_baseline: Minimum past count for a key to be considered.
        exclude: Optional set of keys to skip.

    Returns:
        List of (key, past, now, delta, pct) tuples sorted by abs(pct) desc.
    """
    moves = []
    for key, now in now_d.items():
        if exclude and key in exclude:
            continue
        past = past_d.get(key, 0)
        if past < min_baseline:
            continue
        pct = 100.0 * (now - past) / past
        moves.append((key, past, now, now - past, pct))
    moves.sort(key=lambda x: abs(x[4]), reverse=True)
    return moves


def _baseline_date(conn, today_str, days):
    """Resolve the comparison snapshot for a lookback.

    Prefers the latest run on or before ``today - days``.  When the database
    does not reach back that far, falls back to the very first run and says so
    in the label, rather than silently reporting a much shorter comparison as
    though it were the full window.

    Returns:
        A (date_str, label) tuple, or (None, None) when there is no usable
        baseline at all.
    """
    target = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=days)).strftime(
        "%Y-%m-%d"
    )
    past_date = _latest_run_on_or_before(conn, target)
    if past_date is not None and past_date != today_str:
        return past_date, f"{days}d"

    c = conn.cursor()
    c.execute("SELECT MIN(run_date) FROM user_history")
    row = c.fetchone()
    first = row[0] if row else None
    if not first or first == today_str:
        return None, None
    return first, "since first run"


def biggest_country_mover(
    conn,
    today_str,
    days=HIGHLIGHTS_LOOKBACK_DAYS,
    min_baseline=HIGHLIGHTS_COUNTRY_MIN_BASELINE,
):
    """Return tile data for the country with the largest headcount % change.

    Args:
        conn: An open sqlite3.Connection.
        today_str: Current date 'YYYY-MM-DD'.
        days: Lookback in days for the comparison baseline.
        min_baseline: Minimum historical headcount for a country to qualify.

    Returns:
        ``{'country', 'past', 'now', 'delta', 'pct', 'days_label'}``, or None
        when the country role is unmapped or no baseline exists.
    """
    attribute = ATTR.country
    if not attribute:
        return None
    past_date, days_label = _baseline_date(conn, today_str, days)
    if past_date is None:
        return None

    moves = _biggest_mover(
        _counts_for_date(conn, today_str, attribute),
        _counts_for_date(conn, past_date, attribute),
        min_baseline,
    )
    if not moves:
        return None
    key, past, now, delta, pct = moves[0]
    return {
        "country": key,
        "past": past,
        "now": now,
        "delta": delta,
        "pct": pct,
        "days_label": days_label,
    }


def biggest_office_mover(
    conn,
    today_str,
    days=HIGHLIGHTS_LOOKBACK_DAYS,
    min_baseline=HIGHLIGHTS_OFFICE_MIN_BASELINE,
    exclude_country_tuple=None,
):
    """Return tile data for the office with the largest headcount % change.

    Works like biggest_country_mover but against the office role.  The
    collision rule: if the top-moving office has the same (past, now) counts
    as the country tile, it is almost certainly the same movement seen twice,
    so it is skipped in favour of the runner-up.

    Args:
        conn: An open sqlite3.Connection.
        today_str: Current date 'YYYY-MM-DD'.
        days: Lookback in days for the comparison baseline.
        min_baseline: Minimum historical headcount for an office to qualify.
        exclude_country_tuple: Optional (past, now) tuple from the country
            tile; any office with an identical tuple is skipped.

    Returns:
        ``{'office', 'past', 'now', 'delta', 'pct', 'days_label'}``, or None.
    """
    attribute = ATTR.office
    if not attribute:
        return None
    past_date, days_label = _baseline_date(conn, today_str, days)
    if past_date is None:
        return None

    # "Unknown" is not a real office and would skew the percentage ranking.
    now_d = {
        k: v
        for k, v in _counts_for_date(conn, today_str, attribute).items()
        if k != "Unknown"
    }
    past_d = {
        k: v
        for k, v in _counts_for_date(conn, past_date, attribute).items()
        if k != "Unknown"
    }
    moves = _biggest_mover(now_d, past_d, min_baseline)
    if not moves:
        return None

    pick = None
    for move in moves:
        if exclude_country_tuple and (move[1], move[2]) == exclude_country_tuple:
            continue
        pick = move
        break
    if pick is None:
        return None
    key, past, now, delta, pct = pick
    return {
        "office": key,
        "past": past,
        "now": now,
        "delta": delta,
        "pct": pct,
        "days_label": days_label,
    }


def headcount_deltas(conn, today_str):
    """Return current headcount and 30-day / 90-day deltas.

    Args:
        conn: An open sqlite3.Connection.
        today_str: Current date 'YYYY-MM-DD'.

    Returns:
        ``{'now': int, 'delta_30': int|None, 'delta_90': int|None}``.  A delta
        is None when no baseline snapshot exists that far back.
    """
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM user_history WHERE run_date = ?", (today_str,))
    now = c.fetchone()[0]

    def count_at(target_str):
        date = _latest_run_on_or_before(conn, target_str)
        if date is None or date == today_str:
            return None
        c.execute("SELECT COUNT(*) FROM user_history WHERE run_date = ?", (date,))
        return c.fetchone()[0]

    today_dt = datetime.strptime(today_str, "%Y-%m-%d")
    hc30 = count_at((today_dt - timedelta(days=30)).strftime("%Y-%m-%d"))
    hc90 = count_at((today_dt - timedelta(days=90)).strftime("%Y-%m-%d"))
    return {
        "now": now,
        "delta_30": (now - hc30) if hc30 is not None else None,
        "delta_90": (now - hc90) if hc90 is not None else None,
    }
