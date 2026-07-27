"""End-to-end pipeline tests.

These run the real thing: a real SQLite database, a real ``ldapsearch``
subprocess (a fake binary on PATH emitting real LDIF), real LDIF parsing, real
diffing, and a real rendered HTML report written to disk. Only SMTP is stubbed.

That matters because most of this project's failure modes live in the seams —
LDIF that parses but loses a record, a diff that is right in isolation but
wrong against yesterday's snapshot, a report that renders from data the
pipeline never actually produces. Unit tests do not see those.
"""

import os
import stat
import sys

import pytest

from ltm import db as db_module
from ltm import ldap_client, pipeline, report

from .conftest import (
    ATTR_DEPT,
    ATTR_TITLE,
    dn_for,
    make_record,
)


def to_ldif(records):
    """Render {dn: record} as the LDIF an ldapsearch run would emit."""
    lines = ["# extended LDIF", "#"]
    for dn, record in records.items():
        lines.append(f"dn: {dn}")
        for key, value in record.items():
            for item in value if isinstance(value, list) else [value]:
                lines.append(f"{key}: {item}")
        lines.append("")
    lines.append("# search result")
    lines.append("search: 2")
    lines.append("result: 0 Success")
    return "\n".join(lines) + "\n"


@pytest.fixture
def directory(tmp_path, monkeypatch):
    """A controllable fake directory backed by a real ldapsearch subprocess.

    Returns a setter; call it with {dn: record} to define what the next fetch
    returns.
    """
    response = tmp_path / "response.ldif"
    response.write_text("", encoding="utf-8")

    script = tmp_path / "ldapsearch"
    script.write_text(f'#!/bin/sh\ncat "{response}"\nexit 0\n', encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    monkeypatch.setattr(
        ldap_client, "get_ldap_credentials", lambda: ("svc@example.com", "secret")
    )

    def set_contents(records):
        response.write_text(to_ldif(records), encoding="utf-8")

    return set_contents


@pytest.fixture
def run_pipeline(db_path, isolated_paths, monkeypatch):
    """Run the pipeline against a temporary database, never sending mail."""
    monkeypatch.setattr(report, "get_smtp_settings", lambda: _disabled_smtp())

    def run(send_email=False):
        pipeline._run_pipeline(db_path=db_path, send_email=send_email)
        return db_path

    return run


def _disabled_smtp():
    from ltm.secrets import DISABLED_SMTP

    return DISABLED_SMTP


def archived_report(isolated_paths):
    """Return the text of the most recently archived report."""
    files = sorted(isolated_paths["archive"].glob("*.html"), key=os.path.getmtime)
    assert files, "no report was archived"
    return files[-1].read_text(encoding="utf-8")


def population(count=12, **overrides):
    """Build a directory population of `count` people, named user00..userNN."""
    return {
        dn_for(f"user{i:02d}"): make_record(
            f"user{i:02d}", f"User {i:02d}", **overrides
        )
        for i in range(count)
    }


# ── A first run ───────────────────────────────────────────────────


def test_first_run_stores_a_snapshot(directory, run_pipeline, db_path, isolated_paths):
    directory(population(12))
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 12
        # Nothing to compare against yet, so no changes.
        assert conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0] == 0
    finally:
        conn.close()


def test_first_run_produces_a_readable_report(directory, run_pipeline, isolated_paths):
    directory(population(12))
    run_pipeline()
    content = archived_report(isolated_paths)
    assert content.strip().startswith("<html>")
    assert content.strip().endswith("</html>")
    assert "Active Members" in content


def test_first_run_writes_a_trend_graph(directory, run_pipeline, isolated_paths):
    directory(population(12))
    run_pipeline()
    graph = isolated_paths["root"] / "trend.png"
    assert graph.exists() and graph.stat().st_size > 0


# ── Detecting change between runs ─────────────────────────────────


def test_a_joiner_is_detected_and_reported(directory, run_pipeline, isolated_paths):
    people = population(12)
    directory(people)
    run_pipeline()

    people[dn_for("newbie")] = make_record("newbie", "Nina Newbie", title="Analyst")
    directory(people)
    run_pipeline()

    content = archived_report(isolated_paths)
    assert "New Joiners" in content
    assert "Nina Newbie" in content


def test_an_attribute_change_is_recorded_and_reported(
    directory, run_pipeline, db_path, isolated_paths
):
    people = population(12)
    directory(people)
    run_pipeline()

    people[dn_for("user00")][ATTR_TITLE] = "Principal Engineer"
    directory(people)
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        rows = conn.execute(
            "SELECT old_val, new_val FROM changes WHERE attribute = ?", (ATTR_TITLE,)
        ).fetchall()
    finally:
        conn.close()
    assert ("Engineer", "Principal Engineer") in rows

    content = archived_report(isolated_paths)
    assert "Title Changes" in content
    assert "Principal Engineer" in content


def test_a_departure_is_not_reported_immediately(
    directory, run_pipeline, isolated_paths
):
    """The confirmation window is the whole point: one absent day is not a leaver."""
    people = population(12)
    directory(people)
    run_pipeline()

    del people[dn_for("user00")]
    directory(people)
    run_pipeline()

    content = archived_report(isolated_paths)
    assert "❌ Leavers" not in content


def test_a_bulk_rename_is_rolled_up(directory, run_pipeline, isolated_paths):
    people = population(12)
    directory(people)
    run_pipeline()

    for index in range(6):
        people[dn_for(f"user{index:02d}")][ATTR_DEPT] = "Core Platform"
    directory(people)
    run_pipeline()

    content = archived_report(isolated_paths)
    assert "Bulk renames" in content
    assert "Core Platform" in content


def test_a_rerun_on_the_same_day_replaces_rather_than_duplicates(
    directory, run_pipeline, db_path
):
    directory(population(12))
    run_pipeline()
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 12
        assert (
            conn.execute(
                "SELECT COUNT(DISTINCT run_date) FROM user_history"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_the_run_records_its_own_headcount(directory, run_pipeline, db_path):
    """The trend graph depends on this being written with the snapshot."""
    from datetime import datetime

    directory(population(15))
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        rows = conn.execute("SELECT run_date, record_count FROM run_summary").fetchall()
        stored = conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0]
    finally:
        conn.close()
    today = datetime.now().strftime("%Y-%m-%d")
    assert rows == [(today, 15)]
    assert rows[0][1] == stored


def test_the_summary_follows_a_same_day_rerun(directory, run_pipeline, db_path):
    """A re-run replaces the day, and the recorded count must follow it.

    The second count stays within the result-count guardrail — a drop steep
    enough to trip that would abort before writing anything, which is a
    different behaviour with its own test.
    """
    directory(population(15))
    run_pipeline()
    directory(population(14))
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        counts = conn.execute("SELECT record_count FROM run_summary").fetchall()
        actual = conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0]
    finally:
        conn.close()
    assert counts == [(14,)]
    assert counts[0][0] == actual


# ── Guardrails ────────────────────────────────────────────────────


def test_an_empty_directory_response_aborts_the_run(
    directory, run_pipeline, db_path, caplog
):
    """Nothing is written when the fetch comes back empty."""
    directory({})
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exit_info:
        run_pipeline()
    assert exit_info.value.code == 1
    assert "no records returned" in caplog.text

    conn = db_module.connect_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 0
    finally:
        conn.close()


def test_a_truncated_response_aborts_and_preserves_history(
    directory, run_pipeline, db_path, caplog
):
    """A partial fetch must never overwrite a good snapshot."""
    directory(population(100))
    run_pipeline()

    directory(population(10))  # 10% of the previous count
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exit_info:
        run_pipeline()
    assert exit_info.value.code == 1
    assert "Aborting to avoid storing partial data" in caplog.text

    conn = db_module.connect_db(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 100
    finally:
        conn.close()


def test_a_small_decline_is_allowed_through(directory, run_pipeline, db_path):
    """95% of yesterday is normal attrition, not a truncated result."""
    directory(population(100))
    run_pipeline()
    directory(population(95))
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        counts = dict(
            conn.execute(
                "SELECT run_date, COUNT(*) FROM user_history GROUP BY run_date"
            ).fetchall()
        )
    finally:
        conn.close()
    assert 95 in counts.values()


def test_the_ratio_guard_can_be_disabled(directory, run_pipeline, monkeypatch):
    """A small directory legitimately swings by more than 10%."""
    monkeypatch.setattr(pipeline, "MIN_LDAP_RESULT_RATIO", 0)
    directory(population(100))
    run_pipeline()
    directory(population(5))
    run_pipeline()  # Must not raise.


# ── Flap suppression through the whole pipeline ───────────────────


def test_an_oscillating_attribute_is_suppressed(
    directory, run_pipeline, db_path, isolated_paths
):
    """A -> B on one run, B -> A on the next: the second is noise."""
    people = population(12)
    directory(people)
    run_pipeline()

    people[dn_for("user00")][ATTR_TITLE] = "Analyst"
    directory(people)
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        conn.execute("UPDATE changes SET run_date = date(run_date, '-1 day')")
    finally:
        conn.close()

    people[dn_for("user00")][ATTR_TITLE] = "Engineer"  # back where it started
    directory(people)
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        reverted = conn.execute(
            "SELECT COUNT(*) FROM changes WHERE new_val = 'Engineer'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert reverted == 0


def test_flap_suppression_can_be_disabled(
    directory, run_pipeline, monkeypatch, db_path
):
    monkeypatch.setattr(
        pipeline.FlapIndex, "load", lambda *a, **k: pipeline.FlapIndex()
    )
    people = population(12)
    directory(people)
    run_pipeline()
    people[dn_for("user00")][ATTR_TITLE] = "Analyst"
    directory(people)
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM changes WHERE new_val = 'Analyst'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


# ── Diffing in isolation ──────────────────────────────────────────


def test_diff_reports_a_changed_attribute():
    old = {"CN=A": {"title": "Engineer"}}
    new = {"CN=A": {"title": "Analyst"}}
    rows, mods, flaps = pipeline._diff_snapshots(
        new, old, pipeline.FlapIndex(), "2026-06-30"
    )
    assert rows == [("2026-06-30", "CN=A", "title", "Engineer", "Analyst")]
    assert flaps == 0
    assert mods[0]["changes"][0]["attr"] == "title"


def test_diff_ignores_list_reordering():
    """Multi-valued attribute order carries no meaning and must not alert."""
    old = {"CN=A": {"memberOf": ["b", "a"]}}
    new = {"CN=A": {"memberOf": ["a", "b"]}}
    rows, _mods, _flaps = pipeline._diff_snapshots(
        new, old, pipeline.FlapIndex(), "2026-06-30"
    )
    assert rows == []


def test_diff_records_an_appearing_attribute():
    old = {"CN=A": {}}
    new = {"CN=A": {"title": "Engineer"}}
    rows, _mods, _flaps = pipeline._diff_snapshots(
        new, old, pipeline.FlapIndex(), "2026-06-30"
    )
    assert rows == [("2026-06-30", "CN=A", "title", "N/A", "Engineer")]


def test_diff_counts_suppressed_flaps():
    index = pipeline.FlapIndex({("CN=A", "title"): {"Analyst"}})
    old = {"CN=A": {"title": "Engineer"}}
    new = {"CN=A": {"title": "Analyst"}}
    rows, mods, flaps = pipeline._diff_snapshots(new, old, index, "2026-06-30")
    assert rows == [] and mods == [] and flaps == 1


def test_diff_only_promotes_tracked_attributes_to_the_report():
    old = {"CN=A": {"description": "old"}}
    new = {"CN=A": {"description": "new"}}
    rows, mods, _flaps = pipeline._diff_snapshots(
        new, old, pipeline.FlapIndex(), "2026-06-30"
    )
    assert len(rows) == 1  # stored in history
    assert mods == []  # but not surfaced as a reportable change


# ── Baseline resolution ───────────────────────────────────────────


def test_baseline_prefers_yesterday(sample_db, today):
    from datetime import datetime, timedelta

    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )
    state, date = pipeline._resolve_baseline(sample_db, yesterday)
    assert date == yesterday and state


def test_baseline_falls_back_to_the_last_available_run(sample_db, caplog):
    """A missed run must not make everyone look like a joiner."""
    with caplog.at_level("INFO"):
        state, date = pipeline._resolve_baseline(sample_db, "2099-01-01")
    assert state and date != "2099-01-01"
    assert "using last available" in caplog.text


def test_baseline_is_empty_on_a_first_run(conn, caplog):
    with caplog.at_level("INFO"):
        state, _date = pipeline._resolve_baseline(conn, "2026-06-29")
    assert state == {}
    assert "First run" in caplog.text


# ── Locking ───────────────────────────────────────────────────────


def test_the_lock_prevents_a_concurrent_run(isolated_paths, caplog):
    first = pipeline._acquire_run_lock()
    try:
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exit_info:
            pipeline._acquire_run_lock()
        assert exit_info.value.code == pipeline.EXIT_ALREADY_RUNNING
        assert "already active" in caplog.text
    finally:
        pipeline._release_run_lock(first)


def test_the_lock_records_the_owning_pid(isolated_paths):
    lock_fd = pipeline._acquire_run_lock()
    try:
        with open(pipeline.LOCK_FILE, encoding="ascii") as handle:
            assert handle.read().strip() == str(os.getpid())
    finally:
        pipeline._release_run_lock(lock_fd)


def test_the_lock_is_reusable_after_release(isolated_paths):
    pipeline._release_run_lock(pipeline._acquire_run_lock())
    pipeline._release_run_lock(pipeline._acquire_run_lock())


def test_run_releases_the_lock_even_when_the_pipeline_fails(
    isolated_paths, monkeypatch
):
    """A crash must not leave a lock that blocks every future run."""
    monkeypatch.setattr(pipeline, "setup_logging", lambda *a, **k: None)

    def boom(*_args, **_kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(pipeline, "_run_pipeline", boom)
    with pytest.raises(RuntimeError):
        pipeline.run()

    # Proves the lock was released: this would exit(75) otherwise.
    pipeline._release_run_lock(pipeline._acquire_run_lock())


# ── Logging helpers ───────────────────────────────────────────────


def test_duration_formatting_stays_legible_below_a_second():
    """ "in 0.0s" reads like the step never ran; report milliseconds instead."""
    assert pipeline.format_duration(0.048) == "48ms"
    assert pipeline.format_duration(0.0004) == "0ms"
    assert pipeline.format_duration(0.999) == "999ms"


def test_duration_formatting_uses_seconds_above_one():
    assert pipeline.format_duration(1.0) == "1.0s"
    assert pipeline.format_duration(72.5) == "72.5s"
    assert pipeline.format_duration(3600.0) == "3600.0s"


def test_record_list_logging_caps_the_output(caplog):
    users = [make_record(f"u{i}", f"User {i}") for i in range(25)]
    with caplog.at_level("INFO"):
        pipeline._log_record_list("JOINERS", users, "+", limit=5)
    assert "... and 20 more" in caplog.text


def test_record_list_logging_is_silent_when_empty(caplog):
    with caplog.at_level("INFO"):
        pipeline._log_record_list("JOINERS", [], "+")
    assert "JOINERS" not in caplog.text


# ── main() ────────────────────────────────────────────────────────


def test_main_logs_and_reraises(monkeypatch, caplog):
    def boom(*_args, **_kwargs):
        raise RuntimeError("run exploded")

    monkeypatch.setattr(pipeline, "run", boom)
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        pipeline.main()
    assert "Unhandled error in pipeline" in caplog.text


# ── The full lifecycle ────────────────────────────────────────────


def test_a_full_multi_day_lifecycle(directory, run_pipeline, db_path, isolated_paths):
    """Three runs covering a joiner, a change, and the report that follows.

    Each run is stamped with today's date, so this exercises the same-day
    replace path rather than genuine multi-day history — which is precisely
    the path a re-run in production takes.
    """
    people = population(20)
    directory(people)
    run_pipeline()

    people[dn_for("joiner")] = make_record("joiner", "Jo Joiner", title="Analyst")
    people[dn_for("user01")][ATTR_TITLE] = "Staff Engineer"
    directory(people)
    run_pipeline()

    conn = db_module.connect_db(db_path)
    try:
        stored = conn.execute("SELECT COUNT(*) FROM user_history").fetchone()[0]
        changes = conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
    finally:
        conn.close()

    assert stored == 21
    assert changes >= 1

    content = archived_report(isolated_paths)
    assert "Jo Joiner" in content
    assert "Staff Engineer" in content
    # The document must be complete and self-consistent.
    assert content.count("<html>") == 1
    assert content.count("</html>") == 1


def test_the_entrypoint_module_is_importable():
    """The console script and __main__ path must both resolve."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import ldap_time_machine

    assert callable(ldap_time_machine.main)


def test_unmapped_tracked_roles_are_named_at_run_start(
    monkeypatch, directory_stub, caplog
):
    """Dropping an unmapped tracked role is documented behaviour, but doing it
    silently surprises a new deployer — one warning names the roles."""
    monkeypatch.setattr(pipeline, "UNMAPPED_TRACKED_ROLES", ["division"])
    with caplog.at_level("WARNING"):
        directory_stub()
    assert "leaves unmapped: division" in caplog.text
