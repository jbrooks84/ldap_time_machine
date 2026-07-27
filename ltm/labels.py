"""Formatting helpers for human-readable reporting window labels.

Used in log messages, email subjects, and HTML report headings to keep
the phrasing consistent across the codebase.  Both functions accept the
same window_days integer so callers don't need to specialise for the
1-day case.
"""


def window_label(window_days):
    """Return a prose window label suitable for full sentences.

    Examples: window_label(1) -> 'last 24 hours'
              window_label(7) -> 'last 7 days'
    """
    if window_days == 1:
        return "last 24 hours"
    return f"last {window_days} days"


def window_label_short(window_days):
    """Return a compact window label suitable for badges, email subjects, and log lines.

    Examples: window_label_short(1) -> '24h'
              window_label_short(7) -> '7d'
    """
    if window_days == 1:
        return "24h"
    return f"{window_days}d"
