"""Security headers and the cross-origin / host allowlists."""

from __future__ import annotations

from collections.abc import Sequence

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.web.errors import error_response, new_reference

CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
PERMISSIONS_POLICY = "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Adds the static security headers to every HTTP response, error pages included."""

    def __init__(self, app: ASGIApp, hsts: bool = False) -> None:
        self.app = app
        self.headers: dict[str, str] = {
            "Content-Security-Policy": CSP,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "Permissions-Policy": PERMISSIONS_POLICY,
        }
        if hsts:
            self.headers["Strict-Transport-Security"] = HSTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _hostname(host_header: str) -> str:
    """The host without its port, lower-cased; IPv6 literals keep their brackets."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1] if "]" in host else host
    return host.split(":")[0]


def host_allowed(host_header: str, allowed_hosts: Sequence[str]) -> bool:
    host = _hostname(host_header)
    if not host:
        return False
    for pattern in allowed_hosts:
        pattern = pattern.lower()
        if pattern == "*" or host == pattern:
            return True
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
    return False


class AllowedHostMiddleware:
    """Host allowlist (development may use ``*.x``; production forbids wildcards).

    Starlette's TrustedHostMiddleware answers with a bare text "Invalid host header". This one
    renders the same friendly 400 page as every other error, so nothing differs on the wire.
    """

    def __init__(self, app: ASGIApp, allowed_hosts: Sequence[str]) -> None:
        self.app = app
        self.allowed_hosts = list(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if host_allowed(Headers(scope=scope).get("host", ""), self.allowed_hosts):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await error_response(400, new_reference())(scope, receive, send)


class DefaultCacheControlMiddleware:
    """``Cache-Control: no-store`` unless the response chose its own (static files, /sw.js).

    Pages will hold tenant data, so the safe default is that nothing is stored.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_default(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, send_with_default)


__all__ = [
    "CSP",
    "PERMISSIONS_POLICY",
    "AllowedHostMiddleware",
    "DefaultCacheControlMiddleware",
    "SecurityHeadersMiddleware",
    "host_allowed",
]
