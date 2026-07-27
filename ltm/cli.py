"""The command-line interface.

Lives inside the package so the installed console script and a repository
checkout share one implementation. A wheel install has no repository root
scripts — an entry point that skipped argparse silently turned
``ldap-time-machine --dry-run`` into a production run, which is exactly the
kind of surprise a verification flag must never spring.

Three modes (as the ``ldap-time-machine`` console script, or equivalently the
repo-root ``ldap_time_machine.py`` shim):

* ``ldap-time-machine`` — production.  Fetches the directory, writes to the
  live database, and sends the report to the configured recipients.
* ``ldap-time-machine --dry-run`` — verification.  Snapshots the live
  database to a throwaway copy, redirects all writes there, skips SMTP
  entirely, and archives the rendered HTML under a ``dryrun_`` prefix where it
  cannot evict real reports.
* ``ldap-time-machine --replay YYYY-MM-DD`` — investigation.  Re-renders the
  report a past run date would have produced, from stored history alone: no
  directory fetch, no snapshot write, no mail.  Archived to the dry-run
  bucket.  This is how to answer "why did last Tuesday's report say that?"
  months after the fact.

Run ``--dry-run`` before letting any change reach the scheduled job.  It
exercises the whole pipeline, including the real directory fetch, without
touching the live database or anyone's inbox.

Logging is configured before any work happens, so that even ``--dry-run``
setup failures land in the log file rather than a discarded stderr.  Any
unhandled exception is logged with a full traceback before being re-raised:
scheduled runs that crash need to leave a breadcrumb somewhere.
"""

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime

from .config import DB_FILE, DRY_RUN_DB_FILE, VERSION
from .logging_utils import setup_logging
from .pipeline import replay, run


def _snapshot_db(src, dst):
    """Copy the live database to dst for a dry run, without disturbing it.

    Uses ``VACUUM INTO``, which produces a compacted, transactionally
    consistent copy while the source stays open and readable.  Copying the
    file directly is the classic way to corrupt a SQLite backup: a plain
    ``cp`` of a live database can capture a torn page mid-write, and the
    WAL sidecar holds committed data the main file does not have yet.

    Falls back to a checkpoint-then-copy on SQLite older than 3.27, where
    ``VACUUM INTO`` does not exist.  That path forces the WAL back into the
    main file first, so the copy is at least self-consistent.

    Args:
        src: Path to the live database.  Must exist.
        dst: Destination path.  Replaced if present.

    Exits with status 1 if src is missing.
    """
    if not os.path.exists(src):
        logging.error("Live database not found at %s — nothing to copy.", src)
        sys.exit(1)

    # VACUUM INTO refuses to overwrite, and stale sidecars from a previous run
    # would otherwise be mistaken for this copy's own.
    for path in (dst, dst + "-wal", dst + "-shm"):
        if os.path.exists(path):
            os.remove(path)

    # Autocommit mode, defensively: nothing here writes before the VACUUM,
    # so the driver's implicit transaction never opens today — but VACUUM
    # refuses to run inside a transaction, so the first person to add a DML
    # statement above it would hit a confusing OperationalError.
    conn = sqlite3.connect(src, isolation_level=None)
    try:
        try:
            conn.execute("VACUUM INTO ?", (dst,))
            logging.info("Dry-run database ready at %s (VACUUM INTO)", dst)
        except sqlite3.OperationalError as e:
            logging.warning("VACUUM INTO unavailable (%s); falling back to copy.", e)
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            shutil.copy2(src, dst)
            logging.info("Dry-run database ready at %s (checkpoint + copy)", dst)
    finally:
        conn.close()

    os.chmod(dst, 0o600)


def _cleanup_dry_run_db():
    """Remove the dry-run database copy and its sidecars.

    The copy is the size of the live database — at large scale that is many
    gigabytes parked in the temp directory per dry run. It is removed only
    after a *successful* dry run: after a failure it is deliberately left in
    place for post-mortem, and the next dry run overwrites it anyway.
    """
    removed = False
    for suffix in ("", "-wal", "-shm"):
        path = DRY_RUN_DB_FILE + suffix
        if os.path.exists(path):
            os.remove(path)
            removed = True
    if removed:
        logging.info("Removed dry-run database copy at %s", DRY_RUN_DB_FILE)


def _valid_date(value):
    """argparse type for --replay: insist on a real YYYY-MM-DD date."""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a YYYY-MM-DD date"
        ) from None
    return value


def main():
    """Parse arguments, set up logging, and dispatch to the pipeline."""
    parser = argparse.ArgumentParser(
        prog="ldap-time-machine",
        description="Snapshot an LDAP directory daily and report what changed.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Copy the database aside, write only there, and skip sending mail.",
    )
    mode.add_argument(
        "--replay",
        metavar="YYYY-MM-DD",
        type=_valid_date,
        help="Re-render the report for a past run date from stored history; "
        "archived alongside dry runs, never emailed.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args()

    setup_logging()

    try:
        if args.replay:
            replay(args.replay)
        elif args.dry_run:
            _snapshot_db(DB_FILE, DRY_RUN_DB_FILE)
            run(db_path=DRY_RUN_DB_FILE, send_email=False)
            _cleanup_dry_run_db()
        else:
            run()
    except SystemExit:
        # sys.exit calls inside the pipeline already logged their reason; let
        # them set the exit code without a redundant "unhandled error" line.
        raise
    except Exception:
        logging.exception("Unhandled error reached the entry point")
        raise


if __name__ == "__main__":
    main()
