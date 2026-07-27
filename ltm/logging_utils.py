"""Logging setup.

Every module logs through the root logger configured here.  The handler is a
``TimedRotatingFileHandler`` that rolls at midnight UTC, keeps
``logging.backup_count`` daily backups, and does *not* compress them — so the
next person debugging a week-old run can grep across rotated files without
unpacking anything first.  Rotated files are named
``ldap_time_machine.log.YYYY-MM-DD``.

Console output is attached only when stderr is a TTY.  Interactive runs see
log lines as they happen; scheduled runs write to the file alone rather than
generating mail on every execution.

Matplotlib and PIL are pinned to WARNING: matplotlib's font lookup emits
thousands of DEBUG lines per run and would bury everything else.
"""

import contextlib
import logging
import logging.handlers
import os
import sys

from .config import LOG_BACKUP_COUNT, LOG_FILE, LOG_LEVEL


def setup_logging(log_file=None, level=None, backup_count=None):
    """Configure the root logger.

    Idempotent — safe to call repeatedly in one process.  Existing handlers are
    removed and closed before new ones are attached, which matters for tests
    and for any path that may invoke ``run()`` more than once.

    Args:
        log_file: Path override, mainly for tests.
        level: Level name override (e.g. "INFO").
        backup_count: How many rotated files to keep.
    """
    log_file = log_file or LOG_FILE
    level = level or LOG_LEVEL
    backup_count = LOG_BACKUP_COUNT if backup_count is None else backup_count

    logger = logging.getLogger()
    logger.setLevel(getattr(logging, str(level).upper(), logging.DEBUG))

    # Clear handlers from a prior call so we do not double-log.
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        # Closing a stale handler must never block a new run.
        with contextlib.suppress(Exception):
            handler.close()

    directory = os.path.dirname(log_file)
    if directory:
        os.makedirs(directory, exist_ok=True)

    # utc=True keeps the rotation suffix in the same reference frame as a UTC
    # schedule, so it does not drift across daylight-saving transitions.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")
    )
    logger.addHandler(file_handler)

    # The permissions may already be right, or the filesystem may not support
    # chmod.  Neither is worth failing a run over.
    with contextlib.suppress(OSError):
        os.chmod(log_file, 0o600)

    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    if sys.stderr.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(
            logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(console_handler)

    # Route Python warnings into the same stream so they are not lost on a
    # stderr nobody is reading.
    logging.captureWarnings(True)
