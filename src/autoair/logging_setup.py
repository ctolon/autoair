"""Logging of the standalone server (``logging.*`` settings).

``autoair serve`` / ``autoair local`` configure the root logger (level, text or JSON lines)
and per-logger levels; inside Airflow the plugin only applies ``logging.levels`` — Airflow
owns handlers and formats there.
"""

from __future__ import annotations

import datetime
import json
import logging
import logging.config
from typing import Any

from autoair.sections import LoggingSettings


class JsonFormatter(logging.Formatter):
    """One JSON object per line: timestamp, level, logger, message (+ exception, extras)."""

    _STANDARD = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            # exc_text: already formatted (and secrets masked, autoair.secret_mask).
            out["exception"] = record.exc_text or self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in self._STANDARD and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                out[key] = value
        return json.dumps(out, ensure_ascii=False)


def logging_config(settings: LoggingSettings) -> dict[str, Any]:
    """A ``logging.config.dictConfig`` for the standalone server (uvicorn's loggers
    included, so access and error logs use the same format)."""
    formatter: dict[str, Any]
    if settings.format == "json":
        formatter = {"()": f"{__name__}.JsonFormatter"}
    else:
        formatter = {"format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s"}
    loggers: dict[str, Any] = {
        name: {"handlers": [], "level": settings.level, "propagate": True}
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    for name, level in settings.levels.items():
        loggers.setdefault(name, {"propagate": True})["level"] = level
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {"default": formatter},
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            }
        },
        "root": {"handlers": ["default"], "level": settings.level},
        "loggers": loggers,
    }


def configure_logging(settings: LoggingSettings) -> None:
    """Configure the process' logging (standalone server)."""
    logging.config.dictConfig(logging_config(settings))


def apply_levels(settings: LoggingSettings) -> None:
    """Only the per-logger levels (inside Airflow, which owns handlers and formats)."""
    for name, level in settings.levels.items():
        logging.getLogger(name).setLevel(level)


__all__ = ["JsonFormatter", "apply_levels", "configure_logging", "logging_config"]
