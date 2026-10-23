"""The request body limit: 413 before the body is read, streamed bodies counted, login bounded."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from app.settings import Settings
from app.web.bodylimit import RequestBodyLimitMiddleware
from app.web.cookies import login_csrf_cookie_name
from app.web.csrf import new_login_token
from tests.web_support import ORIGIN, FakeClock, WebDb, make_client

LIMIT = 2048


def test_the_default_limit_is_ten_megabytes_plus_sixty_four_kilobytes() -> None:
    assert Settings().max_request_bytes == 10 * 1024 * 1024 + 64 * 1024


def test_the_limit_can_be_set_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_REQUEST_BYTES", "5000")
    assert Settings().max_request_bytes == 5000


# --- the raw ASGI wrapper, with nothing else in the way -----------------------------------------


def _scope(headers: list[tuple[bytes, bytes]], method: str = "POST") -> Scope:
    return {
        "type": "http",
        "method": method,
        "path": "/x",
        "headers": headers,
        "query_string": b"",
        "scheme": "https",
        "server": ("testserver", 443),
        "client": ("203.0.113.5", 1234),
    }


class Harness:
    def __init__(self, chunks: list[bytes], limit: int = LIMIT) -> None:
        self.chunks = list(chunks)
        self.limit = limit
        self.receive_calls = 0
        self.app_called = False
        self.app_read = 0
        self.sent: list[Message] = []

    async def receive(self) -> Message:
        self.receive_calls += 1
        if not self.chunks:
            return {"type": "http.request", "body": b"", "more_body": False}
        body = self.chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(self.chunks)}

    async def send(self, message: Message) -> None:
        self.sent.append(message)

    async def downstream(self, scope: Scope, receive: Receive, send: Send) -> None:
        """An app that reads the whole body, then answers 200."""
        self.app_called = True
        more = True
        while more:
            message = await receive()
            self.app_read += len(message.get("body", b""))
            more = bool(message.get("more_body"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    def run(
        self, headers: list[tuple[bytes, bytes]], path_limits: dict[str, int] | None = None
    ) -> None:
        middleware = RequestBodyLimitMiddleware(
            self.downstream, max_bytes=self.limit, path_limits=path_limits
        )
        asyncio.run(middleware(_scope(headers), self.receive, self.send))

    @property
    def status(self) -> int:
        return int(next(m["status"] for m in self.sent if m["type"] == "http.response.start"))

    @property
    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.sent if m["type"] == "http.response.body")


def test_a_declared_length_over_the_limit_is_413_without_reading_a_byte() -> None:
    h = Harness([b"x" * 10_000])
    h.run([(b"content-length", b"10000")])
    assert h.status == 413
    assert h.receive_calls == 0  # the body was never touched
    assert h.app_called is False
    assert b"Fichier trop volumineux" in h.body


def test_a_declared_length_at_the_limit_passes() -> None:
    h = Harness([b"x" * LIMIT])
    h.run([(b"content-length", str(LIMIT).encode())])
    assert h.status == 200
    assert h.app_read == LIMIT


def test_a_path_can_have_its_own_larger_limit_and_no_other_path_does() -> None:
    declared = [(b"content-length", b"10000")]
    h = Harness([b"x" * 10_000])
    h.run(declared, path_limits={"/x": 20_000})
    assert h.status == 200 and h.app_read == 10_000
    other = Harness([b"x" * 10_000])
    other.run(declared, path_limits={"/y": 20_000})
    assert other.status == 413 and other.receive_calls == 0


def test_a_path_limit_also_bounds_a_streamed_body() -> None:
    h = Harness([b"x" * 1000] * 50)
    h.run([(b"transfer-encoding", b"chunked")], path_limits={"/x": 5000})
    assert h.status == 413 and h.receive_calls == 6  # 6000 > 5000


def test_a_streamed_body_is_counted_and_stopped_at_the_limit() -> None:
    chunks = [b"x" * 1000] * 50  # no Content-Length: chunked transfer
    h = Harness(chunks)
    h.run([(b"transfer-encoding", b"chunked")])
    assert h.status == 413
    assert h.receive_calls == 3  # 1000, 2000, then 3000 > 2048: stopped there, not at 50
    assert len(h.chunks) == 47
    assert h.app_called is True  # it started reading, and was cut off


def test_a_streamed_body_within_the_limit_passes() -> None:
    h = Harness([b"x" * 1000, b"y" * 1000])
    h.run([(b"transfer-encoding", b"chunked")])
    assert h.status == 200
    assert h.app_read == 2000


def test_a_lying_content_length_cannot_smuggle_a_bigger_body() -> None:
    h = Harness([b"x" * 1000] * 10)
    h.run([(b"content-length", b"500")])  # claims 500, sends 10 000
    assert h.status == 413
    assert h.receive_calls <= 3


@pytest.mark.parametrize("value", [b"abc", b"-5", b"1e3", b"12 34", b"\xc2\xb2"])
def test_a_malformed_content_length_is_a_400(value: bytes) -> None:
    h = Harness([b"x"])
    h.run([(b"content-length", value)])
    assert h.status == 400
    assert h.app_called is False


def test_non_http_scopes_pass_straight_through() -> None:
    seen: list[str] = []

    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=1)
    asyncio.run(middleware({"type": "lifespan"}, lambda: None, lambda m: None))  # type: ignore[arg-type,return-value]
    assert seen == ["lifespan"]


def test_a_response_already_started_is_not_overwritten() -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while True:
            await receive()

    h = Harness([b"x" * 1000] * 10)
    middleware = RequestBodyLimitMiddleware(downstream, max_bytes=LIMIT)
    with pytest.raises(Exception, match="too large|BodyTooLarge"):
        asyncio.run(middleware(_scope([]), h.receive, h.send))
    assert h.status == 200  # no second response was attempted


# --- inside the real app -----------------------------------------------------------------------


@pytest.fixture
def app_client(web_db: WebDb) -> Iterator[tuple[TestClient, FastAPI]]:
    factory = make_client(web_db, FakeClock(), max_request_bytes=LIMIT)
    app: FastAPI = factory.app  # type: ignore[attr-defined]
    with factory() as client:
        yield client, app


def test_the_login_form_is_bounded_for_anonymous_clients_too(
    app_client: tuple[TestClient, FastAPI],
) -> None:
    client, app = app_client
    token = new_login_token()
    client.cookies.set(login_csrf_cookie_name(app.state.settings), token)
    big = {"phone": "+22212345601", "password": "x" * 5000, "csrf_token": token}
    response = client.post("/login", data=big)
    assert response.status_code == 413
    assert "Fichier trop volumineux" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "content-security-policy" in response.headers  # the usual headers still apply
    ok = client.post("/login", data={**big, "password": "short"})
    assert ok.status_code == 401  # a normal body still reaches the route


def test_a_chunked_login_body_over_the_limit_is_the_413_page(
    app_client: tuple[TestClient, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app = app_client
    token = new_login_token()
    client.cookies.set(login_csrf_cookie_name(app.state.settings), token)
    sent: list[int] = []

    def chunks() -> Iterator[bytes]:
        yield b"csrf_token=" + token.encode() + b"&password="
        for n in range(200):
            sent.append(n)
            yield b"a" * 500

    response = client.post(
        "/login",
        content=chunks(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": ORIGIN},
    )
    assert response.status_code == 413
    # (the test client pre-reads its own generator; the early stop is proven on the raw ASGI
    # harness above, where the app's receive() is counted)
    assert sent


def test_the_middleware_runs_before_dependencies_parse_anything(
    app_client: tuple[TestClient, FastAPI], monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.requests import Request

    client, _ = app_client
    parsed: list[Any] = []
    real = Request.form

    async def spy(self: Request, *a: Any, **k: Any) -> Any:
        parsed.append(1)
        return await real(self, *a, **k)

    monkeypatch.setattr(Request, "form", spy)
    response = client.post(
        "/login",
        content=b"a=" + b"x" * 5000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
    assert parsed == []
