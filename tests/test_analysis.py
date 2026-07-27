"""Tests for flap suppression, leaver confirmation, and flapper detection.

These are the rules that decide whether a person appears in someone's inbox as
having left the company, so the edge cases here are the ones with real-world
consequences for getting them wrong.
"""

import pytest

from ltm import analysis
from ltm.config import IMPORTANT_ATTRIBUTES

from .conftest import (
    ATTR_CITY,
    ATTR_COUNTRY,
    ATTR_DEPT,
    ATTR_TITLE,
    FailingConnection,
    dn_for,
    make_record,
    write_changes,
    write_snapshot,
)

# ── Value normalisation ───────────────────────────────────────────


def test_normalize_value_sorts_lists():
    """Attribute order is not meaningful; only membership is."""
    assert analysis.normalize_value(["b", "a"]) == analysis.normalize_value(["a", "b"])


def test_normalize_value_stringifies_scalars():
    assert analysis.normalize_value("x") == "x"
    assert analysis.normalize_value(3) == "3"


# ── FlapIndex ─────────────────────────────────────────────────────


def test_flap_index_detects_a_return_to_a_previous_value(conn):
    """A -> B -> A within the lookback is noise, not a decision."""
    write_changes(conn, [("2026-06-20", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert index.is_flapping("CN=A", "title", "Engineer")


def test_flap_index_detects_arrival_at_a_recently_seen_value(conn):
    write_changes(conn, [("2026-06-20", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert index.is_flapping("CN=A", "title", "Analyst")


def test_flap_index_allows_a_genuinely_new_value(conn):
    write_changes(conn, [("2026-06-20", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert not index.is_flapping("CN=A", "title", "Director")


def test_flap_index_is_scoped_to_the_dn_and_attribute(conn):
    write_changes(conn, [("2026-06-20", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert not index.is_flapping("CN=B", "title", "Engineer")
    assert not index.is_flapping("CN=A", "department", "Engineer")


def test_flap_index_ignores_changes_beyond_the_lookback(conn):
    write_changes(conn, [("2026-05-01", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert not index.is_flapping("CN=A", "title", "Engineer")


def test_flap_index_excludes_the_same_day(conn):
    """A re-run must not suppress the changes it just wrote."""
    write_changes(conn, [("2026-06-25", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert not index.is_flapping("CN=A", "title", "Engineer")


def test_flap_index_normalises_list_values(conn):
    write_changes(
        conn, [("2026-06-20", "CN=A", "memberOf", "['a', 'b']", "['a', 'c']")]
    )
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=14)
    assert index.is_flapping("CN=A", "memberOf", ["b", "a"])


def test_flap_index_is_empty_with_no_history(conn):
    index = analysis.FlapIndex.load(conn, "2026-06-25")
    assert len(index) == 0
    assert not index.is_flapping("CN=A", "title", "anything")


def test_flap_index_fails_open_on_a_database_error(caplog):
    """A broken query must report the change, never silently swallow it."""
    with caplog.at_level("ERROR"):
        index = analysis.FlapIndex.load(FailingConnection(), "2026-06-25")
    assert len(index) == 0
    assert not index.is_flapping("CN=A", "title", "x")
    assert "Failed to load flap index" in caplog.text


def test_flap_index_can_be_built_directly():
    index = analysis.FlapIndex({("CN=A", "title"): {"Engineer"}})
    assert index.is_flapping("CN=A", "title", "Engineer")


# ── Name resolution ───────────────────────────────────────────────


def test_display_name_comes_from_the_current_state_first(sample_db):
    state = {dn_for("alice"): make_record("alice", "Alice Adams")}
    name = analysis.get_display_name_for_dn(sample_db, state, dn_for("alice"))
    assert name == "Alice Adams"


def test_display_name_falls_back_to_stored_history(sample_db):
    """A leaver is gone from the current snapshot but still needs a name."""
    name = analysis.get_display_name_for_dn(sample_db, {}, dn_for("erin"))
    assert name == "Erin Evans"


def test_display_name_falls_back_to_the_dn_when_unknown(sample_db):
    assert analysis.get_display_name_for_dn(sample_db, {}, "CN=Ghost") == "CN=Ghost"


def test_display_name_survives_a_database_error(caplog):
    with caplog.at_level("ERROR"):
        name = analysis.get_display_name_for_dn(FailingConnection(), {}, "CN=A")
    assert name == "CN=A"


def test_username_comes_from_the_current_state_first(sample_db):
    state = {dn_for("alice"): make_record("alice", "Alice Adams")}
    assert analysis.get_username_for_dn(sample_db, state, dn_for("alice")) == "alice"


def test_username_falls_back_to_stored_history(sample_db):
    assert analysis.get_username_for_dn(sample_db, {}, dn_for("erin")) == "erin"


def test_username_is_empty_for_an_unknown_dn(sample_db):
    assert analysis.get_username_for_dn(sample_db, {}, "CN=Ghost") == ""


def test_username_survives_a_database_error(caplog):
    with caplog.at_level("ERROR"):
        assert analysis.get_username_for_dn(FailingConnection(), {}, "CN=A") == ""


def test_get_latest_record_for_dn_returns_none_when_absent(conn):
    assert analysis.get_latest_record_for_dn(conn, "CN=Nobody") is None


# ── Change window ─────────────────────────────────────────────────


def test_changes_window_groups_by_person(sample_db, today, current_state):
    mods, dates = analysis.get_changes_window(
        sample_db, today, 1, IMPORTANT_ATTRIBUTES, current_state
    )
    assert dates == [today]
    by_name = {m["name"]: m for m in mods}
    # Carol changed both country and city, and must appear once with two changes.
    assert len(by_name["Carol Chen"]["changes"]) == 2


def test_changes_window_attaches_names_and_usernames(sample_db, today, current_state):
    mods, _ = analysis.get_changes_window(
        sample_db, today, 1, IMPORTANT_ATTRIBUTES, current_state
    )
    bob = next(m for m in mods if m["name"] == "Bob Brown")
    assert bob["username"] == "bob"


def test_changes_window_filters_to_tracked_attributes(sample_db, today, current_state):
    mods, _ = analysis.get_changes_window(
        sample_db, today, 1, [ATTR_TITLE], current_state
    )
    attrs = {c["attr"] for m in mods for c in m["changes"]}
    assert attrs == {ATTR_TITLE}


def test_changes_window_is_empty_with_no_tracked_attributes(sample_db, today):
    mods, _ = analysis.get_changes_window(sample_db, today, 1, [], {})
    assert mods == []


def test_changes_window_is_empty_outside_the_history(sample_db):
    mods, dates = analysis.get_changes_window(
        sample_db, "1999-01-01", 1, IMPORTANT_ATTRIBUTES, {}
    )
    assert mods == [] and dates == []


def test_the_unique_index_rejects_duplicate_change_rows(conn, today):
    """The first line of defence: the database refuses the duplicate outright."""
    import sqlite3

    row = (today, dn_for("alice"), ATTR_TITLE, "Engineer", "Analyst")
    conn.execute(
        "INSERT INTO changes (run_date, dn, attribute, old_val, new_val) "
        "VALUES (?, ?, ?, ?, ?)",
        row,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO changes (run_date, dn, attribute, old_val, new_val) "
            "VALUES (?, ?, ?, ?, ?)",
            row,
        )


def test_changes_window_deduplicates_rows_from_a_legacy_database(conn, today):
    """Databases predating changes_unique can hold duplicates; dedupe in memory.

    The index is dropped here to reproduce that state, because with the index
    in place the duplicate cannot be written in the first place.
    """
    conn.execute("DROP INDEX changes_unique")
    write_snapshot(conn, today, {dn_for("alice"): make_record("alice", "Alice Adams")})
    row = (today, dn_for("alice"), ATTR_TITLE, "Engineer", "Analyst")
    conn.executemany(
        "INSERT INTO changes (run_date, dn, attribute, old_val, new_val) "
        "VALUES (?, ?, ?, ?, ?)",
        [row, row],
    )
    mods, _ = analysis.get_changes_window(conn, today, 1, [ATTR_TITLE], {})
    assert len(mods[0]["changes"]) == 1


# ── Confirmed leavers ─────────────────────────────────────────────


def test_confirmed_leavers_finds_someone_absent_the_whole_window(sample_db, today):
    leavers, _ = analysis.get_confirmed_leavers(sample_db, today, 7)
    names = {leaver.get("displayName") for leaver in leavers}
    assert "Erin Evans" in names
    assert "Frank Foster" in names


def test_confirmed_leavers_excludes_people_still_present(sample_db, today):
    leavers, _ = analysis.get_confirmed_leavers(sample_db, today, 7)
    names = {leaver.get("displayName") for leaver in leavers}
    assert "Alice Adams" not in names


def test_confirmed_leavers_debounces_a_one_day_absence(sample_db, today):
    """The whole point of the delay: one missing day is not a departure."""
    leavers, _ = analysis.get_confirmed_leavers(sample_db, today, 7)
    names = {leaver.get("displayName") for leaver in leavers}
    assert "Mallory Mann" not in names


def test_confirmed_leavers_carry_their_dn(sample_db, today):
    leavers, _ = analysis.get_confirmed_leavers(sample_db, today, 7)
    assert all("dn" in leaver for leaver in leavers)


def test_confirmed_leavers_needs_a_prior_baseline(conn, today):
    """With only one day of history there is no 'before' to compare against."""
    write_snapshot(conn, today, {dn_for("alice"): make_record("alice", "Alice")})
    leavers, dates = analysis.get_confirmed_leavers(conn, today, 7)
    assert leavers == [] and dates == [today]


def test_confirmed_leavers_is_empty_without_any_history(conn, caplog):
    with caplog.at_level("WARNING"):
        leavers, dates = analysis.get_confirmed_leavers(conn, "2026-06-30", 7)
    assert leavers == [] and dates == []
    assert "No run dates found" in caplog.text


def test_confirm_days_controls_what_counts_as_a_departure(sample_db, today):
    """Same data, different threshold: Mallory qualifies at 1 day, not at 7."""
    at_one_day, _ = analysis.get_confirmed_leavers(sample_db, today, 1)
    at_seven_days, _ = analysis.get_confirmed_leavers(sample_db, today, 7)
    assert "Mallory Mann" in {leaver.get("displayName") for leaver in at_one_day}
    assert "Mallory Mann" not in {leaver.get("displayName") for leaver in at_seven_days}


# ── Known flappers ────────────────────────────────────────────────


def test_known_flappers_detects_a_gap_in_presence(sample_db):
    flappers = analysis.get_known_flappers(sample_db, [dn_for("frank")])
    assert flappers[dn_for("frank")] >= 1


def test_known_flappers_ignores_an_unbroken_history(sample_db):
    assert analysis.get_known_flappers(sample_db, [dn_for("alice")]) == {}


def test_known_flappers_ignores_a_single_appearance(sample_db):
    assert analysis.get_known_flappers(sample_db, [dn_for("dave")]) == {}


def test_known_flappers_with_no_input(conn):
    assert analysis.get_known_flappers(conn, []) == {}
    assert analysis.get_known_flappers(conn, set()) == {}


def test_known_flappers_handles_an_unknown_dn(sample_db):
    assert analysis.get_known_flappers(sample_db, ["CN=Ghost"]) == {}


def test_flap_suppression_and_leavers_are_independent(sample_db, today):
    """Attribute flapping and presence flapping answer different questions."""
    index = analysis.FlapIndex.load(sample_db, today)
    # Frank has a presence gap but no attribute-change history.
    assert not index.is_flapping(dn_for("frank"), ATTR_DEPT, "Platform")
    assert analysis.get_known_flappers(sample_db, [dn_for("frank")])


def test_changes_window_covers_country_and_city_separately(
    sample_db, today, current_state
):
    mods, _ = analysis.get_changes_window(
        sample_db, today, 1, [ATTR_COUNTRY, ATTR_CITY], current_state
    )
    carol = next(m for m in mods if m["name"] == "Carol Chen")
    assert {c["attr"] for c in carol["changes"]} == {ATTR_COUNTRY, ATTR_CITY}


# ── Windowed joiners ──────────────────────────────────────────────


def _three_day_history(conn):
    """d0={A}, d1={A,B}, d2={A,B,C}; returns day-2 state."""
    from ltm import db as db_module

    days = {
        "2026-06-01": ["A"],
        "2026-06-02": ["A", "B"],
        "2026-06-03": ["A", "B", "C"],
    }
    with db_module.write_transaction(conn):
        for day, people in days.items():
            write_snapshot(
                conn,
                day,
                {f"CN={p}": make_record(p.lower(), f"{p} Person") for p in people},
            )
    return {
        f"CN={p}": make_record(p.lower(), f"{p} Person") for p in days["2026-06-03"]
    }


def test_joiners_with_a_one_day_window_match_the_daily_diff(conn):
    state = _three_day_history(conn)
    joined = analysis.get_joiners_window(conn, state, "2026-06-03", 1)
    assert {records_name(r) for r in joined} == {"C Person"}


def test_joiners_with_a_longer_window_reach_back_to_the_baseline(conn):
    """The bug this fixes: a 2-day heading must cover 2 days of arrivals."""
    state = _three_day_history(conn)
    joined = analysis.get_joiners_window(conn, state, "2026-06-03", 2)
    assert {records_name(r) for r in joined} == {"B Person", "C Person"}


def test_joiners_without_a_pre_window_baseline_are_everyone(conn):
    """First run, or a window spanning the whole history: everyone arrived."""
    state = _three_day_history(conn)
    joined = analysis.get_joiners_window(conn, state, "2026-06-03", 30)
    assert len(joined) == 3


def test_joiners_on_an_empty_database_are_everyone(conn):
    state = {"CN=A": make_record("a", "A Person")}
    joined = analysis.get_joiners_window(conn, state, "2026-06-03", 1)
    assert joined == [state["CN=A"]]


def records_name(record):
    from ltm import records as records_module

    return records_module.display_name(record)


# ── Schedule-independent flapper detection ────────────────────────


def test_a_skipped_run_day_is_not_a_flap(conn):
    """A weekend or missed cron day is a gap for everyone — evidence of
    nothing. The old calendar heuristic flagged every DN on a Mon-Fri
    schedule eventually."""
    from ltm import db as db_module

    with db_module.write_transaction(conn):
        # Runs on Fri and Mon; nothing ran in between. DN present both days.
        write_snapshot(conn, "2026-06-05", {"CN=A": make_record("a", "A Person")})
        write_snapshot(conn, "2026-06-08", {"CN=A": make_record("a", "A Person")})
    assert analysis.get_known_flappers(conn, ["CN=A"]) == {}


def test_absence_from_a_run_that_happened_is_a_flap(conn):
    from ltm import db as db_module

    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-06-01", {"CN=A": make_record("a", "A Person")})
        write_snapshot(conn, "2026-06-02", {"CN=B": make_record("b", "B Person")})
        write_snapshot(conn, "2026-06-03", {"CN=A": make_record("a", "A Person")})
    assert analysis.get_known_flappers(conn, ["CN=A"]) == {"CN=A": 1}
