"""Tests for the SQLite access layer.

Beyond correctness, a few of these assert on *how* queries run — the pragmas
that are set and the fact that DN-only reads are served from the index. Those
properties are the difference between a report that takes seconds and one that
takes minutes once the database holds a few years of history, and nothing else
would notice if they silently regressed.
"""

import json
import os
import sqlite3

import pytest

from ltm import db as db_module

from .conftest import (
    ATTR_COUNTRY,
    ATTR_DEPT,
    FailingConnection,
    dn_for,
    make_record,
    write_snapshot,
)

# ── Connection setup ──────────────────────────────────────────────


def test_connection_sets_the_performance_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert conn.execute("PRAGMA mmap_size").fetchone()[0] == db_module.MMAP_SIZE_BYTES
    assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2  # MEMORY
    assert (
        conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        == db_module.JOURNAL_SIZE_LIMIT_BYTES
    )


def test_connection_is_in_autocommit_mode(conn):
    """BEGIN IMMEDIATE requires that the driver not open its own transaction."""
    assert conn.isolation_level is None


def test_init_db_creates_tables_and_indexes(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"user_history", "changes"} <= tables

    indexes = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert "changes_unique" in indexes
    assert "user_history_dn_run_date" in indexes
    assert "changes_dn_attribute_run_date" in indexes


def test_init_db_is_idempotent(db_path):
    first = db_module.init_db(db_path=db_path)
    first.close()
    second = db_module.init_db(db_path=db_path)
    second.close()  # Must not raise on the second call.


def test_init_db_sets_restrictive_permissions(db_path):
    connection = db_module.init_db(db_path=db_path)
    connection.close()
    assert oct(os.stat(db_path).st_mode)[-3:] == "600"


def test_init_db_survives_a_failing_chmod(db_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(db_module.os, "chmod", boom)
    connection = db_module.init_db(db_path=db_path)
    connection.close()  # Logged and ignored, not fatal.


def test_connect_db_defaults_to_the_configured_path(monkeypatch, db_path):
    monkeypatch.setattr(db_module, "DB_FILE", db_path)
    connection = db_module.connect_db()
    try:
        # realpath because macOS reports /var as /private/var.
        assert os.path.realpath(db_module._database_path(connection)) == (
            os.path.realpath(db_path)
        )
    finally:
        connection.close()


# ── Legacy migration ──────────────────────────────────────────────


def test_init_db_dedupes_a_legacy_changes_table(db_path):
    """A database predating changes_unique must migrate, not crash."""
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        "CREATE TABLE changes (run_date DATE, dn TEXT, attribute TEXT, "
        "old_val TEXT, new_val TEXT)"
    )
    row = ("2026-01-01", "CN=A", "title", "X", "Y")
    legacy.executemany("INSERT INTO changes VALUES (?, ?, ?, ?, ?)", [row, row, row])
    legacy.commit()
    legacy.close()

    connection = db_module.init_db(db_path=db_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1
    finally:
        connection.close()


def test_dedupe_is_a_no_op_on_an_empty_table(conn):
    db_module._dedupe_changes_table(conn.cursor())
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0


def test_dedupe_is_a_no_op_when_there_are_no_duplicates(conn):
    conn.execute("INSERT INTO changes VALUES ('2026-01-01', 'CN=A', 'title', 'X', 'Y')")
    db_module._dedupe_changes_table(conn.cursor())
    assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 1


# ── Write transactions ────────────────────────────────────────────


def test_write_transaction_commits_on_success(conn):
    with db_module.write_transaction(conn):
        conn.execute("INSERT INTO user_history VALUES ('2026-01-01', 'CN=A', '{}', '')")
    assert conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 1


def test_write_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError), db_module.write_transaction(conn):
        conn.execute("INSERT INTO user_history VALUES ('2026-01-01', 'CN=A', '{}', '')")
        raise RuntimeError("mid-transaction failure")
    # The partial write must not survive.
    assert conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 0


def test_write_transaction_takes_the_write_lock_immediately(conn, db_path):
    """BEGIN IMMEDIATE must lock at the start, not on the first write."""
    other = sqlite3.connect(db_path, isolation_level=None)
    try:
        with db_module.write_transaction(conn), pytest.raises(sqlite3.OperationalError):
            other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


# ── Snapshot reads ────────────────────────────────────────────────


def test_get_snapshot_state_returns_parsed_records(sample_db, today):
    state = db_module.get_snapshot_state(sample_db, today)
    assert dn_for("alice") in state
    assert state[dn_for("alice")][ATTR_DEPT] == "Core Platform"


def test_get_snapshot_state_is_empty_for_an_unknown_date(sample_db):
    assert db_module.get_snapshot_state(sample_db, "1999-01-01") == {}


def test_get_snapshot_dns_matches_the_full_snapshot_keys(sample_db, today):
    dns = db_module.get_snapshot_dns(sample_db, today)
    assert dns == set(db_module.get_snapshot_state(sample_db, today))


def test_get_snapshot_dns_is_served_entirely_from_the_index(sample_db, today):
    """The DN-only read must never touch the table — that is the whole point."""
    plan = sample_db.execute(
        "EXPLAIN QUERY PLAN SELECT dn FROM user_history WHERE run_date = ?", (today,)
    ).fetchall()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "COVERING INDEX" in detail.upper()


def test_get_records_for_dns_fetches_only_what_was_asked_for(sample_db, today):
    wanted = {dn_for("alice"), dn_for("bob")}
    got = db_module.get_records_for_dns(sample_db, today, wanted)
    assert set(got) == wanted


def test_get_records_for_dns_handles_an_empty_request(sample_db, today):
    assert db_module.get_records_for_dns(sample_db, today, []) == {}


def test_get_records_for_dns_chunks_past_the_parameter_limit(conn):
    """More DNs than SQLite allows bound parameters must still work."""
    count = db_module._PARAM_CHUNK * 2 + 7
    records = {f"CN={i}": make_record(f"u{i}", f"User {i}") for i in range(count)}
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", records)
    got = db_module.get_records_for_dns(conn, "2026-01-01", list(records))
    assert len(got) == count


def test_get_run_dates_for_dns_returns_ascending_dates(sample_db):
    dates = db_module.get_run_dates_for_dns(sample_db, [dn_for("alice")])
    assert dates[dn_for("alice")] == sorted(dates[dn_for("alice")])
    assert len(dates[dn_for("alice")]) == 40


def test_get_run_dates_for_dns_handles_an_empty_request(sample_db):
    assert db_module.get_run_dates_for_dns(sample_db, []) == {}


def test_get_run_dates_for_dns_chunks_past_the_parameter_limit(conn):
    count = db_module._PARAM_CHUNK + 5
    records = {f"CN={i}": make_record(f"u{i}", f"User {i}") for i in range(count)}
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", records)
    dates = db_module.get_run_dates_for_dns(conn, list(records))
    assert len(dates) == count


# ── Date helpers ──────────────────────────────────────────────────


def test_get_latest_run_date(sample_db, today):
    assert db_module.get_latest_run_date(sample_db) == today


def test_get_latest_run_date_is_none_when_empty(conn):
    assert db_module.get_latest_run_date(conn) is None


def test_get_window_run_dates_covers_an_inclusive_range(sample_db, today):
    dates = db_module.get_window_run_dates(sample_db, today, 7)
    assert len(dates) == 7
    assert dates[-1] == today
    assert dates == sorted(dates)


def test_get_window_run_dates_for_a_single_day(sample_db, today):
    assert db_module.get_window_run_dates(sample_db, today, 1) == [today]


def test_get_window_run_dates_is_empty_outside_the_history(sample_db):
    assert db_module.get_window_run_dates(sample_db, "1999-01-01", 7) == []


def test_get_previous_run_date(sample_db, today):
    previous = db_module.get_previous_run_date(sample_db, today)
    assert previous is not None and previous < today


def test_get_previous_run_date_is_none_before_all_history(sample_db):
    assert db_module.get_previous_run_date(sample_db, "1999-01-01") is None


def test_get_run_date_counts_is_ascending_and_complete(sample_db):
    counts = db_module.get_run_date_counts(sample_db)
    assert len(counts) == 40
    assert [row[0] for row in counts] == sorted(row[0] for row in counts)
    assert all(count > 0 for _date, count in counts)


def test_nearest_run_date_finds_the_closest_earlier_snapshot(sample_db, today):
    assert db_module._nearest_run_date(sample_db, "2099-01-01") == today
    assert db_module._nearest_run_date(sample_db, "1999-01-01") is None


# ── Attribute counts ──────────────────────────────────────────────


def test_get_attribute_counts_returns_one_dict_per_attribute(sample_db, today):
    counts = db_module.get_attribute_counts(sample_db, today, [ATTR_COUNTRY, ATTR_DEPT])
    assert set(counts) == {ATTR_COUNTRY, ATTR_DEPT}
    assert counts[ATTR_COUNTRY]["United States"] > 0
    assert counts[ATTR_DEPT]["Core Platform"] == 4


def test_get_attribute_counts_totals_match_the_row_count(sample_db, today):
    counts = db_module.get_attribute_counts(sample_db, today, [ATTR_COUNTRY])
    total_rows = len(db_module.get_snapshot_dns(sample_db, today))
    assert sum(counts[ATTR_COUNTRY].values()) == total_rows


def test_get_attribute_counts_dedupes_repeated_attributes(sample_db, today):
    counts = db_module.get_attribute_counts(
        sample_db, today, [ATTR_COUNTRY, ATTR_COUNTRY]
    )
    assert set(counts) == {ATTR_COUNTRY}


def test_get_attribute_counts_with_no_attributes(sample_db, today):
    assert db_module.get_attribute_counts(sample_db, today, []) == {}


def test_get_attribute_counts_is_none_without_a_snapshot(sample_db):
    counts = db_module.get_attribute_counts(sample_db, "1999-01-01", [ATTR_COUNTRY])
    assert counts[ATTR_COUNTRY] is None


def test_get_attribute_counts_uses_the_nearest_earlier_snapshot(sample_db):
    """A target date between runs resolves backwards, never forwards."""
    counts = db_module.get_attribute_counts(sample_db, "2099-01-01", [ATTR_COUNTRY])
    assert counts[ATTR_COUNTRY]["United States"] > 0


def test_get_attribute_counts_suppresses_a_mostly_unknown_attribute(conn):
    """An attribute not yet collected must not render as a cliff in a trend."""
    records = {}
    for i in range(20):
        record = make_record(f"u{i}", f"User {i}")
        if i > 0:  # 19 of 20 lack the attribute entirely
            del record[ATTR_DEPT]
        records[f"CN={i}"] = record
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", records)

    counts = db_module.get_attribute_counts(conn, "2026-01-01", [ATTR_DEPT])
    assert counts[ATTR_DEPT] is None


def test_get_attribute_counts_keeps_a_partially_unknown_attribute(conn):
    records = {}
    for i in range(20):
        record = make_record(f"u{i}", f"User {i}")
        if i < 5:
            del record[ATTR_DEPT]
        records[f"CN={i}"] = record
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", records)

    counts = db_module.get_attribute_counts(conn, "2026-01-01", [ATTR_DEPT])
    assert counts[ATTR_DEPT]["Platform"] == 15
    assert counts[ATTR_DEPT]["Unknown"] == 5


def test_get_attribute_counts_returns_none_on_a_database_error(today):
    counts = db_module.get_attribute_counts(FailingConnection(), today, [ATTR_COUNTRY])
    assert counts[ATTR_COUNTRY] is None


# ── Run summary ───────────────────────────────────────────────────


def test_run_summary_table_exists(conn):
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "run_summary" in tables


def test_record_run_summary_stores_and_replaces(conn):
    db_module.record_run_summary(conn, "2026-01-01", 100)
    db_module.record_run_summary(conn, "2026-01-01", 120)  # a same-day re-run
    rows = conn.execute("SELECT run_date, record_count FROM run_summary").fetchall()
    assert rows == [("2026-01-01", 120)]


def test_trend_counts_come_from_the_summary_not_a_scan(conn):
    """The graph must read the maintained table, not aggregate history."""
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", {"CN=A": {}, "CN=B": {}})
        db_module.record_run_summary(conn, "2026-01-01", 2)
    assert db_module.get_run_date_counts(conn) == [("2026-01-01", 2)]

    # Deliberately disagree with history: proving the value is read, not derived.
    db_module.record_run_summary(conn, "2026-01-01", 999)
    assert db_module.get_run_date_counts(conn) == [("2026-01-01", 999)]


def test_backfill_populates_a_database_that_predates_the_summary(conn, caplog):
    """Rows loaded without a summary — an upgrade, or a direct import."""
    with db_module.write_transaction(conn):
        write_snapshot(
            conn,
            "2026-01-01",
            {"CN=A": {}, "CN=B": {}, "CN=C": {}},
            update_summary=False,
        )
        write_snapshot(
            conn, "2026-01-02", {"CN=A": {}, "CN=B": {}}, update_summary=False
        )
    assert conn.execute("SELECT COUNT(*) FROM run_summary").fetchone()[0] == 0

    with caplog.at_level("INFO"):
        written = db_module.backfill_run_summary(conn)
    assert written == 2
    assert "one-time scan" in caplog.text
    assert db_module.get_run_date_counts(conn) == [
        ("2026-01-01", 3),
        ("2026-01-02", 2),
    ]


def test_backfill_is_a_no_op_once_in_sync(conn):
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", {"CN=A": {}})
        db_module.record_run_summary(conn, "2026-01-01", 1)
    assert db_module.backfill_run_summary(conn) == 0


def test_backfill_repairs_a_partially_populated_summary(conn):
    """A summary covering only some dates must be completed, not trusted."""
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", {"CN=A": {}, "CN=B": {}})
        write_snapshot(conn, "2026-01-02", {"CN=A": {}}, update_summary=False)
    db_module.backfill_run_summary(conn)
    assert db_module.get_run_date_counts(conn) == [
        ("2026-01-01", 2),
        ("2026-01-02", 1),
    ]


def test_backfill_on_an_empty_database(conn):
    assert db_module.backfill_run_summary(conn) == 0
    assert db_module.get_run_date_counts(conn) == []


# ── Maintenance and health ────────────────────────────────────────


def test_optimize_runs_without_error(sample_db):
    db_module.optimize(sample_db)


def test_optimize_swallows_database_errors():
    db_module.optimize(FailingConnection())  # Housekeeping must never fail a run.


def test_log_database_health_reports_the_key_metrics(sample_db, caplog):
    with caplog.at_level("INFO"):
        db_module.log_database_health(sample_db)
    message = caplog.text
    assert "Database health" in message
    assert "run_dates=40" in message


def test_log_database_health_warns_on_a_large_freelist(conn, caplog):
    """A real freelist, made the way production makes one: bulk insert, delete."""
    records = {f"CN={i}": make_record(f"u{i}", f"User {i}") for i in range(4000)}
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", records)
    with db_module.write_transaction(conn):
        conn.execute("DELETE FROM user_history")

    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    assert freelist / pages > db_module._FREELIST_VACUUM_RATIO

    with caplog.at_level("WARNING"):
        db_module.log_database_health(conn)
    assert "VACUUM" in caplog.text


def test_log_database_health_warns_on_a_large_wal(sample_db, caplog, monkeypatch):
    monkeypatch.setattr(
        db_module, "_wal_path_size", lambda _conn: db_module._WAL_CRIT_BYTES + 1
    )
    with caplog.at_level("WARNING"):
        db_module.log_database_health(sample_db)
    assert "checkpoints are not completing" in caplog.text


def test_log_database_health_warns_at_the_lower_wal_threshold(
    sample_db, caplog, monkeypatch
):
    monkeypatch.setattr(
        db_module, "_wal_path_size", lambda _conn: db_module._WAL_WARN_BYTES + 1
    )
    with caplog.at_level("WARNING"):
        db_module.log_database_health(sample_db)
    assert "and growing" in caplog.text


def test_log_database_health_never_raises(caplog):
    with caplog.at_level("WARNING"):
        db_module.log_database_health(FailingConnection())
    assert "Failed to log database health" in caplog.text


def test_log_database_health_handles_an_empty_database(conn, caplog):
    with caplog.at_level("INFO"):
        db_module.log_database_health(conn)
    assert "Database health" in caplog.text


def test_wal_path_size_is_zero_when_there_is_no_sidecar(conn):
    assert db_module._wal_path_size(conn) >= 0


def test_wal_path_size_returns_zero_on_error():
    assert db_module._wal_path_size(FailingConnection()) == 0


def test_wal_path_size_is_zero_for_an_in_memory_database():
    memory = sqlite3.connect(":memory:")
    try:
        assert db_module._wal_path_size(memory) == 0
    finally:
        memory.close()


def test_database_path_falls_back_when_the_pragma_fails():
    assert db_module._database_path(FailingConnection()) == db_module.DB_FILE


def test_wal_path_size_reports_a_real_sidecar(conn, db_path):
    """WAL mode writes a sidecar; the health log must be able to size it."""
    with db_module.write_transaction(conn):
        conn.execute("INSERT INTO user_history VALUES ('2026-01-01', 'CN=A', '{}', '')")
    assert os.path.exists(db_path + "-wal")
    assert db_module._wal_path_size(conn) > 0


def test_stored_json_round_trips(conn):
    record = make_record("alice", "Alice Adams")
    with db_module.write_transaction(conn):
        write_snapshot(conn, "2026-01-01", {dn_for("alice"): record})
    stored = conn.execute(
        "SELECT data FROM user_history WHERE dn = ?", (dn_for("alice"),)
    ).fetchone()[0]
    assert json.loads(stored) == record


# ── In-memory attribute counts ────────────────────────────────────


def test_counts_from_records_matches_the_database_for_the_same_day(
    sample_db, today, current_state
):
    """The two count paths must be interchangeable, or the report would show
    different numbers depending on which one a section happened to use."""
    from ltm.config import ATTR

    attributes = [ATTR.country, ATTR.department, ATTR.job_title]
    assert db_module.counts_from_records(
        current_state.values(), attributes
    ) == db_module.get_attribute_counts(sample_db, today, attributes)


def test_counts_from_records_counts_and_substitutes_unknown():
    records = [{"co": "US"}, {"co": "US"}, {"co": "JP"}, {}, {"co": ""}]
    out = db_module.counts_from_records(records, ["co", "co"])
    assert out == {"co": {"US": 2, "JP": 1, "Unknown": 2}}


def test_counts_from_records_suppresses_a_mostly_unknown_attribute():
    records = [{"co": "US"}] + [{} for _ in range(19)]  # 95% unknown
    assert db_module.counts_from_records(records, ["co"])["co"] is None


def test_counts_from_records_edge_inputs():
    assert db_module.counts_from_records([{"co": "US"}], []) == {}
    # Zero records: nothing to suppress against, empty counts rather than None.
    assert db_module.counts_from_records([], ["co"]) == {"co": {}}


def test_counts_from_records_refuses_multi_valued_attributes():
    """The SQL path sees JSON array text where this path sees a list; the two
    would count different keys. Fail loudly instead of drifting quietly."""
    records = [{"memberOf": ["CN=A", "CN=B"]}]
    with pytest.raises(ValueError, match="multi-valued"):
        db_module.counts_from_records(records, ["memberOf"])
