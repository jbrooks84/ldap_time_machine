#!/usr/bin/env python3
"""Print every recorded attribute change for one person.

Resolves a username to its most recent DN, then walks the ``changes`` table
for that DN.

A DN is not stable: moving between organisational units rewrites it, and the
change history is keyed by DN.  So when the current DN has no history, this
looks for the person's older DNs and reports them rather than claiming
nothing ever changed — a silent empty result there would be actively
misleading.

    python3 tools/ldap_history_query.py jdoe
"""

import argparse
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ltm.config import ATTR, DB_FILE


def get_all_dns(conn, username):
    """Return every DN ever recorded for a username, most recent first."""
    if not ATTR.username:
        return []
    c = conn.cursor()
    c.execute(
        "SELECT dn, MAX(run_date) AS last_seen FROM user_history "
        "WHERE json_extract(data, ?) = ? GROUP BY dn ORDER BY last_seen DESC",
        (f"$.{ATTR.username}", username),
    )
    return [row[0] for row in c.fetchall()]


def print_changes(conn, dn):
    """Print the change log for one DN.  Returns the number of rows printed."""
    c = conn.cursor()
    c.execute(
        "SELECT run_date, attribute, old_val, new_val FROM changes "
        "WHERE dn = ? ORDER BY run_date ASC",
        (dn,),
    )
    rows = c.fetchall()
    if not rows:
        return 0

    width = max(len(row[1]) for row in rows)
    for date, attribute, old_val, new_val in rows:
        old_display = (old_val or "").replace("\n", " ").strip()
        new_display = (new_val or "").replace("\n", " ").strip()
        print(f"[{date}] {attribute:<{width}} | {old_display} -> {new_display}")
    return len(rows)


def trace(username, db_path=None):
    """Print the full change history for a username.  Returns True if found."""
    db_path = db_path or DB_FILE
    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        return False

    # Read-only via URI so an exploration tool can never take a write lock
    # on — or accidentally modify — the production database it points at.
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        dns = get_all_dns(conn, username)
        if not dns:
            print(f"'{username}' not found in user_history.")
            return False

        total = 0
        for dn in dns:
            count = print_changes(conn, dn)
            if count:
                print(f"  ^ {count} change(s) under {dn}\n")
            total += count

        if total == 0:
            print(f"No attribute changes recorded for '{username}'.")
        else:
            print(f"Total: {total} change(s) across {len(dns)} DN(s).")
        if len(dns) > 1:
            print(f"Note: {username} has moved between {len(dns)} DNs.")
        return True
    finally:
        conn.close()


def main():
    """Parse arguments and run the trace."""
    parser = argparse.ArgumentParser(
        description="Show every recorded attribute change for one directory entry."
    )
    parser.add_argument("username", help="The username to trace")
    parser.add_argument("--db", default=None, help="Database path override")
    args = parser.parse_args()
    sys.exit(0 if trace(args.username.strip(), args.db) else 1)


if __name__ == "__main__":
    main()
