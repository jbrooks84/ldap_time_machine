"""Tests for logging setup.

Every test points the handler at a temporary file. Writing to the configured
log path during a test run would interleave test output with real operational
logs, which is exactly the kind of thing that wastes an hour during an incident.
"""

import logging
import os

from ltm import logging_utils


def teardown_function():
    """Detach handlers so a stray file handle cannot leak into the next test."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()


def test_setup_creates_the_log_file(tmp_path):
    log_file = tmp_path / "app.log"
    logging_utils.setup_logging(log_file=str(log_file))
    logging.info("hello")
    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_setup_creates_a_missing_parent_directory(tmp_path):
    log_file = tmp_path / "nested" / "dir" / "app.log"
    logging_utils.setup_logging(log_file=str(log_file))
    assert log_file.exists()


def test_the_log_file_is_owner_only(tmp_path):
    """Directory data is in here; other local users have no business reading it."""
    log_file = tmp_path / "app.log"
    logging_utils.setup_logging(log_file=str(log_file))
    assert oct(os.stat(log_file).st_mode)[-3:] == "600"


def test_setup_is_idempotent(tmp_path):
    """Calling twice must not double every line."""
    log_file = tmp_path / "app.log"
    logging_utils.setup_logging(log_file=str(log_file))
    logging_utils.setup_logging(log_file=str(log_file))
    logging.info("once")
    assert log_file.read_text(encoding="utf-8").count("once") == 1


def test_the_level_is_configurable(tmp_path):
    log_file = tmp_path / "app.log"
    logging_utils.setup_logging(log_file=str(log_file), level="WARNING")
    logging.debug("suppressed")
    logging.warning("kept")
    content = log_file.read_text(encoding="utf-8")
    assert "suppressed" not in content
    assert "kept" in content


def test_an_unknown_level_falls_back_to_debug(tmp_path):
    log_file = tmp_path / "app.log"
    logging_utils.setup_logging(log_file=str(log_file), level="NOT_A_LEVEL")
    assert logging.getLogger().level == logging.DEBUG


def test_noisy_libraries_are_pinned_to_warning(tmp_path):
    """Matplotlib's font lookup would otherwise bury every real log line."""
    logging_utils.setup_logging(log_file=str(tmp_path / "app.log"))
    assert logging.getLogger("matplotlib").level == logging.WARNING
    assert logging.getLogger("PIL").level == logging.WARNING


def test_rotation_is_configured(tmp_path):
    logging_utils.setup_logging(log_file=str(tmp_path / "app.log"), backup_count=7)
    handler = next(
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    )
    assert handler.backupCount == 7
    assert handler.when == "MIDNIGHT"
    assert handler.utc is True


def test_a_console_handler_is_attached_only_for_a_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(logging_utils.sys.stderr, "isatty", lambda: True)
    logging_utils.setup_logging(log_file=str(tmp_path / "tty.log"))
    assert any(type(h) is logging.StreamHandler for h in logging.getLogger().handlers)

    monkeypatch.setattr(logging_utils.sys.stderr, "isatty", lambda: False)
    logging_utils.setup_logging(log_file=str(tmp_path / "notty.log"))
    assert not any(
        type(h) is logging.StreamHandler for h in logging.getLogger().handlers
    )


def test_setup_survives_a_failing_chmod(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(logging_utils.os, "chmod", boom)
    logging_utils.setup_logging(log_file=str(tmp_path / "app.log"))
    logging.info("still works")


def test_a_failing_handler_close_does_not_block_setup(tmp_path, monkeypatch):
    logging_utils.setup_logging(log_file=str(tmp_path / "first.log"))

    handler = logging.getLogger().handlers[0]

    def boom():
        raise OSError("cannot close")

    monkeypatch.setattr(handler, "close", boom)
    logging_utils.setup_logging(log_file=str(tmp_path / "second.log"))
    logging.info("recovered")
    assert (tmp_path / "second.log").exists()
