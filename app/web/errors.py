"""Custom error pages: friendly French text and a random reference code, never internals."""

from __future__ import annotations

import logging
import secrets
from collections.abc import MutableMapping
from typing import Any

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.web.templating import render

log = logging.getLogger("app.web.errors")

PAGES = {400, 401, 403, 404, 429, 500}
# Only headers that help the client are copied from an HTTPException; nothing else.
FORWARDED_HEADERS = ("allow", "retry-after")


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
        log.error("http %s ref=%s", exc.status_code, ref)
    return error_response(exc.status_code, ref, kept)


async def validation_exception_handler(request: Request, exc: Exception) -> Response:
    return error_response(400, new_reference())


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
        except Exception:
            ref = new_reference()
            log.error("unhandled exception ref=%s", ref, exc_info=True)
            if started:
                raise
            await error_response(500, ref)(scope, receive, send)
