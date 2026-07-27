"""Shared date parsing and tenure formatting helpers.

These pure functions turn the assorted date formats directories emit into
``datetime`` objects, and format durations for display.  They are kept free of
database and config dependencies so they can be imported anywhere.
"""

import re
from datetime import datetime

# LDAP generalizedTime (RFC 4517) as emitted by whenCreated / createTimestamp:
#   20200615123045Z          OpenLDAP createTimestamp
#   20200615123045.0Z        Active Directory whenCreated
#   20200615123045.123+0100
# Fractional seconds and the timezone suffix are both optional, and both are
# discarded — this tool reports tenure in whole days, so sub-second precision
# and a few hours of offset never change the answer.
_GENERALIZED_TIME = re.compile(r"^(?P<stamp>\d{14})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$")

# Plain date forms an HR-sourced start-date attribute might use, beyond the
# ISO form that datetime.fromisoformat already covers.
_FALLBACK_DATE_FORMATS = ("%Y/%m/%d", "%Y%m%d")


def parse_generalized_time(value):
    """Parse an LDAP generalizedTime string, or return None.

    Accepts the AD (``.0Z``), OpenLDAP (``Z``), and offset (``+0100``)
    variants.  Anything that is not exactly a 14-digit stamp with an optional
    fraction and timezone is rejected rather than guessed at.
    """
    if not value:
        return None
    match = _GENERALIZED_TIME.match(str(value).strip())
    if not match:
        return None
    # The fields are built by hand rather than with strptime. strptime
    # re-parses its format string on every call (~6 µs); this is ~0.6 µs, and
    # the seniority ranking calls it once per person per run — profiled at
    # 50,000 people, that one line was the single largest Python cost in the
    # whole pipeline. The regex above already guarantees 14 digits, and the
    # datetime constructor still rejects impossible dates like month 99.
    stamp = match.group("stamp")
    try:
        return datetime(
            int(stamp[0:4]),
            int(stamp[4:6]),
            int(stamp[6:8]),
            int(stamp[8:10]),
            int(stamp[10:12]),
            int(stamp[12:14]),
        )
    except ValueError:
        return None


def parse_date_value(value):
    """Parse a date-ish attribute value into a datetime, or return None.

    Tries, in order: LDAP generalizedTime, then the plain date formats applied
    to the value truncated at any time component.  Returns None when nothing
    matches, so callers can treat "no usable date" uniformly.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    stamp = parse_generalized_time(text)
    if stamp:
        return stamp

    # Trim an ISO time component ("2020-06-15T00:00:00Z") down to the date.
    head = text.split("T", 1)[0].split(" ", 1)[0]
    # fromisoformat handles the common YYYY-MM-DD case in C; the strptime
    # loop only runs for the rarer slashed and undelimited forms.
    try:
        return datetime.fromisoformat(head)
    except ValueError:
        pass
    for fmt in _FALLBACK_DATE_FORMATS:
        try:
            return datetime.strptime(head, fmt)
        except ValueError:
            continue
    return None


def parse_join_date(start_date, created):
    """Return the earliest of an HR start date and a record creation date.

    ``start_date`` comes from the optional ``start_date`` attribute role — a
    schema extension some directories carry (an HR-sourced service date).
    ``created`` comes from the ``created`` role (``whenCreated`` on Active
    Directory, ``createTimestamp`` on OpenLDAP).

    When both parse, the earlier wins, so tenure reflects when the person
    actually started rather than when their directory record happened to be
    created — those can differ by weeks, and by years after a migration.

    Args:
        start_date: HR start date value, or None/empty.
        created: Directory creation timestamp value, or None/empty.

    Returns:
        A datetime, or None if neither value could be parsed.
    """
    parsed = [d for d in (parse_date_value(start_date), parse_date_value(created)) if d]
    return min(parsed) if parsed else None


def tenure_str(start, end):
    """Format the duration between two datetimes as a compact string.

    Returns one of three shapes depending on magnitude:
        ``3y 2m``  — years + months (tenure >= 1 year)
        ``5m``     — months only (< 1 year, >= 1 month)
        ``18d``    — days only (< 1 month)

    Month rounding uses 30-day months; this is intentionally approximate and
    consistent with how HR systems usually display tenure.

    Args:
        start: Start datetime (e.g. from parse_join_date).
        end: End datetime (e.g. datetime.now() or the report date).

    Returns:
        A formatted string such as ``2y 4m``, ``11m``, or ``3d``.
    """
    days = (end - start).days
    years = days // 365
    months = (days % 365) // 30
    if years > 0:
        return f"{years}y {months}m"
    if months > 0:
        return f"{months}m"
    return f"{days}d"
