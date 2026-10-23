"""Structured JSON logging: one line per request, secrets redacted, request id on everything."""

from __future__ import annotations

import contextvars
import copy
import json
import logging
import re
import sys
import time
import uuid
from datetime import UTC, date, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REDACTED = "[REDACTED]"
# Keys whose value is never logged. Broad on purpose: a false positive costs a log field, a
# false negative leaks a credential.
SENSITIVE_KEY = re.compile(
    r"password|passwd|pwd|token|cookie|authorization|auth|csrf|secret|api[_-]?key|"
    r"database[_-]?url|dsn|session|creds|credential|bearer|private",
    re.IGNORECASE,
)
# Secrets that sit inside free text (messages, exception text, string values).
_URL_USERINFO = re.compile(r"(\w[\w+.-]*://)[^/@\s:]+(?::[^/\s]*)?@")
_BEARER = re.compile(r"\b(Bearer|Basic)\s+\S+", re.IGNORECASE)
_KEY_VALUE = re.compile(r"(password|passwd|pwd|token|secret|api[_-]?key)=[^&\s]+", re.IGNORECASE)
# "key": "value" / 'key': 'value' as printed by repr(dict) or json.dumps.
_QUOTED_PAIR = re.compile(
    r"""(?P<key>(?P<q>['"])[\w-]*(?:"""
    + SENSITIVE_KEY.pattern
    + r""")[\w-]*(?P=q)\s*:\s*)(?:'[^']*'|"[^"]*"|[^\s,}\]]+)""",
    re.IGNORECASE,
)
UNMATCHED_ROUTE = "<unmatched>"
STATIC_ROUTE = "static"
HANDLER_MARK = "_bruge_json_handler"
RESERVED_FIELDS = frozenset({"ts", "level", "logger", "message", "request_id", "exception"})
MAX_DEPTH = 8

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


def scrub_text(text: str) -> str:
    """Mask credentials that appear inside free text."""
    text = _URL_USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _QUOTED_PAIR.sub(lambda m: m.group("key") + REDACTED, text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


def _type_name(value: object) -> str:
    return f"<{type(value).__name__}>"


def _key_text(key: object) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bool | int | float) or key is None:
        return str(key)
    return _type_name(key)


def redact(value: Any, key: str | None = None, _depth: int = 0) -> Any:
    """A JSON-safe copy of ``value`` with every secret masked.

    * values under a sensitive key are replaced;
    * strings are scrubbed for URL passwords, bearer tokens and ``password=...`` pairs;
    * ``(sensitive_name, value)`` 2-tuples (header lists) are treated as pairs;
    * sets are walked; unknown objects are logged as their type name, never their repr.
    """
    if key is not None and SENSITIVE_KEY.search(key):
        return REDACTED
    if _depth > MAX_DEPTH:
        return "<truncated>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {_key_text(k): redact(v, _key_text(k), _depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple):
        if len(value) == 2 and isinstance(value[0], str) and SENSITIVE_KEY.search(value[0]):
            return [value[0], REDACTED]
        return [redact(v, None, _depth + 1) for v in value]
    if isinstance(value, set | frozenset):
        items = [redact(v, None, _depth + 1) for v in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
    if isinstance(value, datetime | date | dt_time):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    return _type_name(value)


class RedactionFilter(logging.Filter):
    """Early pass over ``extra`` fields; ``JsonFormatter`` repeats it as the final step."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key in list(record.__dict__):
            if key not in _STANDARD_ATTRS:
                record.__dict__[key] = redact(record.__dict__[key], key)
        return True


def _safe_args(args: Any) -> Any:
    if isinstance(args, dict):
        return redact(args)
    if isinstance(args, tuple):
        return tuple(redact(a) for a in args)
    return args


class JsonFormatter(logging.Formatter):
    """Redaction happens here, last, so it covers every handler path and every field."""

    def format(self, record: logging.LogRecord) -> str:
        safe = copy.copy(record)
        safe.msg = record.msg if isinstance(record.msg, str) else redact(record.msg)
        safe.args = _safe_args(record.args)
        doc: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": scrub_text(safe.getMessage()),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            doc["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and key not in RESERVED_FIELDS:
                doc[key] = redact(value, key)
        if record.exc_info:
            doc["exception"] = scrub_text(self.formatException(record.exc_info))
        if record.stack_info:
            doc["stack"] = scrub_text(self.formatStack(record.stack_info))
        return json.dumps(doc, default=_type_name, ensure_ascii=False)


QUIET_LIBRARIES = ("PIL", "anthropic", "httpcore", "httpcore2")


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
    # At DEBUG these print what they handle: Pillow the EXIF tags of an image (GPS, camera), the
    # Anthropic SDK and httpcore the request body (a base64 screenshot). Never below WARNING.
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)


def _route_label(scope: Scope) -> str:
    """The route template, never the raw path; static files all share one label."""
    path = getattr(scope.get("route"), "path", None)
    if path:
        return str(path)
    if isinstance(scope.get("endpoint"), StaticFiles):
        return STATIC_ROUTE
    return UNMATCHED_ROUTE


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
            route = _route_label(scope)
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
