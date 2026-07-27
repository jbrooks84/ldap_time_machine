"""Shared fixtures.

The sample database built here is the backbone of the suite: a realistic
multi-day history with joiners, leavers, attribute changes, a flapper, a
homonym pair, and a bulk department rename.  Building it once, honestly,
means the tests exercise the same query shapes and edge cases a real
deployment hits, instead of asserting against three hand-written rows.
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from ltm import db as db_module

# The sample data uses Active Directory attribute names, matching the default
# flavor the package loads with when no config.yml is present.
ATTR_USERNAME = "sAMAccountName"
ATTR_DISPLAY = "displayName"
ATTR_CN = "cn"
ATTR_TITLE = "title"
ATTR_DEPT = "department"
ATTR_MANAGER = "manager"
ATTR_COUNTRY = "co"
ATTR_CITY = "l"
ATTR_OFFICE = "physicalDeliveryOfficeName"
ATTR_CREATED = "whenCreated"


def make_record(
    username,
    display,
    title="Engineer",
    department="Platform",
    country="United States",
    city="Austin",
    office="Austin HQ",
    manager="CN=Dana Lead,OU=People,DC=example,DC=com",
    created="20200115090000.0Z",
):
    """Build one directory record dict in the default (AD) attribute shape."""
    return {
        ATTR_USERNAME: username,
        ATTR_DISPLAY: display,
        ATTR_CN: display,
        ATTR_TITLE: title,
        ATTR_DEPT: department,
        ATTR_MANAGER: manager,
        ATTR_COUNTRY: country,
        ATTR_CITY: city,
        ATTR_OFFICE: office,
        ATTR_CREATED: created,
    }


def dn_for(username):
    """Return the DN this fixture data uses for a username."""
    return f"CN={username},OU=People,DC=example,DC=com"


@pytest.fixture
def db_path():
    """Yield a path to a throwaway database file, cleaned up afterwards."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    yield path
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.unlink(path + suffix)


@pytest.fixture
def conn(db_path):
    """Yield an initialised, empty database connection."""
    connection = db_module.init_db(db_path=db_path)
    yield connection
    connection.close()


def write_snapshot(conn, run_date, records, update_summary=True):
    """Insert one day's snapshot.  records is {dn: record_dict}.

    Also records the run_summary row, because that is what the pipeline does in
    the same transaction. Pass ``update_summary=False`` to simulate history that
    predates the summary table.
    """
    conn.executemany(
        "INSERT OR REPLACE INTO user_history (run_date, dn, data, data_hash) "
        "VALUES (?, ?, ?, ?)",
        [(run_date, dn, json.dumps(rec), "") for dn, rec in records.items()],
    )
    if update_summary:
        db_module.record_run_summary(conn, run_date, len(records))


def write_changes(conn, rows):
    """Insert change rows: (run_date, dn, attribute, old_val, new_val)."""
    conn.executemany(
        "INSERT OR IGNORE INTO changes "
        "(run_date, dn, attribute, old_val, new_val) VALUES (?, ?, ?, ?, ?)",
        rows,
    )


@pytest.fixture
def today():
    """A fixed 'today' so date arithmetic in tests is deterministic."""
    return "2026-06-30"


@pytest.fixture
def sample_db(conn, today):
    """A 40-day history exercising every path the report cares about.

    Shape of the data:

    * ``alice``/``bob``/``carol`` are present throughout — the stable core.
    * ``dave`` joins on the final day, so he is a joiner.
    * ``erin`` was last present 7 days before the final date — the run just
      before the confirmation window — so she is absent from all 7 dates
      inside it and is a confirmed leaver today.
    * ``frank`` left on the same day as Erin but also has a gap earlier in his
      history — he is a confirmed leaver *and* a known flapper.
    * ``mallory`` disappeared only on the final day. She is the debounce case:
      absent, but nowhere near the 7-day threshold, so she must not be
      reported as a leaver.
    * ``grace`` and a second ``Grace Hopper`` share a display name, which is
      the homonym case.
    * On the final day a bulk department rename moves four people from
      ``Platform`` to ``Core Platform``, plus one individual move.
    """
    end = datetime.strptime(today, "%Y-%m-%d")
    dates = [(end - timedelta(days=n)).strftime("%Y-%m-%d") for n in range(39, -1, -1)]

    people = {
        "alice": make_record("alice", "Alice Adams", title="Staff Engineer"),
        "bob": make_record("bob", "Bob Brown", department="Networking"),
        "carol": make_record(
            "carol",
            "Carol Chen",
            country="United Kingdom",
            city="London",
            office="London Office",
            created="20150301090000.0Z",
        ),
        "grace": make_record("grace", "Grace Hopper", department="Research"),
        "grace2": make_record("grace2", "Grace Hopper", department="Security"),
        "heidi": make_record("heidi", "Heidi Hall"),
        "ivan": make_record(
            "ivan", "Ivan Ito", country="Japan", city="Tokyo", office="Tokyo Office"
        ),
        "judy": make_record(
            "judy", "Judy Jones", country="Japan", city="Tokyo", office="Tokyo Office"
        ),
    }
    erin = make_record("erin", "Erin Evans", created="20101101090000.0Z")
    frank = make_record("frank", "Frank Foster", created="20180601090000.0Z")
    mallory = make_record("mallory", "Mallory Mann")

    # The last day someone can be present and still count as absent for the
    # whole 7-day confirmation window ending on the final date.
    last_present = len(dates) - 8

    for index, run_date in enumerate(dates):
        snapshot = {dn_for(u): dict(r) for u, r in people.items()}

        if index <= last_present:
            snapshot[dn_for("erin")] = dict(erin)

        # Frank leaves on the same day as Erin, but is also absent for a
        # stretch in the middle — that earlier gap is what makes him a known
        # flapper on top of being a confirmed leaver.
        in_gap = 12 <= index <= 15
        if index <= last_present and not in_gap:
            snapshot[dn_for("frank")] = dict(frank)

        # Mallory is present right up to the day before the end.
        if index < len(dates) - 1:
            snapshot[dn_for("mallory")] = dict(mallory)

        # Dave appears only on the final day.
        if index == len(dates) - 1:
            snapshot[dn_for("dave")] = make_record(
                "dave", "Dave Diaz", title="Analyst", department="Finance"
            )
            # The bulk rename lands on the final day too.
            for username in ("alice", "heidi", "ivan", "judy"):
                snapshot[dn_for(username)][ATTR_DEPT] = "Core Platform"
            snapshot[dn_for("bob")][ATTR_TITLE] = "Principal Engineer"

        write_snapshot(conn, run_date, snapshot)

    # Change rows matching that final day, as the pipeline would have written.
    final = dates[-1]
    rows = [
        (final, dn_for(u), ATTR_DEPT, "Platform", "Core Platform")
        for u in ("alice", "heidi", "ivan", "judy")
    ]
    rows.append((final, dn_for("bob"), ATTR_TITLE, "Engineer", "Principal Engineer"))
    rows.append((final, dn_for("carol"), ATTR_COUNTRY, "United Kingdom", "Ireland"))
    rows.append((final, dn_for("carol"), ATTR_CITY, "London", "Dublin"))
    # An older change, inside the flap lookback window, used to prove that a
    # value returning to a previously-seen state is suppressed.
    earlier = dates[-3]
    rows.append((earlier, dn_for("grace"), ATTR_TITLE, "Engineer", "Senior Engineer"))
    write_changes(conn, rows)

    return conn


@pytest.fixture
def current_state(sample_db, today):
    """The final day's snapshot as {dn: record}, for report-level tests."""
    return db_module.get_snapshot_state(sample_db, today)


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """Redirect every file the pipeline writes into a temporary directory.

    Config resolves paths at import time, so the constants are patched where
    each module imported them rather than on ltm.config itself.
    """
    from ltm import pipeline, report

    archive = tmp_path / "email_archive"
    monkeypatch.setattr(pipeline, "LOCK_FILE", str(tmp_path / "run.lock"))
    monkeypatch.setattr(report, "EMAIL_ARCHIVE_DIR", str(archive))
    monkeypatch.setattr(report, "TREND_GRAPH_FILE", str(tmp_path / "trend.png"))
    return {"root": tmp_path, "archive": archive}


@pytest.fixture
def directory_stub(db_path, isolated_paths, monkeypatch):
    """Run the pipeline once against a stubbed directory fetch.

    Lighter than the subprocess-backed fixture in test_pipeline: this one
    replaces the fetch outright, for tests that care about what the pipeline
    does with the data rather than how it obtained it.
    """
    from ltm import pipeline, report
    from ltm.secrets import DISABLED_SMTP

    people = {
        dn_for(f"user{i:02d}"): make_record(f"user{i:02d}", f"User {i:02d}")
        for i in range(5)
    }
    monkeypatch.setattr(pipeline, "fetch_directory_records", lambda: people)
    monkeypatch.setattr(report, "get_smtp_settings", lambda: DISABLED_SMTP)

    def run(send_email=False):
        pipeline._run_pipeline(db_path=db_path, send_email=send_email)
        return db_path

    return run


@pytest.fixture(autouse=True)
def clear_secrets_cache():
    """Keep cached credentials from leaking between tests."""
    from ltm import secrets

    secrets.reset_cache()
    yield
    secrets.reset_cache()


class FailingConnection:
    """A connection stand-in whose every operation raises.

    ``sqlite3.Connection`` attributes are read-only, so a broken database
    cannot be simulated with monkeypatch.  This substitutes for one instead,
    which is what the error-handling paths need in order to be exercised at
    all.
    """

    def execute(self, *_args, **_kwargs):
        raise sqlite3.Error("simulated database failure")

    def cursor(self, *_args, **_kwargs):
        raise sqlite3.Error("simulated database failure")

    def close(self):
        """No-op, so code that closes the connection in a finally block works."""
