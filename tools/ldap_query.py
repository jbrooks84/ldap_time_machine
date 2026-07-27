#!/usr/bin/env python3
"""Look up one person's current stored record.

Reads the most recent snapshot from the local database.  No directory access,
so it works offline and cannot be affected by directory availability.

    python3 tools/ldap_query.py jdoe
"""

import argparse
import json
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ltm.config import ATTR, DB_FILE


def inspect_user(identifier, db_path=None):
    """Print the most recent stored record for a username or name.

    Tries the username attribute first because it is unique; falls back to a
    substring match on the display name, which is not.

    Returns True if a record was found.
    """
    db_path = db_path or DB_FILE
    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}", file=sys.stderr)
        return False

    # Read-only via URI so an exploration tool can never take a write lock
    # on — or accidentally modify — the production database it points at.
    uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        c = conn.cursor()
        row = None
        if ATTR.username:
            c.execute(
                "SELECT run_date, data FROM user_history "
                "WHERE json_extract(data, ?) = ? ORDER BY run_date DESC LIMIT 1",
                (f"$.{ATTR.username}", identifier),
            )
            row = c.fetchone()

        if not row:
            name_attr = ATTR.display_name or ATTR.common_name
            if name_attr:
                c.execute(
                    "SELECT run_date, data FROM user_history "
                    "WHERE json_extract(data, ?) LIKE ? "
                    "ORDER BY run_date DESC LIMIT 1",
                    (f"$.{name_attr}", f"%{identifier}%"),
                )
                row = c.fetchone()

        if not row:
            print(f"'{identifier}' not found in the time machine.")
            return False

        print(f"--- Snapshot date: {row[0]} ---")
        print(json.dumps(json.loads(row[1]), indent=4, sort_keys=True))
        return True
    finally:
        conn.close()


def main():
    """Parse arguments and run the lookup."""
    parser = argparse.ArgumentParser(
        description="Look up a directory record in the local snapshot database."
    )
    parser.add_argument("identifier", help="Username, or part of a display name")
    parser.add_argument("--db", default=None, help="Database path override")
    args = parser.parse_args()
    sys.exit(0 if inspect_user(args.identifier, args.db) else 1)


if __name__ == "__main__":
    main()
