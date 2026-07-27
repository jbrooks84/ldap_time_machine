"""Edge cases and fallback paths.

These are the branches that only fire on a first run, an unmapped role, or a
directory that behaves unusually — the paths nobody exercises by hand, and so
the ones most likely to be broken when they finally matter.
"""

import os
import re

import pytest
import yaml

from ltm import analysis, config, highlights, pipeline, report
from ltm.config import ATTR
from ltm.ldif_parser import parse_ldif_output

from .conftest import ATTR_COUNTRY, make_record, write_changes

# ── Disabling the noise controls ──────────────────────────────────


def test_a_zero_lookback_disables_flap_suppression(conn):
    """The setting a clean directory uses: report every change."""
    write_changes(conn, [("2026-06-20", "CN=A", "title", "Engineer", "Analyst")])
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=0)
    assert len(index) == 0
    assert not index.is_flapping("CN=A", "title", "Engineer")


def test_a_negative_lookback_also_disables_it(conn):
    index = analysis.FlapIndex.load(conn, "2026-06-25", lookback_days=-1)
    assert len(index) == 0


def test_leaver_scan_stops_early_once_nobody_is_left(sample_db, today):
    """The window loop short-circuits as soon as the candidate set empties.

    With a two-day window, everyone present on the day before it is also
    present on its first day, so there is nothing left to check on day two.
    """
    leavers, dates = analysis.get_confirmed_leavers(sample_db, today, 2)
    assert leavers == []
    assert len(dates) == 2


def test_leaver_scan_handles_a_window_longer_than_the_history(sample_db, today):
    leavers, _dates = analysis.get_confirmed_leavers(sample_db, today, 40)
    assert leavers == []


# ── Config discovery ──────────────────────────────────────────────


def test_config_is_discovered_next_to_the_repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    discovered = tmp_path / "config.yml"
    discovered.write_text(yaml.safe_dump({"ldap": {"server": "x"}}), encoding="utf-8")
    assert config.find_config_file(env={}) == str(discovered)


def test_config_falls_back_to_the_xdg_location(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path / "repo"))
    xdg = tmp_path / "xdg-config.yml"
    xdg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config.os.path, "expanduser", lambda _p: str(xdg))
    assert config.find_config_file(env={}) == str(xdg)


# ── LDIF parsing edge cases ───────────────────────────────────────


def test_a_base64_dn_flushes_the_preceding_record():
    """A record must be committed before the next one starts, either notation."""
    import base64

    encoded = base64.b64encode(b"CN=Renee,DC=example").decode("ascii")
    ldif = f"dn: CN=Plain,DC=example\ntitle: First\ndn:: {encoded}\ntitle: Second\n"
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=Plain,DC=example"]["title"] == "First"
    assert parsed["CN=Renee,DC=example"]["title"] == "Second"


def test_three_distinct_values_accumulate_into_one_list():
    ldif = (
        "dn: CN=A,DC=example\n"
        "memberOf: CN=One,DC=x\n"
        "memberOf: CN=Two,DC=x\n"
        "memberOf: CN=Three,DC=x\n"
    )
    parsed = parse_ldif_output(ldif)
    assert parsed["CN=A,DC=example"]["memberOf"] == [
        "CN=One,DC=x",
        "CN=Two,DC=x",
        "CN=Three,DC=x",
    ]


# ── Highlights without a baseline ─────────────────────────────────


def test_the_longest_tenured_leaver_keeps_the_earliest_seen():
    """A later start date must not displace an earlier one already found."""
    from datetime import datetime

    from ltm.dates import parse_date_value

    leavers = [
        {"displayName": "Early", "whenCreated": "20050101000000.0Z"},
        {"displayName": "Later", "whenCreated": "20200101000000.0Z"},
    ]
    result = highlights.longest_tenured_leaver(leavers, datetime(2026, 6, 30))
    assert result["name"] == "Early"
    assert parse_date_value("20050101000000.0Z").year == 2005


def test_country_mover_is_none_without_any_baseline(conn):
    assert highlights.biggest_country_mover(conn, "2026-06-30") is None


def test_office_mover_is_none_without_any_baseline(conn):
    assert highlights.biggest_office_mover(conn, "2026-06-30") is None


# ── Report branches for unusual configurations ────────────────────


def test_the_leaver_tile_includes_a_country_when_one_is_known():
    tile = {"name": "Erin Evans", "tenure": "12y 3m", "country": "Ireland", "count": 1}
    out = report.render_highlights_html(tile, None, None, None)
    assert "Erin Evans" in out
    assert "Ireland" in out
    assert "12y 3m" in out


def test_the_leaver_tile_omits_a_missing_country():
    tile = {"name": "Erin Evans", "tenure": "12y 3m", "country": "", "count": 1}
    out = report.render_highlights_html(tile, None, None, None)
    assert "Erin Evans" in out


def test_city_gets_its_own_section_when_country_is_not_tracked(monkeypatch):
    """Location changes must still render for a directory with no country."""
    monkeypatch.setattr(report, "TRACKED_ROLES", ["city"])
    mods = [
        {
            "name": "Carol Chen",
            "username": "carol",
            "changes": [{"attr": ATTR.city, "old": "London", "new": "Dublin"}],
        }
    ]
    out = report.render_attribute_changes(mods, 3, "last 24 hours")
    assert "Location Changes" in out
    assert "Dublin" in out


def test_seniority_is_skipped_when_no_date_role_is_mapped(monkeypatch, current_state):
    """A directory exposing no timestamps cannot rank anyone by tenure."""
    monkeypatch.setattr(
        report.ATTR, "_map", dict(ATTR.as_dict(), created=None, start_date=None)
    )
    assert report.build_seniority_table(list(current_state.values())) == ""


def test_an_unmapped_aggregate_role_is_skipped(
    monkeypatch, sample_db, today, current_state
):
    monkeypatch.setattr(report.ATTR, "_map", dict(ATTR.as_dict(), division=None))
    out = report.build_aggregate_sections(sample_db, today, 10, current_state)
    assert "Divisions" not in out
    assert "Countries" in out


def test_an_aggregate_table_with_only_unknown_values_is_skipped(
    conn, today, monkeypatch
):
    """A column of "Unknown" is not a breakdown worth printing."""
    from ltm import db as db_module

    from .conftest import write_snapshot

    records = {}
    for index in range(10):
        record = make_record(f"u{index}", f"User {index}")
        record[ATTR_COUNTRY] = "Unknown"
        records[f"CN={index}"] = record
    with db_module.write_transaction(conn):
        write_snapshot(conn, today, records)

    monkeypatch.setattr(report, "TOP_N", {"countries": 25})
    assert "Countries" not in report.build_aggregate_sections(conn, today, 10, records)


# ── Pipeline fallbacks ────────────────────────────────────────────


def test_the_pipeline_falls_back_to_daily_changes(monkeypatch, directory_stub, caplog):
    """With no change window available, the day's own diff is used instead."""
    monkeypatch.setattr(pipeline, "get_changes_window", lambda *a, **k: ([], []))
    with caplog.at_level("INFO"):
        directory_stub()
    assert "Insufficient history for change window" in caplog.text


def test_the_pipeline_notes_a_missing_leaver_window(
    monkeypatch, directory_stub, caplog
):
    monkeypatch.setattr(pipeline, "get_confirmed_leavers", lambda *a, **k: ([], []))
    with caplog.at_level("INFO"):
        directory_stub()
    assert "Insufficient history for confirmed leaver window" in caplog.text


def test_the_headcount_tile_omits_deltas_it_cannot_compute():
    """A young database has no 30- or 90-day baseline; show the count alone."""
    out = report.render_highlights_html(
        None, None, None, {"now": 42, "delta_30": None, "delta_90": None}
    )
    assert "42" in out
    assert "(30d)" not in out
    assert "(90d)" not in out


def test_extract_cn_handles_a_dn_whose_first_component_is_not_cn():
    """OU-first DNs occur; the CN must still be found, not the first component."""
    from ltm.ldif_parser import extract_cn

    assert extract_cn("OU=Contractors,CN=Jane Smith,DC=example") == "Jane Smith"


def test_extract_cn_returns_the_input_when_cn_is_only_a_substring():
    """ "CN=" inside a value is not a CN component; return the value unchanged."""
    from ltm.ldif_parser import extract_cn

    value = "OU=teamCN=notreal,DC=example"
    assert extract_cn(value) == value


def test_database_path_falls_back_for_an_in_memory_database():
    """An in-memory database reports an empty path; fall back rather than return ''."""
    import sqlite3

    from ltm import db as db_module

    memory = sqlite3.connect(":memory:")
    try:
        assert db_module._database_path(memory) == db_module.DB_FILE
    finally:
        memory.close()


def test_folded_continuation_before_any_content_is_dropped():
    """A leading continuation line has nothing to continue and must not crash."""
    parsed = parse_ldif_output("  orphan continuation\ndn: CN=A,DC=example\n")
    assert parsed == {"CN=A,DC=example": {}}


def test_database_path_reads_the_pragma(conn, db_path):
    from ltm import db as db_module

    assert os.path.realpath(db_module._database_path(conn)) == os.path.realpath(db_path)


def test_logging_accepts_a_bare_filename(tmp_path, monkeypatch):
    """A log path with no directory component must not trip the makedirs call."""
    import logging

    from ltm import logging_utils

    monkeypatch.chdir(tmp_path)
    try:
        logging_utils.setup_logging(log_file="bare.log")
        logging.info("written")
        assert (tmp_path / "bare.log").exists()
    finally:
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
            handler.close()


# ── Package metadata ──────────────────────────────────────────────


def test_the_package_exposes_its_version():
    import ltm

    assert ltm.VERSION == config.VERSION
    assert "VERSION" in ltm.__all__


def test_every_version_reference_comes_from_one_place():
    """The version must not be able to drift between code and packaging.

    ``ltm/_version.py`` is the single declaration; ``pyproject.toml`` reads it
    dynamically. A duplicated literal is the thing this guards against — it
    stays correct right up until someone bumps one and not the other, and the
    symptom is a released artifact reporting the wrong version.
    """
    import ltm
    from ltm import _version

    assert ltm.__version__ == _version.__version__
    assert _version.__version__ == ltm.VERSION
    assert _version.__version__ == config.VERSION


def test_the_installed_metadata_matches_the_source():
    import importlib.metadata

    from ltm import _version

    try:
        installed = importlib.metadata.version("ldap-time-machine")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        pytest.skip("package is not installed in this environment")
    assert installed == _version.__version__


def test_pyproject_does_not_hardcode_a_version():
    """Catch a future edit that reintroduces a second source of truth."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as handle:
        content = handle.read()
    assert 'dynamic = ["version"]' in content
    assert 'attr = "ltm._version.__version__"' in content
    # A literal `version = "x.y.z"` in [project] would silently win.
    assert not re.search(r'^version\s*=\s*"', content, re.MULTILINE)


def test_the_example_config_parses_and_is_accepted():
    """A broken example is worse than none: it is the first thing people copy."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example = os.path.join(root, "config.example.yml")
    with open(example, encoding="utf-8") as handle:
        parsed = yaml.safe_load(handle)
    resolved = config.load_config(path=example, env={})
    assert parsed["ldap"]["flavor"] in config.FLAVORS
    assert resolved["ldap"]["filter"]


def test_the_example_credentials_file_parses():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "credentials.example.yml"), encoding="utf-8"
    ) as handle:
        parsed = yaml.safe_load(handle)
    for key in ("LDAP_USERNAME", "SMTP_SERVER", "TO_EMAIL", "SEND_EMAIL"):
        assert key in parsed


def test_the_example_files_contain_no_real_looking_secrets():
    """Guard against a real password reaching the repository through an example."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(
        os.path.join(root, "credentials.example.yml"), encoding="utf-8"
    ) as handle:
        parsed = yaml.safe_load(handle)
    assert parsed["LDAP_PASSWORD"] == "change-me"
    assert "example.com" in parsed["LDAP_USERNAME"]


def test_no_source_file_mentions_a_prior_employer():
    """The repository must not carry its origin's internal names or hosts.

    This project was generalised from an internal tool. A single overlooked
    hostname or distribution list in a public repository is not something a
    later commit can take back, so the check runs on every test run rather
    than relying on anyone remembering to grep before pushing.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    this_file = os.path.abspath(__file__)

    # Assembled at runtime so this file does not itself contain the strings
    # it forbids, which would make the check permanently self-failing.
    banned = (
        # Employer and infrastructure identifiers.
        "rack" + "space",
        "rxt" + "-pvc",
        "gd" + "ci",
        "/home/" + "rack",
        "ngm" + "-ldap",
        "explore" + "_ldap",
        "network" + "lab_svc",
        "ad." + "auth.",
        # People.
        "jan." + "brooks",
        "jmy" + "hre",
        "lam." + "nguyen",
        "bhupen" + "dra",
        "zinn" + "dorf",
        # Internal operational facts: real deployment stats, event dates,
        # commit hashes, schedules. Names were never the whole problem —
        # operational anecdotes identify an employer just as surely.
        "712" + "840",
        "2025-" + "12-24",
        "d4757" + "8a",
        "a8231" + "b2",
        "project_" + "flapper",
        "27-" + "year",
        "sole " + "maintainer",
        "07:" + "20",
    )

    offenders = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache"}
        ]
        for filename in filenames:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".toml", ".txt")):
                continue
            path = os.path.join(dirpath, filename)
            if os.path.abspath(path) == this_file:
                continue
            with open(path, encoding="utf-8", errors="ignore") as handle:
                lowered = handle.read().lower()
            for term in banned:
                if term in lowered:
                    offenders.append(f"{os.path.relpath(path, root)}: {term}")
    assert offenders == []
