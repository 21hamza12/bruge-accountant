"""Custom error pages: friendly French text and a random reference code, never internals."""

from __future__ import annotations

import logging
import secrets
from collections.abc import MutableMapping
from typing import Any

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app import alerting
from app.observability import request_id_var
from app.web.templating import render

log = logging.getLogger("app.web.errors")

PAGES = {400, 401, 403, 404, 405, 413, 429, 500, 503}
# Only headers that help the client are copied from an HTTPException; nothing else.
FORWARDED_HEADERS = ("allow", "retry-after", "connection")


def _context() -> dict[str, str | None]:
    return {"request_id": request_id_var.get()}


def new_reference() -> str:
    return secrets.token_hex(4).upper()


def page_for(status: int) -> int:
    if status in PAGES:
        return status
    return 500 if status >= 500 else 400


def error_response(status: int, ref: str, headers: dict[str, str] | None = None) -> Response:
    body = render(f"errors/{page_for(status)}.html", ref=ref, status=status)
    out = {"Cache-Control": "no-store", **(headers or {})}
    return HTMLResponse(body, status_code=status, headers=out)


async def http_exception_handler(request: Request, exc: Exception) -> Response:
    ref = new_reference()
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - registered for HTTP only
        return error_response(500, ref)
    kept = {k: v for k, v in (exc.headers or {}).items() if k.lower() in FORWARDED_HEADERS}
    if exc.status_code >= 500:
        log.error("http %s ref=%s", exc.status_code, ref, extra=_context())
    return error_response(exc.status_code, ref, kept)


_RANGE_ERRORS = frozenset({"greater_than_equal", "less_than_equal", "greater_than", "less_than"})


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    """400 for malformed input; 404 for a path id outside its allowed range (no such page)."""
    if isinstance(exc, RequestValidationError):
        errors = exc.errors()
        if errors and all(e["loc"][:1] == ("path",) and e["type"] in _RANGE_ERRORS for e in errors):
            return error_response(404, new_reference())
    return error_response(400, new_reference())


class ResponseException(Exception):  # noqa: N818 - it carries a response, it is not an error
    """Raised by a dependency to answer with a ready-made response (a redirect, a 401 with
    ``HX-Redirect``). Registered as an exception handler, so the response still passes through
    every middleware (security headers, request log)."""

    def __init__(self, response: Response) -> None:
        super().__init__("response")
        self.response = response


async def response_exception_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, ResponseException):
        return exc.response
    return error_response(500, new_reference())  # pragma: no cover - registered for this type


class ErrorBoundaryMiddleware:
    """Turns an unhandled exception into the 500 page.

    Starlette's own catch-all sits outside every middleware, so a 500 it renders would skip
    the security headers and the request log. Catching here, inside them, keeps both.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: MutableMapping[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except StarletteHTTPException as exc:
            # Raised by a middleware: the router's exception handler is not in play there.
            if started:
                raise
            kept = {k: v for k, v in (exc.headers or {}).items() if k.lower() in FORWARDED_HEADERS}
            ref = new_reference()
            if exc.status_code >= 500:
                log.error("http %s ref=%s", exc.status_code, ref, extra=_context())
            await error_response(exc.status_code, ref, kept)(scope, receive, send)
        except Exception as exc:
            ref = new_reference()
            log.error("unhandled exception ref=%s", ref, exc_info=True, extra=_context())
            alerting.capture_exception(exc)
            if started:
                raise
            await error_response(500, ref)(scope, receive, send)
