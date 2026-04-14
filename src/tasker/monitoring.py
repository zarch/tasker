"""Structured monitoring via structlog — application-level observability.

This module provides a **one-call** setup function that configures structlog
to write human-readable, timestamped, key-value log events to a file.  It
is designed to complement (not replace) the existing JSONL iteration log
(``log.py``) which records only QA↔Dev exchanges.  The monitor log captures
**everything else** — orchestration decisions, recovery escalations,
session rotations, VCS operations, subprocess launches, parser results,
and UI lifecycle events.

Usage::

    from tasker.monitoring import setup_monitoring

    # Call once, early, before any other tasker module logs:
    setup_monitoring("/path/to/tasker.log")

    # Then, in any module:
    import structlog
    log = structlog.get_logger()
    log.info("task.started", task_label="P1.T1", phase="Phase 1 — MVP")

Design Decisions
----------------
* **Human-readable** (not JSON) — the monitor log is meant for developers
  reading with ``tail -f`` or an editor, not for machine parsing.
* **Console also gets output** — ``DEBUG`` and above are mirrored to
  stderr so problems are visible even if the file is not being tailed.
* **Single call-site** — ``setup_monitoring()`` is idempotent; calling it
  multiple times is safe (subsequent calls are no-ops).
* **structlog standard library integration** — we use structlog's built-in
  ``stdlib`` integration so that ``logging.getLogger(__name__)`` calls in
  existing code (VCS backends) are also captured in the monitor log.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

import structlog


# Module-level flag to prevent double-initialization
_configured = False

# Supported log-level names (lowercase) → stdlib constants
_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,  # alias
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "crit": logging.CRITICAL,  # alias
}


def _resolve_level(name: str) -> int:
    """Convert a level name (case-insensitive) to a stdlib logging level.

    Args:
        name: One of ``debug``, ``info``, ``warning``, ``warn``, ``error``, ``critical``, ``crit``.

    Returns:
        The corresponding ``logging`` module constant (e.g. ``logging.INFO``).

    Raises:
        ValueError: If *name* is not a recognised level.
    """
    key = name.strip().lower()
    if key not in _LEVEL_MAP:
        raise ValueError(
            f"Unknown log level '{name}'. Must be one of: {', '.join(sorted(set(_LEVEL_MAP)))}"
        )
    return _LEVEL_MAP[key]


def setup_monitoring(
    log_path: str | Path | None = None,
    console_level: str = "WARNING",
    file_level: str = "DEBUG",
) -> None:
    """Configure structlog for file + console output.

    Call this exactly once, early in application startup (e.g. in
    ``main.py`` before constructing the ``Orchestrator``).

    Args:
        log_path: Path to the monitor log file.  If ``None`` or empty,
            monitoring is set up for console output only (no file).
        console_level: Minimum level for console (stderr) output.
            Defaults to ``"WARNING"`` so the terminal isn't noisy.
        file_level: Minimum level for the log file.  Defaults to
            ``"DEBUG"`` so the file captures everything.
    """
    global _configured
    if _configured:
        return

    # Validate levels early — before setting the idempotency flag
    # so that a ValueError doesn't leave the module in a half-configured state.
    _resolve_level(console_level)
    _resolve_level(file_level)

    _configured = True

    # ── Resolve and prepare the log file ────────────────────────
    if log_path:
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Shared structlog processors ─────────────────────────────
    shared_processors: list[structlog.types.Processor] = [
        # Add a timestamp in ISO-8601 format
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
    ]

    # ── File renderer (human-readable, colored=False for files) ─
    if log_path:
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_path),
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(_resolve_level(file_level))
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=shared_processors,
        )
        file_handler.setFormatter(file_formatter)

    # ── Console renderer (colored, stderr) ──────────────────────
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(_resolve_level(console_level))
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors,
    )
    console_handler.setFormatter(console_formatter)

    # ── Configure the root stdlib logger ────────────────────────
    # This ensures that existing ``logging.getLogger(__name__)`` calls
    # (e.g. in VCS backends) also flow through structlog's formatting.
    root_logger = logging.getLogger()
    # The root logger must accept the most permissive of the two handler
    # levels so that messages are not filtered before reaching the handlers.
    root_logger.setLevel(logging.DEBUG)

    # Remove any default handlers to avoid duplicates
    root_logger.handlers.clear()

    root_logger.addHandler(console_handler)
    if log_path:
        root_logger.addHandler(file_handler)

    # ── Configure structlog itself ──────────────────────────────
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, optionally bound to a module name.

    Convenience wrapper so modules don't need to import structlog
    directly — they can do ``from tasker.monitoring import get_logger``.

    Args:
        name: Logger name (typically ``__name__``).  If ``None``, uses
            the caller's module name via inspection.
    """
    if name is None:
        # Fall back to structlog's default (which uses the calling module)
        return structlog.get_logger()
    return structlog.get_logger(name)
