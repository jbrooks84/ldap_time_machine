"""Tests for the highlights tile data functions."""

import json
import sqlite3
from datetime import datetime

import pytest

from ltm import db as db_module
from ltm import highlights
from ltm.config import ATTR

from .conftest import ATTR_COUNTRY, ATTR_CREATED, ATTR_DISPLAY, ATTR_OFFICE

TODAY = "2026-05-03"
PAST = "2026-04-03"


@pytest.fixture
def movers_db(db_path):
    """A two-snapshot database with a clear country and office movement.

    Australia drops 80 -> 50 while the United States holds steady, so the
    country tile has an unambiguous winner.
    """
    conn = db_module.init_db(db_path=db_path)
    rows = []
    for index in range(80):
        rows.append(
            (
                PAST,
                f"au{index}",
                json.dumps({ATTR_COUNTRY: "Australia", ATTR_OFFICE: "Sydney"}),
                "",
            )
        )
    for index in range(50):
        rows.append(
            (
                TODAY,
                f"au{index}",
                json.dumps({ATTR_COUNTRY: "Australia", ATTR_OFFICE: "Sydney"}),
                "",
            )
        )
    for index in range(100):
        payload = json.dumps({ATTR_COUNTRY: "United States", ATTR_OFFICE: "Austin"})
        rows.append((PAST, f"us{index}", payload, ""))
        rows.append((TODAY, f"us{index}", payload, ""))
    with db_module.write_transaction(conn):
        conn.executemany("INSERT INTO user_history VALUES (?, ?, ?, ?)", rows)
    yield conn
    conn.close()


@pytest.fixture
def two_office_db(db_path):
    """Two offices moving by different absolute amounts but similar percentages."""
    conn = db_module.init_db(db_path=db_path)
    rows = []
    for date, count, office in (
        (PAST, 80, "Sydney"),
        (TODAY, 50, "Sydney"),
        (PAST, 50, "Melbourne"),
        (TODAY, 30, "Melbourne"),
    ):
        for index in range(count):
            rows.append(
                (
                    date,
                    f"{office}{index}",
                    json.dumps({ATTR_COUNTRY: "Australia", ATTR_OFFICE: office}),
                    "",
                )
            )
    with db_module.write_transaction(conn):
        conn.executemany("INSERT INTO user_history VALUES (?, ?, ?, ?)", rows)
    yield conn
    conn.close()


# ── Longest-tenured leaver ────────────────────────────────────────


def test_picks_the_earliest_start_date():
    leavers = [
        {ATTR_DISPLAY: "Alice", ATTR_COUNTRY: "USA", ATTR_CREATED: "20200101000000.0Z"},
        {ATTR_DISPLAY: "Bob", ATTR_COUNTRY: "UK", ATTR_CREATED: "20100601000000.0Z"},
    ]
    result = highlights.longest_tenured_leaver(leavers, datetime(2026, 5, 4))
    assert result["name"] == "Bob"
    assert result["country"] == "UK"
    assert result["count"] == 2
    assert "y" in result["tenure"]


def test_is_none_without_leavers():
    assert highlights.longest_tenured_leaver([], datetime(2026, 5, 4)) is None


def test_skips_leavers_with_no_parseable_date():
    leavers = [
        {ATTR_DISPLAY: "Alice", ATTR_CREATED: "garbage"},
        {ATTR_DISPLAY: "Bob", ATTR_CREATED: "20150101000000.0Z"},
    ]
    result = highlights.longest_tenured_leaver(leavers, datetime(2026, 5, 4))
    assert result["name"] == "Bob"


def test_is_none_when_no_leaver_has_a_date():
    leavers = [{ATTR_DISPLAY: "Alice", ATTR_CREATED: ""}]
    assert highlights.longest_tenured_leaver(leavers, datetime(2026, 5, 4)) is None


# ── Counts and movers ─────────────────────────────────────────────


def test_counts_for_date_excludes_empty_values(movers_db):
    counts = highlights._counts_for_date(movers_db, TODAY, ATTR_COUNTRY)
    assert counts == {"Australia": 50, "United States": 100}


def test_counts_for_date_rejects_a_disallowed_attribute(movers_db):
    """The allowlist guards the one query that interpolates an attribute name."""
    with pytest.raises(ValueError, match="Disallowed attribute"):
        highlights._counts_for_date(movers_db, TODAY, ATTR.job_title)


def test_counts_for_date_skips_null_and_blank(db_path):
    conn = db_module.init_db(db_path=db_path)
    try:
        with db_module.write_transaction(conn):
            conn.executemany(
                "INSERT INTO user_history VALUES (?, ?, ?, ?)",
                [
                    (TODAY, "a", json.dumps({ATTR_COUNTRY: "Japan"}), ""),
                    (TODAY, "b", json.dumps({ATTR_COUNTRY: ""}), ""),
                    (TODAY, "c", json.dumps({}), ""),
                ],
            )
        assert highlights._counts_for_date(conn, TODAY, ATTR_COUNTRY) == {"Japan": 1}
    finally:
        conn.close()


def test_biggest_mover_ranks_by_absolute_percentage():
    now = {"A": 50, "B": 110}
    past = {"A": 100, "B": 100}
    moves = highlights._biggest_mover(now, past, min_baseline=10)
    assert moves[0][0] == "A"  # -50% outranks +10%


def test_biggest_mover_applies_the_baseline_floor():
    """A group of 1 growing to 2 is +100% and must not win the tile."""
    moves = highlights._biggest_mover(
        {"tiny": 2, "big": 110}, {"tiny": 1, "big": 100}, min_baseline=10
    )
    assert [m[0] for m in moves] == ["big"]


def test_biggest_mover_honours_the_exclude_set():
    moves = highlights._biggest_mover(
        {"A": 50, "B": 60}, {"A": 100, "B": 100}, min_baseline=10, exclude={"A"}
    )
    assert [m[0] for m in moves] == ["B"]


def test_country_mover_finds_the_drop(movers_db):
    result = highlights.biggest_country_mover(
        movers_db, TODAY, days=30, min_baseline=30
    )
    assert result["country"] == "Australia"
    assert (result["past"], result["now"]) == (80, 50)
    assert result["pct"] < -30
    assert result["days_label"] == "30d"


def test_country_mover_is_none_when_the_role_is_unmapped(movers_db, monkeypatch):
    monkeypatch.setattr(highlights.ATTR, "_map", dict(ATTR.as_dict(), country=None))
    assert highlights.biggest_country_mover(movers_db, TODAY) is None


def test_country_mover_is_none_when_nothing_clears_the_baseline(movers_db):
    assert highlights.biggest_country_mover(movers_db, TODAY, min_baseline=10_000) is (
        None
    )


def test_office_mover_skips_a_collision_with_the_country_tile(movers_db):
    """Sydney's (80, 50) mirrors Australia's; showing both says nothing new."""
    result = highlights.biggest_office_mover(
        movers_db, TODAY, days=30, min_baseline=20, exclude_country_tuple=(80, 50)
    )
    assert result is None or result["office"] != "Sydney"


def test_office_mover_is_none_when_the_only_office_collides(db_path):
    """With nothing to fall through to, the tile is blank rather than redundant."""
    conn = db_module.init_db(db_path=db_path)
    try:
        rows = []
        for date, count in ((PAST, 80), (TODAY, 50)):
            for index in range(count):
                rows.append(
                    (
                        date,
                        f"syd{index}",
                        json.dumps({ATTR_COUNTRY: "Australia", ATTR_OFFICE: "Sydney"}),
                        "",
                    )
                )
        with db_module.write_transaction(conn):
            conn.executemany("INSERT INTO user_history VALUES (?, ?, ?, ?)", rows)
        result = highlights.biggest_office_mover(
            conn, TODAY, days=30, min_baseline=20, exclude_country_tuple=(80, 50)
        )
        assert result is None
    finally:
        conn.close()


def test_office_mover_falls_through_to_the_runner_up(two_office_db):
    result = highlights.biggest_office_mover(
        two_office_db, TODAY, days=30, min_baseline=20, exclude_country_tuple=(80, 50)
    )
    assert result["office"] == "Melbourne"
    assert (result["past"], result["now"]) == (50, 30)


def test_office_mover_returns_the_top_mover_without_a_collision(two_office_db):
    result = highlights.biggest_office_mover(
        two_office_db, TODAY, days=30, min_baseline=20
    )
    assert result["office"] in {"Sydney", "Melbourne"}


def test_office_mover_is_none_when_the_role_is_unmapped(movers_db, monkeypatch):
    monkeypatch.setattr(highlights.ATTR, "_map", dict(ATTR.as_dict(), office=None))
    assert highlights.biggest_office_mover(movers_db, TODAY) is None


def test_office_mover_ignores_unknown_as_an_office(db_path):
    conn = db_module.init_db(db_path=db_path)
    try:
        rows = []
        for date, count in ((PAST, 100), (TODAY, 20)):
            for index in range(count):
                rows.append(
                    (date, f"u{index}", json.dumps({ATTR_OFFICE: "Unknown"}), "")
                )
        with db_module.write_transaction(conn):
            conn.executemany("INSERT INTO user_history VALUES (?, ?, ?, ?)", rows)
        assert highlights.biggest_office_mover(conn, TODAY, min_baseline=20) is None
    finally:
        conn.close()


# ── Baseline resolution ───────────────────────────────────────────


def test_baseline_falls_back_to_the_first_run_and_says_so(movers_db):
    """A short history must be labelled honestly, not passed off as 30 days."""
    date, label = highlights._baseline_date(movers_db, TODAY, days=3650)
    assert date == PAST
    assert label == "since first run"


def test_baseline_is_none_with_only_one_snapshot(db_path):
    conn = db_module.init_db(db_path=db_path)
    try:
        with db_module.write_transaction(conn):
            conn.execute("INSERT INTO user_history VALUES (?, 'a', '{}', '')", (TODAY,))
        assert highlights._baseline_date(conn, TODAY, days=30) == (None, None)
    finally:
        conn.close()


def test_baseline_is_none_on_an_empty_database(conn):
    assert highlights._baseline_date(conn, TODAY, days=30) == (None, None)


def test_latest_run_on_or_before_returns_none_when_empty(conn):
    assert highlights._latest_run_on_or_before(conn, TODAY) is None


# ── Headcount deltas ──────────────────────────────────────────────


def test_headcount_deltas_reports_the_30_day_change(movers_db):
    result = highlights.headcount_deltas(movers_db, TODAY)
    assert result["now"] == 150
    assert result["delta_30"] == -30


def test_headcount_deltas_is_none_without_a_90_day_baseline(movers_db):
    assert highlights.headcount_deltas(movers_db, TODAY)["delta_90"] is None


def test_headcount_deltas_on_an_empty_database(conn):
    result = highlights.headcount_deltas(conn, TODAY)
    assert result == {"now": 0, "delta_30": None, "delta_90": None}


def test_counts_for_date_works_on_an_in_memory_database():
    memory = sqlite3.connect(":memory:")
    try:
        memory.execute(
            "CREATE TABLE user_history (run_date DATE, dn TEXT, data JSON, "
            "data_hash TEXT)"
        )
        assert highlights._counts_for_date(memory, TODAY, ATTR_COUNTRY) == {}
    finally:
        memory.close()
