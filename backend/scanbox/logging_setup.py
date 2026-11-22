"""structlog configuration: JSON to stdout + rotating file in the data dir."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections import deque
from pathlib import Path

import structlog

# Recent warnings/errors for the admin diagnostics page.
RECENT_ERRORS: deque[dict] = deque(maxlen=100)


def _capture_errors(_logger, method_name, event_dict):  # noqa: ANN001
    if method_name in ("warning", "error", "critical", "exception"):
        entry = {k: (v if isinstance(v, str | int | float | bool | type(None)) else str(v)) for k, v in event_dict.items()}
        entry["level"] = method_name
        RECENT_ERRORS.append(entry)
    return event_dict


def setup_logging(level: str = "INFO", json: bool = True, log_dir: Path | None = None) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / "scanbox.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        h.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(h)
    root.setLevel(lvl)
    for noisy in ("httpx", "httpcore", "multipart", "PIL", "ocrmypdf", "pikepdf"):
        logging.getLogger(noisy).setLevel(max(lvl, logging.WARNING))

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _capture_errors,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
