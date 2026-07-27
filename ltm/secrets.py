"""Credential and SMTP settings loader.

Secrets live in a YAML file separate from ``config.yml`` — the directory bind
password and the SMTP password have no business sitting next to settings you
would happily commit.  The path comes from ``paths.credentials_file`` and
defaults to ``~/.config/ldap-time-machine/credentials.yml``.  It should be
mode 0600.

Every value can be overridden at runtime by an ``LTM_<KEY>`` environment
variable, which takes precedence over the file.  That is how CI and secret
managers inject credentials without writing them to disk.

Failure modes:

- A missing or unparseable file logs an error and yields empty values.  The
  pipeline then aborts before contacting anything, rather than proceeding with
  half a configuration.
- ``_SECRETS_CACHE`` avoids re-reading the file within a process.  It is not
  thread-safe by design; this is a single-threaded scheduled job.
"""

import logging
import os
from collections import namedtuple

import yaml

from .config import CREDENTIALS_FILE

_SECRETS_CACHE = None

SmtpSettings = namedtuple(
    "SmtpSettings",
    "server port from_email recipients use_tls username password enabled",
)

# An entirely disabled configuration, returned whenever settings are missing or
# malformed.  ``enabled=False`` is what every caller checks before connecting.
DISABLED_SMTP = SmtpSettings("", 0, "", [], False, None, None, False)


def _load_yaml(path):
    """Read a YAML file and return a dict, or None on any error.

    Covers the three failure modes a new deployment actually hits: the file
    does not exist yet, it cannot be read, or it parses to something that is
    not a mapping (an empty file, or a list from a copy-paste accident).
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logging.error("Credentials file not found: %s", path)
        return None
    except Exception as e:
        logging.error("Failed to load credentials file %s: %s", path, e)
        return None

    if not isinstance(data, dict):
        logging.error("Credentials file %s did not parse to a mapping", path)
        return None
    return data


def load_secrets(path=None):
    """Return the secrets dict, loading and caching on first call.

    Returns an empty dict rather than None when the file is unavailable, so
    callers can use ``.get()`` without guarding.
    """
    global _SECRETS_CACHE
    if path is not None:
        return _load_yaml(path) or {}
    if _SECRETS_CACHE is not None:
        return _SECRETS_CACHE

    _SECRETS_CACHE = _load_yaml(CREDENTIALS_FILE) or {}
    return _SECRETS_CACHE


def reset_cache():
    """Clear the cached secrets.  Used by tests between configurations."""
    global _SECRETS_CACHE
    _SECRETS_CACHE = None


def _get_value(secrets, *keys):
    """Return the first non-empty value across the given key names.

    For each key the lookup order is the ``LTM_<KEY>`` environment variable,
    then the key in the secrets mapping.  Returns None when nothing matches.
    """
    for key in keys:
        env_key = f"LTM_{key}"
        if os.environ.get(env_key) not in (None, ""):
            return os.environ[env_key]
        if secrets.get(key) not in (None, ""):
            return secrets[key]
    return None


def _parse_bool(val):
    """Convert a YAML or environment value to a bool.

    YAML gives real booleans, environment variables give strings, and people
    write ``1``, ``true``, and ``yes`` interchangeably.  Handle all three the
    same way so a kill switch never fails open because of quoting.
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(val)


def get_ldap_credentials():
    """Return (username, password) for binding to the directory.

    Reads ``LDAP_USERNAME`` / ``LDAP_PASSWORD``.  Returns (None, None) and
    logs an error when either is missing — the caller treats a None username
    as a hard stop and returns an empty result rather than attempting an
    anonymous bind that would silently return a different population.
    """
    secrets = load_secrets()
    user = _get_value(secrets, "LDAP_USERNAME", "ldap_username")
    password = _get_value(secrets, "LDAP_PASSWORD", "ldap_password")

    if not user or not password:
        logging.error("Missing LDAP_USERNAME/LDAP_PASSWORD in %s", CREDENTIALS_FILE)
        return None, None

    return user, password


def get_smtp_settings():
    """Return an SmtpSettings tuple describing how to deliver the report.

    ``TO_EMAIL`` is split on commas here so callers get a recipient list
    rather than a string to re-parse.

    An explicit ``SEND_EMAIL: false`` disables delivery quietly — INFO, not
    ERROR — before any other validation, so an email-less deployment with an
    incomplete SMTP section is treated as the documented configuration it is,
    not a misconfiguration.

    Authentication is optional: set ``SMTP_USERNAME`` and ``SMTP_PASSWORD``
    for a relay that requires it, and ``SMTP_USE_TLS`` to negotiate STARTTLS
    after connecting.  An unauthenticated relay on port 25 needs neither.

    Any missing required value disables delivery — ``enabled`` comes back
    False — while the rest of the run continues, so the report is still
    rendered and archived to disk where you can look at it.
    """
    secrets = load_secrets()
    server = _get_value(secrets, "SMTP_SERVER")
    port_raw = _get_value(secrets, "SMTP_PORT")
    from_email = _get_value(secrets, "FROM_EMAIL")
    to_email = _get_value(secrets, "TO_EMAIL")
    send_raw = _get_value(secrets, "SEND_EMAIL")

    if send_raw not in (None, "") and not _parse_bool(send_raw):
        # The operator's master off-switch. An email-less deployment is a
        # documented configuration, not a misconfiguration, so an incomplete
        # SMTP section must not produce ERROR noise when delivery is off.
        logging.info("SEND_EMAIL is false; email delivery disabled.")
        return DISABLED_SMTP

    missing = [
        name
        for name, value in (
            ("SMTP_SERVER", server),
            ("SMTP_PORT", port_raw),
            ("FROM_EMAIL", from_email),
            ("TO_EMAIL", to_email),
            ("SEND_EMAIL", send_raw),
        )
        if value is None or value == ""
    ]
    if missing:
        logging.error(
            "Missing SMTP settings in %s: %s. Email delivery disabled.",
            CREDENTIALS_FILE,
            ", ".join(missing),
        )
        return DISABLED_SMTP

    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        logging.error("Invalid SMTP_PORT value: %s; email delivery disabled", port_raw)
        return DISABLED_SMTP

    recipients = [e.strip() for e in str(to_email).split(",") if e.strip()]
    if not recipients:
        logging.error("TO_EMAIL parsed to no recipients; email delivery disabled")
        return DISABLED_SMTP

    return SmtpSettings(
        server=server,
        port=port,
        from_email=from_email,
        recipients=recipients,
        use_tls=_parse_bool(_get_value(secrets, "SMTP_USE_TLS") or False),
        username=_get_value(secrets, "SMTP_USERNAME"),
        password=_get_value(secrets, "SMTP_PASSWORD"),
        enabled=_parse_bool(send_raw),
    )
