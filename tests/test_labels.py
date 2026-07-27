"""Tests for reporting-window labels."""

from ltm.labels import window_label, window_label_short


def test_a_single_day_reads_as_hours():
    """ "last 1 days" is the kind of wording that makes a report look unfinished."""
    assert window_label(1) == "last 24 hours"
    assert window_label_short(1) == "24h"


def test_multiple_days_are_pluralised():
    assert window_label(7) == "last 7 days"
    assert window_label_short(7) == "7d"


def test_larger_windows_follow_the_same_shape():
    assert window_label(30) == "last 30 days"
    assert window_label_short(30) == "30d"
