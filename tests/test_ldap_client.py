"""Tests for the ldapsearch wrapper.

Several of these run a *real* subprocess against a fake ``ldapsearch`` script
placed on PATH. That exercises the parts a mocked subprocess would skip: the
password temp file actually being written and read, the argument order being
one the binary accepts, and exit codes being interpreted correctly.
"""

import os
import stat

import pytest

from ltm import ldap_client

SAMPLE_LDIF = """\
# extended LDIF
dn: CN=Alice Adams,OU=People,DC=example,DC=com
sAMAccountName: alice
displayName: Alice Adams
title: Staff Engineer

dn: CN=Bob Brown,OU=People,DC=example,DC=com
sAMAccountName: bob
displayName: Bob Brown
title: Engineer
"""


@pytest.fixture
def fake_credentials(monkeypatch):
    """Supply a bind user and password without touching the credentials file."""
    monkeypatch.setattr(
        ldap_client, "get_ldap_credentials", lambda: ("svc@example.com", "secret")
    )


def install_fake_ldapsearch(tmp_path, monkeypatch, body):
    """Put an executable fake `ldapsearch` at the front of PATH.

    ``body`` is shell source, written verbatim — no dedent, so callers must
    not indent it. Shell is whitespace-sensitive in ways that silently change
    behaviour rather than erroring.
    """
    script = tmp_path / "ldapsearch"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return script


def responding_with(tmp_path, monkeypatch, ldif, exit_code=0):
    """Install a fake ldapsearch that emits `ldif` and exits with `exit_code`."""
    response = tmp_path / "response.ldif"
    response.write_text(ldif, encoding="utf-8")
    return install_fake_ldapsearch(
        tmp_path, monkeypatch, f'cat "{response}"\nexit {exit_code}\n'
    )


# ── Command construction ──────────────────────────────────────────


def test_command_includes_the_server_bind_and_base():
    cmd = ldap_client.build_command("svc@example.com")
    assert cmd[0] == "ldapsearch"
    assert "-x" in cmd
    assert cmd[cmd.index("-H") + 1] == ldap_client.LDAP_SERVER
    assert cmd[cmd.index("-D") + 1] == "svc@example.com"
    assert cmd[cmd.index("-b") + 1] == ldap_client.LDAP_BASE_DN


def test_command_requests_paged_results_by_default():
    cmd = ldap_client.build_command("svc")
    assert cmd[cmd.index("-E") + 1] == f"pr={ldap_client.LDAP_PAGE_SIZE}/noprompt"


def test_paging_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ldap_client, "LDAP_PAGE_SIZE", 0)
    assert "-E" not in ldap_client.build_command("svc")


def test_start_tls_is_mandatory_when_enabled(monkeypatch):
    """-ZZ fails the connection rather than silently continuing in the clear."""
    monkeypatch.setattr(ldap_client, "LDAP_START_TLS", True)
    assert "-ZZ" in ldap_client.build_command("svc")


def test_extra_args_are_appended(monkeypatch):
    monkeypatch.setattr(ldap_client, "LDAP_EXTRA_ARGS", ["-o", "ldif-wrap=no"])
    cmd = ldap_client.build_command("svc")
    assert cmd[-2:] == ["-o", "ldif-wrap=no"]


def test_the_password_never_appears_in_the_command():
    """Process arguments are world-readable; the password goes in a file."""
    assert "secret" not in " ".join(ldap_client.build_command("svc@example.com"))


# ── Fetching ──────────────────────────────────────────────────────


def test_fetch_parses_a_successful_response(tmp_path, monkeypatch, fake_credentials):
    responding_with(tmp_path, monkeypatch, SAMPLE_LDIF)
    records = ldap_client.fetch_directory_records()
    assert len(records) == 2
    assert "CN=Alice Adams,OU=People,DC=example,DC=com" in records


def test_fetch_passes_the_password_through_a_file(
    tmp_path, monkeypatch, fake_credentials
):
    """Prove the -y file is real, readable, and holds exactly the password."""
    captured = tmp_path / "captured"
    install_fake_ldapsearch(
        tmp_path,
        monkeypatch,
        f"while [ $# -gt 0 ]; do\n"
        f'  if [ "$1" = "-y" ]; then cat "$2" > "{captured}"; fi\n'
        f"  shift\n"
        f"done\n"
        f"exit 0\n",
    )
    ldap_client.fetch_directory_records()
    assert captured.read_text() == "secret"


def test_fetch_returns_nothing_without_credentials(monkeypatch, caplog):
    monkeypatch.setattr(ldap_client, "get_ldap_credentials", lambda: (None, None))
    with caplog.at_level("ERROR"):
        assert ldap_client.fetch_directory_records() == {}
    assert "credentials unavailable" in caplog.text


def test_fetch_refuses_partial_data_on_a_size_limit(
    tmp_path, monkeypatch, fake_credentials, caplog
):
    """Exit code 4 means truncation; storing it would invent leavers tomorrow."""
    responding_with(tmp_path, monkeypatch, SAMPLE_LDIF, exit_code=4)
    with caplog.at_level("ERROR"):
        assert ldap_client.fetch_directory_records() == {}
    assert "size limit exceeded" in caplog.text


def test_fetch_reports_a_generic_failure(
    tmp_path, monkeypatch, fake_credentials, caplog
):
    install_fake_ldapsearch(
        tmp_path,
        monkeypatch,
        'echo "ldap_bind: Invalid credentials (49)" >&2\nexit 49\n',
    )
    with caplog.at_level("ERROR"):
        assert ldap_client.fetch_directory_records() == {}
    assert "Invalid credentials" in caplog.text


def test_fetch_handles_a_missing_binary(
    monkeypatch, tmp_path, fake_credentials, caplog
):
    monkeypatch.setenv("PATH", str(tmp_path))  # An empty directory.
    with caplog.at_level("ERROR"):
        assert ldap_client.fetch_directory_records() == {}
    assert "Failed to execute ldapsearch" in caplog.text


def test_fetch_gives_up_on_a_timeout(tmp_path, monkeypatch, fake_credentials, caplog):
    install_fake_ldapsearch(tmp_path, monkeypatch, "sleep 5\nexit 0\n")
    monkeypatch.setattr(ldap_client, "LDAP_TIMEOUT_SECONDS", 1)
    with caplog.at_level("ERROR"):
        assert ldap_client.fetch_directory_records() == {}
    assert "timed out" in caplog.text


def test_fetch_returns_empty_for_an_empty_result(
    tmp_path, monkeypatch, fake_credentials
):
    install_fake_ldapsearch(tmp_path, monkeypatch, "exit 0\n")
    assert ldap_client.fetch_directory_records() == {}


def test_fetch_requests_the_configured_attributes(
    tmp_path, monkeypatch, fake_credentials
):
    captured = tmp_path / "args"
    install_fake_ldapsearch(
        tmp_path, monkeypatch, f'echo "$@" > "{captured}"\nexit 0\n'
    )
    ldap_client.fetch_directory_records()
    args = captured.read_text()
    for attribute in ldap_client.FETCH_ATTRIBUTES:
        assert attribute in args
    assert ldap_client.LDAP_FILTER in args
