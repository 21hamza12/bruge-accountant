"""Trust ``X-Forwarded-*`` from exactly N reverse proxies, counted from the right.

Uvicorn's ``--proxy-headers`` only trusts 127.0.0.1 unless told otherwise, which on Render
leaves the scheme at ``http`` and the client at the proxy. This middleware replaces it.

A client controls the left side of ``X-Forwarded-For``; each proxy appends the address it saw
on the right. With N trusted proxies the N-th entry from the right is the real client, and
anything to its left is attacker-controlled and ignored. With N = 0 both headers are ignored.
"""

from __future__ import annotations

import ipaddress

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

VALID_SCHEMES = frozenset({"http", "https"})


def _values(headers: Headers, name: str) -> list[str]:
    """Every comma separated entry over all lines of ``name``, in order, blanks removed."""
    entries: list[str] = []
    for line in headers.getlist(name):
        entries.extend(part.strip() for part in line.split(","))
    return [e for e in entries if e]


def _client_ip(headers: Headers, hops: int) -> str | None:
    entries = _values(headers, "x-forwarded-for")
    if len(entries) < hops:
        return None
    candidate = entries[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _scheme(headers: Headers) -> str | None:
    entries = _values(headers, "x-forwarded-proto")
    if not entries:
        return None
    proto = entries[-1].lower()
    return proto if proto in VALID_SCHEMES else None


class ProxyHeadersMiddleware:
    def __init__(self, app: ASGIApp, hops: int = 0) -> None:
        if hops < 0:
            raise ValueError("hops must be >= 0")
        self.app = app
        self.hops = hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.hops > 0 and scope["type"] == "http":
            headers = Headers(scope=scope)
            ip = _client_ip(headers, self.hops)
            if ip is not None:
                scope["client"] = (ip, 0)
            scheme = _scheme(headers)
            if scheme is not None:
                scope["scheme"] = scheme
        await self.app(scope, receive, send)
