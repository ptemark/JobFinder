"""Structured logging configuration for Job Finder (LLD §12).

:func:`setup_logging` wires the root logger to a console handler and a
:class:`~logging.handlers.RotatingFileHandler` writing JSON lines to
``data/logs/jobfinder.log`` (5 × 1 MB), so every poll's per-source count funnel
(``fetched → kept_after_recency → eligible → scored``, emitted by the pipeline)
is captured both on the terminal and on disk for after-the-fact debugging.

The function is idempotent: repeated calls (successive CLI invocations, the
spawned poll process) reconfigure rather than stack duplicate handlers.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jobfinder.settings import Settings

# Rotating file handler sizing (LLD §12: "RotatingFileHandler, 5 × 1 MB").
LOG_FILENAME = "jobfinder.log"
_MAX_BYTES = 1_000_000  # 1 MB per file
_BACKUP_COUNT = 5  # keep 5 rotated files

# Marks the handlers this module installs so a repeated setup_logging call can
# remove exactly its own handlers without disturbing any added elsewhere (e.g.
# pytest's caplog), keeping reconfiguration idempotent rather than additive.
_MANAGED = "_jobfinder_managed"


class _JsonFormatter(logging.Formatter):
    """Render each record as a single JSON object (the LLD §12 JSON-ish line).

    One self-contained line per record keeps the on-disk log greppable and
    machine-parseable; the exception text is folded in when present so a bulkhead
    ``log.exception`` is fully captured.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(settings: Settings, *, level: int = logging.INFO) -> None:
    """Configure root logging to console + a rotating JSON file (LLD §12).

    Creates ``settings.log_dir`` if absent and installs a console
    :class:`~logging.StreamHandler` and a
    :class:`~logging.handlers.RotatingFileHandler` over
    ``settings.log_dir / jobfinder.log``. Idempotent: handlers installed by a
    previous call are removed first, so calling this per CLI command (or in the
    spawned poll process) never duplicates log lines.
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in [h for h in root.handlers if getattr(h, _MANAGED, False)]:
        root.removeHandler(handler)
        handler.close()

    formatter = _JsonFormatter()
    console: logging.Handler = logging.StreamHandler()
    file_handler: logging.Handler = RotatingFileHandler(
        settings.log_dir / LOG_FILENAME,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    for handler in (console, file_handler):
        handler.setLevel(level)
        handler.setFormatter(formatter)
        setattr(handler, _MANAGED, True)
        root.addHandler(handler)


__all__ = ["LOG_FILENAME", "setup_logging"]
