"""Tests for date parsing and tenure formatting.

The generalizedTime cases matter more than they look: Active Directory writes
``.0Z``, OpenLDAP writes a bare ``Z``, and some servers write a numeric offset.
Parsing only the AD form means every non-AD deployment silently reports
"Unknown" tenure for everyone.
"""

from datetime import datetime

from ltm.dates import (
    parse_date_value,
    parse_generalized_time,
    parse_join_date,
    tenure_str,
)

# ── generalizedTime ───────────────────────────────────────────────


def test_parses_active_directory_form_with_fractional_seconds():
    assert parse_generalized_time("20200615123045.0Z") == datetime(
        2020, 6, 15, 12, 30, 45
    )


def test_parses_openldap_form_without_fraction():
    assert parse_generalized_time("20200615123045Z") == datetime(
        2020, 6, 15, 12, 30, 45
    )


def test_parses_bare_stamp_without_a_timezone():
    assert parse_generalized_time("20200615123045") == datetime(2020, 6, 15, 12, 30, 45)


def test_parses_numeric_offsets_in_both_notations():
    assert parse_generalized_time("20200615123045.123+0100") == datetime(
        2020, 6, 15, 12, 30, 45
    )
    assert parse_generalized_time("20200615123045-05:00") == datetime(
        2020, 6, 15, 12, 30, 45
    )


def test_rejects_non_generalized_time_input():
    assert parse_generalized_time("") is None
    assert parse_generalized_time(None) is None
    assert parse_generalized_time("2020-06-15") is None
    assert parse_generalized_time("not-a-date") is None


def test_rejects_a_well_formed_stamp_with_an_impossible_date():
    # 14 digits, right shape, month 99 — the regex passes, strptime must not.
    assert parse_generalized_time("20209915123045Z") is None


# ── General date values ───────────────────────────────────────────


def test_parses_plain_and_slashed_dates():
    assert parse_date_value("2020-06-15") == datetime(2020, 6, 15)
    assert parse_date_value("2020/06/15") == datetime(2020, 6, 15)


def test_parses_an_iso_timestamp_by_truncating_the_time():
    assert parse_date_value("2020-06-15T09:30:00Z") == datetime(2020, 6, 15)
    assert parse_date_value("2020-06-15 09:30:00") == datetime(2020, 6, 15)


def test_returns_none_for_unusable_values():
    assert parse_date_value(None) is None
    assert parse_date_value("") is None
    assert parse_date_value("   ") is None
    assert parse_date_value("garbage") is None


# ── Join date ─────────────────────────────────────────────────────


def test_uses_the_hr_start_date_alone():
    assert parse_join_date("2020-06-15", None) == datetime(2020, 6, 15)


def test_uses_the_created_timestamp_alone():
    assert parse_join_date(None, "20200615123045.0Z") == datetime(
        2020, 6, 15, 12, 30, 45
    )


def test_picks_the_earlier_of_the_two():
    # A record recreated after a migration must not reset someone's tenure.
    assert parse_join_date("2020-06-15", "20180101000000.0Z").year == 2018


def test_returns_none_when_neither_parses():
    assert parse_join_date(None, None) is None
    assert parse_join_date("", "") is None
    assert parse_join_date("garbage", "more-garbage") is None


# ── Tenure formatting ─────────────────────────────────────────────


def test_years_and_months():
    assert tenure_str(datetime(2020, 1, 1), datetime(2026, 5, 4)) == "6y 4m"


def test_months_only_under_a_year():
    assert tenure_str(datetime(2026, 1, 1), datetime(2026, 5, 4)) == "4m"


def test_days_only_under_a_month():
    assert tenure_str(datetime(2026, 5, 1), datetime(2026, 5, 4)) == "3d"


def test_same_day_is_zero_days():
    assert tenure_str(datetime(2026, 5, 4), datetime(2026, 5, 4)) == "0d"


def test_exactly_one_year_reports_zero_months():
    assert tenure_str(datetime(2025, 1, 1), datetime(2026, 1, 1)) == "1y 0m"


def test_parses_an_undelimited_date():
    assert parse_date_value("20200615") == datetime(2020, 6, 15)
