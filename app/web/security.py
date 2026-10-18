"""Security headers and the cross-origin / host allowlists."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.settings import Settings

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


def add_security_middleware(app: FastAPI, settings: Settings) -> None:
    """Call after ErrorBoundaryMiddleware: the last middleware added is the outermost."""
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    if settings.cors_allowed_origins:
        cors: dict[str, Any] = {
            "allow_origins": settings.cors_allowed_origins,
            "allow_credentials": True,
            "allow_methods": ["GET", "POST"],
            "allow_headers": ["Content-Type", "X-CSRF-Token", "HX-Request"],
        }
        app.add_middleware(CORSMiddleware, **cors)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)


__all__ = ["CSP", "PERMISSIONS_POLICY", "SecurityHeadersMiddleware", "add_security_middleware"]
