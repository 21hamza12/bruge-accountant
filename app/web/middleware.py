"""The one place the middleware stack is built.

Order, outermost first (a request passes top to bottom, a response bottom to top):

1. ``ProxyHeadersMiddleware``: fixes ``scope["client"]`` / ``scope["scheme"]`` from the trusted
   proxy hops, so everything below sees the real client and https.
2. ``RequestLogMiddleware``: assigns the request id, adds ``X-Request-ID`` and logs one line.
3. ``SecurityHeadersMiddleware``: CSP, nosniff, HSTS and friends on every response.
4. ``DefaultCacheControlMiddleware``: ``no-store`` unless a response sets its own.
5. ``CORSMiddleware`` (only when an allowlist is configured).
6. ``AllowedHostMiddleware``: host allowlist; rejects with the custom 400 page.
7. ``ErrorBoundaryMiddleware``: turns an unhandled exception, or an ``HTTPException`` raised by
   a middleware below it, into the custom error page.
8. Anything passed as ``extra_middleware``, then the router.

Why this order: layers 1 to 4 wrap everything that can fail, so a 400 from the host check, a
500 from the boundary and a 403 from a later middleware all still carry the security headers,
the request id and ``no-store``. Starlette's own catch-all sits outside every middleware and
would skip them, which is why the boundary lives inside, not outside.

Design rule: sessions and CSRF (PR 3) are FastAPI dependencies, not middleware. A dependency
runs inside the router, where the exception handlers render the custom pages and the database
session is available. New middleware must go in ``extra_middleware`` (innermost, behind the
boundary) or be added to this list deliberately, updating ``test_middleware_order_is_pinned``.
Never call ``app.add_middleware`` after the app is built: it would become the outermost layer,
outside the request log and the security headers.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from app.observability import RequestLogMiddleware
from app.settings import Settings
from app.web.errors import ErrorBoundaryMiddleware
from app.web.proxy import ProxyHeadersMiddleware
from app.web.security import (
    AllowedHostMiddleware,
    DefaultCacheControlMiddleware,
    SecurityHeadersMiddleware,
)


def build_middleware(
    settings: Settings, extra_middleware: Sequence[Middleware] = ()
) -> list[Middleware]:
    stack: list[Middleware] = [
        Middleware(ProxyHeadersMiddleware, hops=settings.trusted_proxy_hops),
        Middleware(RequestLogMiddleware),
        Middleware(SecurityHeadersMiddleware, hsts=settings.is_production),
        Middleware(DefaultCacheControlMiddleware),
    ]
    if settings.cors_allowed_origins:
        stack.append(
            Middleware(
                CORSMiddleware,
                allow_origins=settings.cors_allowed_origins,
                allow_credentials=True,
                allow_methods=["GET", "POST"],
                allow_headers=["Content-Type", "X-CSRF-Token", "HX-Request"],
            )
        )
    stack.append(Middleware(AllowedHostMiddleware, allowed_hosts=settings.allowed_hosts))
    stack.append(Middleware(ErrorBoundaryMiddleware))
    stack.extend(extra_middleware)
    return stack
