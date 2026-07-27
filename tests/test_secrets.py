"""Tests for credential loading and SMTP settings resolution."""

import pytest
import yaml

from ltm import secrets

FULL_CREDENTIALS = {
    "LDAP_USERNAME": "svc-reader@example.com",
    "LDAP_PASSWORD": "bind-secret",
    "SMTP_SERVER": "smtp.example.com",
    "SMTP_PORT": 25,
    "FROM_EMAIL": "reports@example.com",
    "TO_EMAIL": "team@example.com",
    "SEND_EMAIL": True,
}


@pytest.fixture
def credentials_file(tmp_path, monkeypatch):
    """Point the loader at a temporary credentials file and return a writer."""

    def write(data):
        path = tmp_path / "credentials.yml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        monkeypatch.setattr(secrets, "CREDENTIALS_FILE", str(path))
        secrets.reset_cache()
        return str(path)

    return write


@pytest.fixture(autouse=True)
def no_stray_env(monkeypatch):
    """Strip LTM_ overrides so the host environment cannot skew a test."""
    for key in list(secrets.os.environ):
        if key.startswith("LTM_"):
            monkeypatch.delenv(key, raising=False)


# ── File loading ──────────────────────────────────────────────────


def test_loads_values_from_the_file(credentials_file):
    credentials_file(FULL_CREDENTIALS)
    assert secrets.get_ldap_credentials() == ("svc-reader@example.com", "bind-secret")


def test_missing_file_yields_no_credentials(monkeypatch, caplog):
    monkeypatch.setattr(secrets, "CREDENTIALS_FILE", "/nonexistent/credentials.yml")
    secrets.reset_cache()
    with caplog.at_level("ERROR"):
        assert secrets.get_ldap_credentials() == (None, None)
    assert "not found" in caplog.text


def test_a_non_mapping_file_is_rejected(tmp_path, monkeypatch, caplog):
    path = tmp_path / "credentials.yml"
    path.write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.setattr(secrets, "CREDENTIALS_FILE", str(path))
    secrets.reset_cache()
    with caplog.at_level("ERROR"):
        assert secrets.load_secrets() == {}
    assert "did not parse to a mapping" in caplog.text


def test_unparseable_yaml_is_reported(tmp_path, monkeypatch, caplog):
    path = tmp_path / "credentials.yml"
    path.write_text("key: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(secrets, "CREDENTIALS_FILE", str(path))
    secrets.reset_cache()
    with caplog.at_level("ERROR"):
        assert secrets.load_secrets() == {}
    assert "Failed to load" in caplog.text


def test_an_explicit_path_bypasses_the_cache(tmp_path):
    path = tmp_path / "other.yml"
    path.write_text(yaml.safe_dump({"LDAP_USERNAME": "direct"}), encoding="utf-8")
    assert secrets.load_secrets(path=str(path))["LDAP_USERNAME"] == "direct"


def test_the_cache_avoids_re_reading(credentials_file):
    path = credentials_file(FULL_CREDENTIALS)
    secrets.load_secrets()
    # Rewriting the file has no effect until the cache is cleared.
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump({"LDAP_USERNAME": "changed"}, handle)
    assert secrets.load_secrets()["LDAP_USERNAME"] == "svc-reader@example.com"
    secrets.reset_cache()
    assert secrets.load_secrets()["LDAP_USERNAME"] == "changed"


# ── LDAP credentials ──────────────────────────────────────────────


def test_missing_password_is_a_hard_stop(credentials_file, caplog):
    """An anonymous bind would silently return a different population."""
    credentials_file({"LDAP_USERNAME": "svc-reader@example.com"})
    with caplog.at_level("ERROR"):
        assert secrets.get_ldap_credentials() == (None, None)
    assert "Missing LDAP_USERNAME/LDAP_PASSWORD" in caplog.text


def test_lowercase_key_names_are_accepted(credentials_file):
    credentials_file({"ldap_username": "svc", "ldap_password": "pw"})
    assert secrets.get_ldap_credentials() == ("svc", "pw")


def test_environment_overrides_the_file(credentials_file, monkeypatch):
    credentials_file(FULL_CREDENTIALS)
    monkeypatch.setenv("LTM_LDAP_PASSWORD", "from-environment")
    assert secrets.get_ldap_credentials()[1] == "from-environment"


def test_an_empty_environment_value_does_not_override(credentials_file, monkeypatch):
    credentials_file(FULL_CREDENTIALS)
    monkeypatch.setenv("LTM_LDAP_PASSWORD", "")
    assert secrets.get_ldap_credentials()[1] == "bind-secret"


# ── SMTP settings ─────────────────────────────────────────────────


def test_smtp_settings_resolve_completely(credentials_file):
    credentials_file(FULL_CREDENTIALS)
    smtp = secrets.get_smtp_settings()
    assert smtp.enabled is True
    assert smtp.server == "smtp.example.com"
    assert smtp.port == 25
    assert smtp.recipients == ["team@example.com"]
    assert smtp.use_tls is False


def test_recipients_are_split_on_commas(credentials_file):
    credentials_file(dict(FULL_CREDENTIALS, TO_EMAIL="a@x.com, b@x.com ,c@x.com"))
    assert secrets.get_smtp_settings().recipients == ["a@x.com", "b@x.com", "c@x.com"]


def test_a_missing_setting_disables_delivery(credentials_file, caplog):
    """Rendering still happens; only delivery is switched off."""
    incomplete = dict(FULL_CREDENTIALS)
    del incomplete["SMTP_SERVER"]
    credentials_file(incomplete)
    with caplog.at_level("ERROR"):
        assert secrets.get_smtp_settings().enabled is False
    assert "SMTP_SERVER" in caplog.text


def test_a_non_numeric_port_disables_delivery(credentials_file, caplog):
    credentials_file(dict(FULL_CREDENTIALS, SMTP_PORT="not-a-number"))
    with caplog.at_level("ERROR"):
        assert secrets.get_smtp_settings().enabled is False
    assert "Invalid SMTP_PORT" in caplog.text


def test_an_empty_recipient_list_disables_delivery(credentials_file, caplog):
    credentials_file(dict(FULL_CREDENTIALS, TO_EMAIL=" , , "))
    with caplog.at_level("ERROR"):
        assert secrets.get_smtp_settings().enabled is False
    assert "no recipients" in caplog.text


def test_send_email_false_disables_delivery(credentials_file):
    credentials_file(dict(FULL_CREDENTIALS, SEND_EMAIL=False))
    assert secrets.get_smtp_settings().enabled is False


def test_send_email_false_with_incomplete_smtp_logs_no_error(credentials_file, caplog):
    """The master off-switch wins: an email-less deployment (README calls the
    relay optional) must not log ERROR about SMTP fields it deliberately
    left out."""
    incomplete = dict(FULL_CREDENTIALS, SEND_EMAIL=False)
    del incomplete["SMTP_PORT"]
    del incomplete["SMTP_SERVER"]
    credentials_file(incomplete)
    with caplog.at_level("INFO"):
        assert secrets.get_smtp_settings() == secrets.DISABLED_SMTP
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "SEND_EMAIL is false" in caplog.text


def test_optional_authentication_is_picked_up(credentials_file):
    credentials_file(
        dict(
            FULL_CREDENTIALS,
            SMTP_USE_TLS=True,
            SMTP_USERNAME="mailer",
            SMTP_PASSWORD="mail-secret",
        )
    )
    smtp = secrets.get_smtp_settings()
    assert smtp.use_tls is True
    assert (smtp.username, smtp.password) == ("mailer", "mail-secret")


def test_missing_credentials_produce_the_disabled_settings(monkeypatch):
    monkeypatch.setattr(secrets, "CREDENTIALS_FILE", "/nonexistent/credentials.yml")
    secrets.reset_cache()
    assert secrets.get_smtp_settings() == secrets.DISABLED_SMTP


# ── Boolean parsing ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (1.5, True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("y", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("no", False),
        ("", False),
        (None, False),
    ],
)
def test_parse_bool_accepts_every_common_spelling(value, expected):
    """A kill switch must never fail open because of YAML quoting."""
    assert secrets._parse_bool(value) is expected


def test_get_value_returns_none_when_nothing_matches():
    assert secrets._get_value({}, "ABSENT_KEY") is None
