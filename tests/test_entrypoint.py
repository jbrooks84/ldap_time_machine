"""Tests for the command-line entry point.

The dry-run snapshot is the interesting part. Copying a live SQLite file with
``cp`` is the classic way to produce a corrupt backup — the copy can catch a
torn page, and the WAL sidecar holds committed data the main file does not
have yet. ``VACUUM INTO`` produces a consistent, compacted copy instead, and
these tests prove the resulting database is actually usable.
"""

import os
import sqlite3
import sys

import pytest

from ltm import cli as entrypoint
from ltm import db as db_module

from .conftest import dn_for, make_record, write_snapshot


@pytest.fixture
def live_db(db_path):
    """A populated database with uncheckpointed data sitting in the WAL."""
    conn = db_module.init_db(db_path=db_path)
    with db_module.write_transaction(conn):
        write_snapshot(
            conn,
            "2026-06-30",
            {dn_for("alice"): make_record("alice", "Alice Adams")},
        )
    # Deliberately left open, so the WAL is not checkpointed. This is the
    # state a naive file copy gets wrong.
    yield db_path, conn
    conn.close()


def test_snapshot_produces_a_usable_copy(live_db, tmp_path):
    source, _conn = live_db
    destination = str(tmp_path / "dryrun.db")
    entrypoint._snapshot_db(source, destination)

    copy = sqlite3.connect(destination)
    try:
        assert copy.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 1
    finally:
        copy.close()


def test_snapshot_captures_data_still_in_the_wal(live_db, tmp_path):
    """The row was never checkpointed; a plain file copy would lose it."""
    source, conn = live_db
    with db_module.write_transaction(conn):
        write_snapshot(
            conn, "2026-06-30", {dn_for("bob"): make_record("bob", "Bob Brown")}
        )
    destination = str(tmp_path / "dryrun.db")
    entrypoint._snapshot_db(source, destination)

    copy = sqlite3.connect(destination)
    try:
        names = {
            row[0]
            for row in copy.execute(
                "SELECT json_extract(data, '$.displayName') FROM user_history"
            ).fetchall()
        }
    finally:
        copy.close()
    assert names == {"Alice Adams", "Bob Brown"}


def test_snapshot_leaves_the_source_untouched(live_db, tmp_path):
    """A dry run must be provably read-only against the live database."""
    source, _conn = live_db
    before = os.path.getsize(source)
    entrypoint._snapshot_db(source, str(tmp_path / "dryrun.db"))
    assert os.path.getsize(source) == before


def test_snapshot_is_owner_only(live_db, tmp_path):
    source, _conn = live_db
    destination = str(tmp_path / "dryrun.db")
    entrypoint._snapshot_db(source, destination)
    assert oct(os.stat(destination).st_mode)[-3:] == "600"


def test_snapshot_replaces_an_existing_destination(live_db, tmp_path):
    source, _conn = live_db
    destination = tmp_path / "dryrun.db"
    destination.write_text("stale content", encoding="utf-8")
    entrypoint._snapshot_db(source, str(destination))

    copy = sqlite3.connect(str(destination))
    try:
        assert copy.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 1
    finally:
        copy.close()


def test_snapshot_clears_stale_sidecars(live_db, tmp_path):
    """Leftover -wal/-shm would be misread as belonging to the new copy."""
    source, _conn = live_db
    destination = tmp_path / "dryrun.db"
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"dryrun.db{suffix}").write_text("stale", encoding="utf-8")
    entrypoint._snapshot_db(source, str(destination))
    assert not (tmp_path / "dryrun.db-wal").exists()
    assert not (tmp_path / "dryrun.db-shm").exists()


def test_snapshot_exits_when_the_source_is_missing(tmp_path, caplog):
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exit_info:
        entrypoint._snapshot_db(
            str(tmp_path / "absent.db"), str(tmp_path / "dryrun.db")
        )
    assert exit_info.value.code == 1
    assert "not found" in caplog.text


def test_snapshot_falls_back_when_vacuum_into_is_unavailable(
    live_db, tmp_path, monkeypatch, caplog
):
    """SQLite older than 3.27 has no VACUUM INTO; the copy must still happen."""
    source, _conn = live_db
    destination = str(tmp_path / "dryrun.db")

    real_connect = sqlite3.connect

    class OldSQLite:
        def __init__(self, path, isolation_level="DEFERRED"):
            self._conn = real_connect(path, isolation_level=isolation_level)

        def execute(self, sql, *args):
            if sql.startswith("VACUUM INTO"):
                raise sqlite3.OperationalError('near "INTO": syntax error')
            return self._conn.execute(sql, *args)

        def close(self):
            self._conn.close()

    monkeypatch.setattr(entrypoint.sqlite3, "connect", OldSQLite)
    with caplog.at_level("WARNING"):
        entrypoint._snapshot_db(source, destination)
    assert "falling back to copy" in caplog.text

    copy = real_connect(destination)
    try:
        assert copy.execute("SELECT COUNT(*) FROM user_history").fetchone()[0] == 1
    finally:
        copy.close()


# ── Argument parsing ──────────────────────────────────────────────


def test_the_default_invocation_runs_the_live_pipeline(monkeypatch):
    calls = []
    monkeypatch.setattr(entrypoint, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(entrypoint, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine"])
    entrypoint.main()
    assert calls == [{}]


def test_dry_run_redirects_writes_and_skips_mail(monkeypatch):
    calls = []
    snapshots = []
    monkeypatch.setattr(entrypoint, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(
        entrypoint, "_snapshot_db", lambda src, dst: snapshots.append((src, dst))
    )
    monkeypatch.setattr(entrypoint, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine", "--dry-run"])
    entrypoint.main()

    assert len(snapshots) == 1
    assert calls[0]["send_email"] is False
    assert calls[0]["db_path"] == entrypoint.DRY_RUN_DB_FILE


def test_version_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        entrypoint.main()
    assert exit_info.value.code == 0
    assert entrypoint.VERSION in capsys.readouterr().out


def test_a_pipeline_exit_propagates_without_extra_noise(monkeypatch, caplog):
    """sys.exit inside the pipeline already logged its reason."""
    monkeypatch.setattr(entrypoint, "setup_logging", lambda *a, **k: None)

    def bail(**_kwargs):
        raise SystemExit(1)

    monkeypatch.setattr(entrypoint, "run", bail)
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine"])
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        entrypoint.main()
    assert "Unhandled error" not in caplog.text


def test_an_unexpected_error_is_logged_with_a_traceback(monkeypatch, caplog):
    monkeypatch.setattr(entrypoint, "setup_logging", lambda *a, **k: None)

    def boom(**_kwargs):
        raise RuntimeError("something broke")

    monkeypatch.setattr(entrypoint, "run", boom)
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine"])
    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        entrypoint.main()
    assert "Unhandled error reached the entry point" in caplog.text


# ── Dry-run copy cleanup ──────────────────────────────────────────


def test_a_successful_dry_run_removes_its_database_copy(monkeypatch, tmp_path):
    """The copy is live-database sized; leaving one per dry run fills /tmp."""
    copy = tmp_path / "dryrun.db"
    monkeypatch.setattr(entrypoint, "DRY_RUN_DB_FILE", str(copy))
    monkeypatch.setattr(entrypoint, "setup_logging", lambda *a, **k: None)

    def fake_snapshot(src, dst):
        pathlib_local = __import__("pathlib").Path
        pathlib_local(dst).write_bytes(b"db")
        pathlib_local(dst + "-wal").write_bytes(b"wal")

    monkeypatch.setattr(entrypoint, "_snapshot_db", fake_snapshot)
    monkeypatch.setattr(entrypoint, "run", lambda **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine", "--dry-run"])
    entrypoint.main()
    assert not copy.exists()
    assert not (tmp_path / "dryrun.db-wal").exists()


def test_a_failed_dry_run_keeps_the_copy_for_post_mortem(monkeypatch, tmp_path):
    copy = tmp_path / "dryrun.db"
    monkeypatch.setattr(entrypoint, "DRY_RUN_DB_FILE", str(copy))
    monkeypatch.setattr(entrypoint, "setup_logging", lambda *a, **k: None)
    monkeypatch.setattr(
        entrypoint, "_snapshot_db", lambda src, dst: copy.write_bytes(b"db")
    )

    def boom(**_kwargs):
        raise RuntimeError("pipeline failed")

    monkeypatch.setattr(entrypoint, "run", boom)
    monkeypatch.setattr(sys, "argv", ["ldap-time-machine", "--dry-run"])
    with pytest.raises(RuntimeError):
        entrypoint.main()
    assert copy.exists()  # deliberately retained


def test_cleanup_is_quiet_when_nothing_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(
        entrypoint, "DRY_RUN_DB_FILE", str(tmp_path / "never-created.db")
    )
    entrypoint._cleanup_dry_run_db()  # must not raise or log spuriously
