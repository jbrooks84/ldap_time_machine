#!/usr/bin/env python3
"""Ad-hoc directory queries using the configured service account.

A thin, safe wrapper around ``ldapsearch`` for interactive investigation.
Reuses the same connection settings and credentials as the daily pipeline, so
there is no second place to configure and no chance of querying a different
server than the one the reports come from.

The person lookups and ``--stats`` follow the configured attribute map, so
they work on any flavor.  The *group* queries (``-g``, ``-m``) use Active
Directory conventions — ``objectClass=group`` and ``distinguishedName`` —
and will come back empty on OpenLDAP-style servers, whose groups are
usually ``groupOfNames``; adapt those filters if you need them there.

    python3 tools/ldap_explorer.py -u jdoe --full
    python3 tools/ldap_explorer.py -g engineering
    python3 tools/ldap_explorer.py --stats
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ltm.config import ATTR, LDAP_BASE_DN, LDAP_TIMEOUT_SECONDS
from ltm.ldap_client import build_command
from ltm.secrets import get_ldap_credentials


def run_query(filter_str, attributes=None, quiet=False):
    """Run one ldapsearch query and return stdout, or None on failure.

    The password goes into a 0600 temporary file rather than the command line,
    for the same reason the pipeline does it: arguments are visible to every
    process on the host.
    """
    user, password = get_ldap_credentials()
    if not user:
        return None

    cmd = build_command(user)
    if not quiet:
        print("Executing LDAP query...", file=sys.stderr)

    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as password_file:
            os.fchmod(password_file.fileno(), 0o600)
            password_file.write(password)
            password_file.flush()
            full_cmd = [*cmd, "-y", password_file.name, filter_str]
            if attributes:
                full_cmd.extend(attributes)
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=LDAP_TIMEOUT_SECONDS,
            )
    except subprocess.TimeoutExpired:
        print("Error: LDAP query timed out.", file=sys.stderr)
        return None
    except OSError as e:
        print(f"Error: could not run ldapsearch: {e}", file=sys.stderr)
        return None

    if result.returncode == 4 and not quiet:
        print("Warning: size limit exceeded; results are truncated.", file=sys.stderr)
    elif result.returncode not in (0, 4):
        print(f"Error executing query: {result.stderr}", file=sys.stderr)
        return None
    return result.stdout


def _person_filter(term):
    """Build a filter matching a person by username or common name."""
    clauses = []
    if ATTR.username:
        clauses.append(f"({ATTR.username}={term})")
    if ATTR.common_name:
        clauses.append(f"({ATTR.common_name}={term})")
    inner = (
        f"(|{''.join(clauses)})"
        if len(clauses) > 1
        else (clauses[0] if clauses else "")
    )
    return f"(&(objectClass=person){inner})"


def search_user(term, full_info=False):
    """Look up one person by username or common name."""
    print(f"--- Searching for: {term} ---")
    roles = ("username", "email", "job_title", "department", "country", "city")
    attributes = None if full_info else ["dn", *ATTR.names(roles)]
    output = run_query(_person_filter(term), attributes)
    if not output:
        return

    if not full_info:
        print(output)
        return

    print("\n--- Full attribute dump ---")
    for line in output.splitlines():
        if ": " in line and not line.startswith("#"):
            key, value = line.split(": ", 1)
            print(f"{key:<30}: {value}")


def search_group(name):
    """Find groups whose common name contains the given substring."""
    print(f"--- Searching for group: {name} ---")
    cn = ATTR.common_name or "cn"
    output = run_query(f"(&(objectClass=group)({cn}=*{name}*))")
    if output:
        print(output)


def list_members(group_name):
    """List the members of one group."""
    print(f"--- Members of group: {group_name} ---")
    cn = ATTR.common_name or "cn"
    output = run_query(f"(&(objectClass=group)({cn}={group_name}))", ["dn"])
    if not output:
        return

    dns = [
        line.split("dn: ", 1)[1]
        for line in output.splitlines()
        if line.startswith("dn: ")
    ]
    if not dns:
        print(f"Group '{group_name}' not found.")
        return

    print(f"Group DN: {dns[0]}\n")
    members = run_query(f"(distinguishedName={dns[0]})", ["member"])
    count = 0
    for line in (members or "").splitlines():
        if line.startswith("member: "):
            member_dn = line.split("member: ", 1)[1]
            match = re.search(r"CN=([^,]+)", member_dn)
            print(f" - {match.group(1) if match else member_dn}")
            count += 1
    print(f"\nTotal members: {count}")


def list_user_groups(username):
    """List the groups one person belongs to."""
    print(f"--- Groups for: {username} ---")
    if not ATTR.groups:
        print("No group-membership attribute is configured.")
        return

    output = run_query(_person_filter(username), [ATTR.groups])
    prefix = f"{ATTR.groups}: "
    count = 0
    for line in (output or "").splitlines():
        if line.startswith(prefix):
            group_dn = line.split(prefix, 1)[1]
            match = re.search(r"CN=([^,]+)", group_dn)
            print(f" - {match.group(1) if match else group_dn}")
            count += 1
    print(f"\nTotal groups: {count}")


def show_stats():
    """Print a country and city breakdown of all person entries."""
    print(f"--- Demographics under {LDAP_BASE_DN} ---")
    print("Fetching all person objects; this may take a moment.")

    country = ATTR.country
    city = ATTR.city
    attributes = [a for a in (country, city) if a]
    if not attributes:
        print("Neither a country nor a city attribute is configured.")
        return

    output = run_query("(objectClass=person)", attributes)
    if not output:
        print("No data returned.")
        return

    total = 0
    counters = {attribute: Counter() for attribute in attributes}
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("dn:"):
            total += 1
            continue
        for attribute in attributes:
            if line.startswith(f"{attribute}: "):
                counters[attribute][line.split(": ", 1)[1]] += 1

    print(f"\nTotal entries: {total}")
    for attribute in attributes:
        known = sum(counters[attribute].values())
        print(f"\n--- Breakdown by {attribute} ---")
        for value, count in counters[attribute].most_common(15):
            print(f"{value:<25}: {count}")
        print(f"{'Unknown/not set':<25}: {total - known}")


def main():
    """Parse arguments and dispatch to the requested query."""
    parser = argparse.ArgumentParser(
        description="Ad-hoc LDAP queries using the configured service account."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-u", "--user", help="Search for a person")
    group.add_argument("-g", "--group", help="Search for a group")
    group.add_argument("-m", "--members", help="List the members of a group")
    group.add_argument("--user-groups", help="List the groups a person belongs to")
    group.add_argument(
        "-s", "--stats", action="store_true", help="Country and city breakdown"
    )
    parser.add_argument(
        "--full", action="store_true", help="Dump every attribute (with --user)"
    )
    args = parser.parse_args()

    if args.user:
        search_user(args.user, args.full)
    elif args.group:
        search_group(args.group)
    elif args.members:
        list_members(args.members)
    elif args.user_groups:
        list_user_groups(args.user_groups)
    elif args.stats:
        show_stats()


if __name__ == "__main__":
    main()
