"""Tests for --replay: re-rendering a past report from stored history.

The point of replay is fidelity: the output must describe the replayed date
— its joiners, its confirmed leavers, tenure as of that day, a trend graph
that stops there — and must never touch the directory, the mail relay, or
the snapshot tables.
"""

from pathlib import Path

import pytest

from ltm import cli, pipeline, report
from ltm.secrets import DISABLED_SMTP

from .conftest import dn_for, make_record, write_snapshot


@pytest.fixture
def no_smtp(monkeypatch):
    monkeypatch.setattr(report, "get_smtp_settings", lambda: DISABLED_SMTP)


def test_replay_reproduces_the_final_day(
    sample_db, db_path, today, isolated_paths, no_smtp
):
    """Replaying the last recorded day matches what that day's run reported."""
    path = pipeline.replay(today, db_path=db_path)

    assert "dryrun_" in path  # investigation output, kept out of the real bucket
    html = Path(path).read_text(encoding="utf-8")
    assert "Dave Diaz" in html  # that day's joiner
    assert "Erin Evans" in html and "Frank Foster" in html  # confirmed leavers
    assert "Core Platform" in html  # the bulk rename landed that day
    assert "Grace Hopper" in html


def test_replay_of_an_earlier_date_shows_that_days_world(
    sample_db, db_path, isolated_paths, no_smtp
):
    """A mid-history replay must not leak anything that happened afterwards."""
    dates = [
        row[0]
        for row in sample_db.execute(
            "SELECT DISTINCT run_date FROM user_history ORDER BY run_date"
        )
    ]
    mid = dates[-5]  # before dave joined and before the bulk rename

    path = pipeline.replay(mid, db_path=db_path)
    html = Path(path).read_text(encoding="utf-8")
    assert "Dave Diaz" not in html
    assert "Core Platform" not in html


def test_replay_does_not_modify_the_snapshot_tables(
    sample_db, db_path, today, isolated_paths, no_smtp
):
    before = sample_db.execute("SELECT COUNT(*) FROM user_history").fetchone()[0]
    pipeline.replay(today, db_path=db_path)
    after = sample_db.execute("SELECT COUNT(*) FROM user_history").fetchone()[0]
    assert after == before


def test_replay_refuses_a_date_with_no_snapshot(sample_db, db_path, isolated_paths):
    with pytest.raises(SystemExit) as exc:
        pipeline.replay("1999-01-01", db_path=db_path)
    assert exc.value.code == 1


def test_replay_refuses_a_missing_database(tmp_path, isolated_paths):
    with pytest.raises(SystemExit) as exc:
        pipeline.replay("2026-06-30", db_path=str(tmp_path / "absent.db"))
    assert exc.value.code == 1


def test_replay_tenure_is_as_of_the_replayed_date(
    conn, db_path, isolated_paths, no_smtp
):
    """A leaver's tenure must be what it was then, not inflated to today."""
    zoe = make_record("zoe", "Zoe Zheng", created="20200101000000.0Z")
    ann = {dn_for("ann"): make_record("ann", "Ann Ames")}
    # Present through 01-03 (the run just before the 7-day window), then
    # absent for the window's seven runs — confirmed as a leaver on 01-10.
    for day in (2, 3):
        write_snapshot(conn, f"2021-01-{day:02d}", {dn_for("zoe"): zoe, **ann})
    for day in range(4, 11):
        write_snapshot(conn, f"2021-01-{day:02d}", dict(ann))

    path = pipeline.replay("2021-01-10", db_path=db_path)
    html = Path(path).read_text(encoding="utf-8")
    assert "Zoe Zheng" in html
    assert "1y 0m" in html  # ~1 year as of 2021, not 6+ as of the wall clock


def test_trend_graph_truncation_can_empty_the_series(sample_db, tmp_path):
    """A cutoff before all history yields no graph; the full series yields one."""
    out = tmp_path / "trend.png"
    assert not report.generate_trend_graph(
        sample_db, str(out), end_date_str="1990-01-01"
    )
    assert report.generate_trend_graph(sample_db, str(out))


def test_cli_replay_dispatches_to_the_pipeline(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "setup_logging", lambda: None)
    monkeypatch.setattr(cli, "replay", lambda date: seen.setdefault("date", date))
    monkeypatch.setattr(
        cli.sys, "argv", ["ldap-time-machine", "--replay", "2026-06-30"]
    )
    cli.main()
    assert seen["date"] == "2026-06-30"


def test_cli_rejects_a_malformed_replay_date(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "argv", ["ldap-time-machine", "--replay", "June 3rd"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_cli_replay_and_dry_run_are_mutually_exclusive(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.sys,
        "argv",
        ["ldap-time-machine", "--replay", "2026-06-30", "--dry-run"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_rapid_archives_never_overwrite_each_other(tmp_path):
    """A scripted replay loop archives several reports in one second."""
    first = report.archive_email_html("<html>a</html>", directory=str(tmp_path))
    second = report.archive_email_html("<html>b</html>", directory=str(tmp_path))
    assert first != second
    assert len(list(tmp_path.glob("ldap_report_*.html"))) == 2
