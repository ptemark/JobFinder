"""Tests for the structured logging setup (LLD §12, task T26).

These assert that :func:`setup_logging` installs a rotating JSON file handler
over ``data/logs/jobfinder.log``, that records actually land in the file as
parseable JSON, and that repeated configuration is idempotent (no duplicate
handlers / lines) — all offline, no network.
"""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from jobfinder.logging_setup import _MANAGED, LOG_FILENAME, setup_logging
from jobfinder.settings import Settings


def _managed_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, _MANAGED, False)]


def _teardown_managed() -> None:
    root = logging.getLogger()
    for handler in _managed_handlers():
        root.removeHandler(handler)
        handler.close()


def test_setup_logging_installs_rotating_file_handler(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path)
    try:
        setup_logging(settings)

        handlers = _managed_handlers()
        rotating = [h for h in handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        handler = rotating[0]
        # Sized per LLD §12: 5 × 1 MB over data/logs/jobfinder.log.
        assert handler.maxBytes == 1_000_000
        assert handler.backupCount == 5
        assert Path(handler.baseFilename) == settings.log_dir / LOG_FILENAME
        assert settings.log_dir.is_dir()  # the log dir was created
    finally:
        _teardown_managed()


def test_setup_logging_writes_json_line_to_file(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path)
    try:
        setup_logging(settings)
        logging.getLogger("jobfinder.test").info("greenhouse funnel: fetched=2")

        log_path = settings.log_dir / LOG_FILENAME
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert lines
        record = json.loads(lines[-1])  # each line is a JSON object
        assert record["level"] == "INFO"
        assert record["logger"] == "jobfinder.test"
        assert record["message"] == "greenhouse funnel: fetched=2"
        assert "ts" in record
    finally:
        _teardown_managed()


def test_setup_logging_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(base_dir=tmp_path)
    try:
        setup_logging(settings)
        setup_logging(settings)
        setup_logging(settings)
        # Three calls, still exactly one console + one file handler (no stacking).
        assert len(_managed_handlers()) == 2

        logging.getLogger("jobfinder.test").info("once")
        log_path = settings.log_dir / LOG_FILENAME
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        # One log call → one line, not duplicated by leftover handlers.
        assert len(lines) == 1
    finally:
        _teardown_managed()
