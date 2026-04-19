"""Structured logging configuration for sb-stack.

One `configure_logging(settings, process_name)` call per process, at startup.
Every log line uses a snake_case `event` string + typed fields. No freeform
strings — see docs/11_observability_and_testing.md §Log event taxonomy for
the canonical event catalog.

Output fan-out:
  - stdout           (for Docker log collection; toggled by SB_LOG_TO_STDOUT)
  - /data/logs/{process_name}.log (rotated daily, 30-day retention;
                                   toggled by SB_LOG_TO_FILE)
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog

from sb_stack.settings import Settings

_LEVEL_NAMES = {"debug", "info", "warning", "error", "critical"}


def _level_int(name: str) -> int:
    name = name.lower()
    if name not in _LEVEL_NAMES:
        raise ValueError(f"invalid log level: {name!r}")
    value: int = getattr(logging, name.upper())
    return value


def configure_logging(settings: Settings, process_name: str) -> None:
    """Configure structlog + stdlib logging for this process.

    Safe to call once; calling more than once replaces handlers. Not
    thread-safe (no reason it should be — called from main before any work).
    """
    level = _level_int(settings.log_level)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=settings.log_to_stdout)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, httpx, etc.) through the same handlers.
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    if settings.log_to_stdout:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(stdout_handler)

    if settings.log_to_file:
        settings.logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            settings.logs_dir / f"{process_name}.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(file_handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Thin wrapper so callers don't need to import structlog directly."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
