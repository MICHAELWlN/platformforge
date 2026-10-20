"""Structured JSON logging to stdout."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from opentelemetry import trace

from pf_service_common.requestid import request_id_var

LOG_LEVELS = ("debug", "info", "warning", "error")

_factory_installed = False


def install_request_id_record_factory() -> None:
    """Stamp every LogRecord with the request ID active when it was created.

    Stamping at creation (not at format time) keeps the ID correct even when records are
    handled later, e.g. by queue handlers or pytest's log capture.
    """
    global _factory_installed
    if _factory_installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.request_id = request_id_var.get()
        # Stamp the span that is current at creation time, same reason as the request ID.
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            record.trace_id = format(context.trace_id, "032x")
            record.span_id = format(context.span_id, "016x")
        else:
            record.trace_id = None
            record.span_id = None
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        trace_id = getattr(record, "trace_id", None)
        span_id = getattr(record, "span_id", None)
        if trace_id and span_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: str = "info") -> None:
    """Route all logging (including uvicorn's) through one JSON handler on stdout."""
    if level not in LOG_LEVELS:
        raise ValueError(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    install_request_id_record_factory()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # Access logging is done by RequestContextMiddleware, which includes the request ID.
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
