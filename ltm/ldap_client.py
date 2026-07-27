"""LDAP client for fetching the configured population from the directory.

This module is the only place in the codebase that executes an external
subprocess (``ldapsearch``).  All directory interaction is funnelled here so
that the rest of the pipeline works against plain Python dicts and never needs
to know how they were obtained.

Design choices worth knowing:

- We shell out to the system ``ldapsearch`` binary rather than use a Python
  LDAP library.  The binary handles paged results and TLS negotiation
  reliably against every server we have tried, with no build-time dependency
  on OpenLDAP headers.  It must be on ``PATH`` (Debian/Ubuntu: ``ldap-utils``,
  RHEL/Fedora: ``openldap-clients``, macOS: preinstalled).
- The bind password is passed via a temporary file (``-y``) rather than on the
  command line, so it never appears in process listings.  The temp file is
  chmod 0600 *before* the password is written to it.
- Return code 4 means "size limit exceeded".  We treat it as a hard failure
  rather than accepting the partial dataset, because a truncated snapshot
  would make everyone past the limit look like a leaver on the next run.
- On any failure ``fetch_directory_records()`` returns ``{}`` so the pipeline
  can detect the empty result and abort before writing anything.
"""

import logging
import os
import subprocess
import tempfile

from .config import (
    FETCH_ATTRIBUTES,
    LDAP_BASE_DN,
    LDAP_EXTRA_ARGS,
    LDAP_FILTER,
    LDAP_PAGE_SIZE,
    LDAP_SERVER,
    LDAP_START_TLS,
    LDAP_TIMEOUT_SECONDS,
)
from .ldif_parser import parse_ldif_output
from .secrets import get_ldap_credentials


def build_command(bind_user):
    """Assemble the ldapsearch argument list, minus the password file.

    Split out from the fetch so the command can be inspected in tests and
    logged without a live directory.

    Args:
        bind_user: The bind DN or UPN to authenticate as.

    Returns:
        A list of command-line arguments.  The caller appends
        ``["-y", <password file>, <filter>, *attributes]``.
    """
    cmd = ["ldapsearch", "-x", "-H", LDAP_SERVER, "-D", bind_user, "-b", LDAP_BASE_DN]
    if LDAP_START_TLS:
        # -ZZ requires the StartTLS negotiation to succeed rather than
        # silently continuing in the clear.
        cmd.append("-ZZ")
    if LDAP_PAGE_SIZE:
        cmd += ["-E", f"pr={LDAP_PAGE_SIZE}/noprompt"]
    cmd += list(LDAP_EXTRA_ARGS)
    return cmd


def fetch_directory_records():
    """Run ldapsearch and return every matching directory record as a dict.

    Returns a dict of ``{dn: record_dict}`` where each record holds the
    configured attributes for that entry.  Returns ``{}`` on any failure so
    callers can treat an empty dict as a hard error without risking partial
    writes.

    Failure cases handled explicitly:

    - Missing credentials: logged, returns ``{}``.
    - ``subprocess.TimeoutExpired``: the search ran past the configured timeout.
    - ``OSError``: the ldapsearch binary is missing, or the temp file failed.
    - Return code 4 (size limit exceeded): partial data refused.
    - Any other non-zero return code: stderr is logged for diagnosis.
    """
    user, password = get_ldap_credentials()
    if not user:
        logging.error("LDAP credentials unavailable; aborting ldapsearch")
        return {}

    cmd = build_command(user)

    logging.info("Fetching directory records...")
    logging.debug(
        "LDAP query: server=%s base_dn=%s filter=%s attrs=%d",
        LDAP_SERVER,
        LDAP_BASE_DN,
        LDAP_FILTER,
        len(FETCH_ATTRIBUTES),
    )
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as password_file:
            # Restrict permissions before writing the password so it is never
            # readable by other users, even momentarily.
            os.fchmod(password_file.fileno(), 0o600)
            password_file.write(password)
            password_file.flush()
            result = subprocess.run(
                [*cmd, "-y", password_file.name, LDAP_FILTER, *FETCH_ATTRIBUTES],
                capture_output=True,
                text=True,
                timeout=LDAP_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired:
        logging.error("ldapsearch timed out after %ss", LDAP_TIMEOUT_SECONDS)
        return {}
    except OSError as e:
        logging.error("Failed to execute ldapsearch: %s", e)
        return {}

    if result.returncode == 4:
        # Size limit exceeded — the server cut the results short.  Storing an
        # incomplete snapshot would make everyone beyond the limit look like a
        # leaver on the next run, so we refuse the data entirely.
        logging.error("ldapsearch size limit exceeded; refusing to store partial data.")
        return {}
    if result.returncode != 0:
        logging.error(
            "ldapsearch failed (%s): %s", result.returncode, result.stderr.strip()
        )
        return {}

    logging.debug("LDIF fetch complete, %d bytes received.", len(result.stdout))
    return parse_ldif_output(result.stdout)
