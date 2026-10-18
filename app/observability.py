"""Structured JSON logging: one line per request, secrets redacted, request id on everything."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"password|token|cookie|authorization|csrf|secret|set-cookie", re.IGNORECASE
)
UNMATCHED_ROUTE = "<unmatched>"
HANDLER_MARK = "_bruge_json_handler"

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
request_log = logging.getLogger("app.request")

# Attributes every LogRecord has; anything else came in through ``extra=`` and is data.
_STANDARD_ATTRS = frozenset(logging.LogRecord("x", logging.INFO, "x", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def redact(value: Any, key: str | None = None) -> Any:
    """A copy of ``value`` with the value of every sensitive key replaced."""
    if key is not None and SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key not in _STANDARD_ATTRS:
                record.__dict__[key] = redact(record.__dict__[key], key)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            doc["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                doc[key] = value
        if record.exc_info:
            doc["exception"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=repr, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """JSON lines on stdout. Safe to call repeatedly; uvicorn's access log is turned off
    because it prints the raw URL (query string included)."""
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, HANDLER_MARK, False)]:
        root.removeHandler(existing)
    handler = logging.StreamHandler(sys.stdout)
    setattr(handler, HANDLER_MARK, True)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("uvicorn.access").disabled = True


class RequestLogMiddleware:
    """Outermost layer: assigns the request id and logs one line when the response is done."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = getattr(scope.get("route"), "path", None) or UNMATCHED_ROUTE
            request_log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": route,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)
