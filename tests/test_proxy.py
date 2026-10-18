"""X-Forwarded-* handling: only the rightmost N entries are trusted, never the client's own."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.web.proxy import ProxyHeadersMiddleware


def build(hops: int) -> FastAPI:
    app = FastAPI()

    @app.get("/who")
    def who(request: Request) -> dict[str, Any]:
        client = request.client
        return {
            "host": client.host if client else None,
            "scheme": request.url.scheme,
            "scope_scheme": request.scope["scheme"],
        }

    app.add_middleware(ProxyHeadersMiddleware, hops=hops)
    return app


def get(hops: int, **headers: str) -> dict[str, Any]:
    with TestClient(build(hops), base_url="http://testserver") as c:
        out: dict[str, Any] = c.get("/who", headers=headers).json()
    return out


def xff(value: str) -> dict[str, str]:
    return {"X-Forwarded-For": value}


def test_spoofed_leftmost_entry_is_never_used() -> None:
    out = get(1, **xff("6.6.6.6, 203.0.113.9"))
    assert out["host"] == "203.0.113.9"


def test_hops_one_picks_the_rightmost_entry() -> None:
    assert get(1, **xff("203.0.113.9"))["host"] == "203.0.113.9"
    assert get(1, **xff("1.1.1.1, 2.2.2.2, 3.3.3.3"))["host"] == "3.3.3.3"


def test_hops_two_picks_the_second_from_the_right() -> None:
    assert get(2, **xff("6.6.6.6, 198.51.100.7, 10.0.0.1"))["host"] == "198.51.100.7"


def test_fewer_entries_than_hops_keeps_the_socket_peer() -> None:
    out = get(3, **xff("1.1.1.1, 2.2.2.2"))
    assert out["host"] == "testclient"


def test_hops_zero_ignores_both_headers() -> None:
    out = get(0, **xff("203.0.113.9"), **{"X-Forwarded-Proto": "https"})
    assert out["host"] == "testclient"
    assert out["scheme"] == "http"


def test_multiple_header_lines_are_joined_in_order() -> None:
    with TestClient(build(1), base_url="http://testserver") as c:
        resp = c.get(
            "/who",
            headers=[("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "203.0.113.9")],
        )
    assert resp.json()["host"] == "203.0.113.9"


@pytest.mark.parametrize("bad", ["not-an-ip", "", "1.1.1.1:99999999", "<script>"])
def test_garbage_forwarded_for_keeps_the_socket_peer(bad: str) -> None:
    assert get(1, **xff(bad))["host"] == "testclient"


def test_ipv6_entry_is_accepted() -> None:
    assert get(1, **xff("2001:db8::1"))["host"] == "2001:db8::1"


def test_scheme_becomes_https_from_the_rightmost_proto() -> None:
    out = get(1, **{"X-Forwarded-Proto": "https"})
    assert out["scheme"] == "https"
    assert out["scope_scheme"] == "https"
    assert get(1, **{"X-Forwarded-Proto": "http, https"})["scheme"] == "https"
    assert get(1, **{"X-Forwarded-Proto": "https, http"})["scheme"] == "http"


@pytest.mark.parametrize("bad", ["ftp", "javascript", "HTTPS://x", "", "wss"])
def test_a_bad_proto_is_ignored(bad: str) -> None:
    assert get(1, **{"X-Forwarded-Proto": bad})["scheme"] == "http"


def test_proto_is_case_insensitive_for_the_two_valid_values() -> None:
    assert get(1, **{"X-Forwarded-Proto": "HTTPS"})["scheme"] == "https"


def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    asyncio.run(ProxyHeadersMiddleware(inner, hops=1)({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    assert seen == ["lifespan"]


def test_app_wires_the_middleware_from_settings(web_settings: Any) -> None:
    from app.web.main import create_app

    def build_app(hops: int) -> FastAPI:
        app = create_app(web_settings.model_copy(update={"trusted_proxy_hops": hops}))

        @app.get("/t/who")
        def who(request: Request) -> dict[str, Any]:
            return {
                "host": request.client.host if request.client else None,
                "scheme": request.url.scheme,
            }

        return app

    headers = {"X-Forwarded-For": "6.6.6.6, 203.0.113.9", "X-Forwarded-Proto": "https"}
    with TestClient(build_app(1), base_url="http://testserver") as c:
        assert c.get("/t/who", headers=headers).json() == {
            "host": "203.0.113.9",
            "scheme": "https",
        }
    with TestClient(build_app(0), base_url="http://testserver") as c:
        assert c.get("/t/who", headers=headers).json() == {"host": "testclient", "scheme": "http"}
