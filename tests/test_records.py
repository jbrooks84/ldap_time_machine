"""Tests for role-based record access."""

from ltm import records

from .conftest import ATTR_CN, ATTR_DISPLAY, ATTR_USERNAME, make_record


def test_value_reads_through_the_role_map():
    record = make_record("alice", "Alice Adams", title="Staff Engineer")
    assert records.value(record, "job_title") == "Staff Engineer"


def test_value_returns_the_default_for_an_unmapped_role():
    record = make_record("alice", "Alice Adams")
    assert records.value(record, "start_date", "n/a") == "n/a"


def test_value_returns_the_default_for_a_missing_attribute():
    assert records.value({}, "job_title", "n/a") == "n/a"


def test_display_name_prefers_the_display_role():
    record = {ATTR_DISPLAY: "Preferred Name", ATTR_CN: "Legal Name"}
    assert records.display_name(record) == "Preferred Name"


def test_display_name_falls_back_to_common_name():
    assert records.display_name({ATTR_CN: "Legal Name"}) == "Legal Name"


def test_display_name_strips_whitespace():
    assert records.display_name({ATTR_DISPLAY: "  Padded  "}) == "Padded"


def test_display_name_skips_a_blank_display_attribute():
    record = {ATTR_DISPLAY: "   ", ATTR_CN: "Legal Name"}
    assert records.display_name(record) == "Legal Name"


def test_display_name_unwraps_a_single_item_list():
    assert records.display_name({ATTR_DISPLAY: ["Listed Name"]}) == "Listed Name"


def test_display_name_handles_an_empty_list():
    assert records.display_name({ATTR_DISPLAY: [], ATTR_CN: "Fallback"}) == "Fallback"


def test_display_name_returns_the_default_when_nothing_matches():
    assert records.display_name({}, "unknown") == "unknown"


def test_username_reads_and_strips():
    assert records.username({ATTR_USERNAME: "  alice "}) == "alice"


def test_username_unwraps_a_list():
    assert records.username({ATTR_USERNAME: ["alice"]}) == "alice"


def test_username_returns_the_default_when_absent():
    assert records.username({}, "n/a") == "n/a"
    assert records.username({ATTR_USERNAME: []}, "n/a") == "n/a"


def test_label_prefers_display_name_then_username():
    assert records.label({ATTR_DISPLAY: "Alice Adams"}) == "Alice Adams"
    assert records.label({ATTR_USERNAME: "alice"}) == "alice"
    assert records.label({}, "Unknown") == "Unknown"
