"""Structured logging setup with a rotating file handler.

Mirrors `app.py:22-46` from v1.0.0:
  - format: `%(asctime)s [%(levelname)s] %(name)s - %(message)s`
  - handlers: StreamHandler (stdout) + RotatingFileHandler
  - rotation: 5 MB max per file, 3 backups kept, UTF-8
  - root level: INFO
  - library quiet-down: `urllib3` -> WARNING (legacy; in this code base
    we use httpx, so we quiet that down instead)
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_DEFAULT_BACKUP_COUNT = 3
_DEFAULT_LEVEL = "INFO"


def configure_logging(
    log_file: str | os.PathLike = "fc_inventory.log",
    log_level: str = _DEFAULT_LEVEL,
    max_bytes: int = _DEFAULT_MAX_BYTES,
    backup_count: int = _DEFAULT_BACKUP_COUNT,
) -> structlog.stdlib.BoundLogger:
    """Wire up the rotating-file logger + the stdout handler.

    Returns the configured structlog bound logger for convenience.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Ensure the directory exists so RotatingFileHandler can open the file.
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Formatter mirrors v1.0.0's plain-text layout. (structlog emits
    # via the stdlib handler so this format string is what shows up.)
    fmt = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    formatter = logging.Formatter(fmt)

    # Stream handler -> stderr (uvicorn captures stderr by default).
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)

    # Rotating file handler.
    rfh = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    rfh.setFormatter(formatter)

    root = logging.getLogger()
    # Reset handlers in case configure_logging is called more than once
    # (e.g. from a test that re-uses the process).
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(stream)
    root.addHandler(rfh)

    # Quiet down chatty third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)

    # Configure structlog to delegate to the stdlib logger. The processor
    # chain adds context (log level, timestamp, stack info) and then
    # `KeyValueRenderer` formats the event_dict into a plain-text line
    # suitable for the rotating file + stderr. Without a renderer the
    # last processor the BoundLogger would see is a dict, which then
    # would fail when forwarded to stdlib `Logger.info(msg)`.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            structlog.processors.KeyValueRenderer(
                key_order=["timestamp", "level", "event"],
                drop_missing=True,
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger()
