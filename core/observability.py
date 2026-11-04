"""Structured logging with a per-cycle correlation ID."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")


def new_correlation_id() -> str:
    cid = uuid.uuid4().hex[:12]
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str:
    return _correlation_id.get()


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    """Log a named event with structured fields (rendered as JSON keys)."""
    logger.log(level, event, extra={"fields": fields})


class _CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "cid": getattr(record, "correlation_id", "-"),
        }
        payload.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {}) or {}
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        base = (
            f"{datetime.fromtimestamp(record.created).isoformat(timespec='seconds')} "
            f"{record.levelname:<8} [{getattr(record, 'correlation_id', '-')}] "
            f"{record.name}: {record.getMessage()}"
        )
        line = f"{base} {kv}" if kv else base
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(level: str = "INFO", json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_CorrelationFilter())
    handler.setFormatter(JsonFormatter() if json_output else KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "urllib3", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
