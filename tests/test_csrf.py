"""Step 18: CSRF dependencies (synchronizer token, login double-submit cookie, origin check)."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI, Form
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from app.settings import Settings
from app.web import csrf as csrf_module
from app.web.cookies import login_csrf_cookie_name, session_cookie_name
from app.web.csrf import csrf_protect, login_csrf_protect, new_login_token
from app.web.deps import AuthContext, get_auth
from tests.web_support import (
    ORIGIN,
    PHONE_A,
    PHONE_STAFF_A,
    FakeClock,
    WebDb,
    audit_events,
    audit_sequence,
    make_client,
    seed,
    session_cookie_for,
)

Factory = Callable[[], TestClient]
EVIL = "https://evil.example"


class Env:
    def __init__(self, web_db: WebDb, **overrides: object) -> None:
        self.web_db = web_db
        self.seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock, **overrides)
        self.app: FastAPI = self.factory.app  # type: ignore[attr-defined]
        self.settings: Settings = self.app.state.settings

        def secure_form(note: Annotated[str, Form()] = "") -> HTMLResponse:
            return HTMLResponse("saved " + note)

        def login_form(note: Annotated[str, Form()] = "") -> HTMLResponse:
            return HTMLResponse("login " + note)

        def token_page(auth: Annotated[AuthContext | None, Depends(get_auth)]) -> HTMLResponse:
            return HTMLResponse(auth.csrf_token if auth else "")

        self.app.add_api_route(
            "/t/secure", secure_form, methods=["POST"], dependencies=[Depends(csrf_protect)]
        )
        self.app.add_api_route(
            "/t/secure", token_page, methods=["GET"], dependencies=[Depends(csrf_protect)]
        )

        def raw_body() -> HTMLResponse:  # never touches the body, like a streaming upload
            return HTMLResponse("raw ok")

        self.app.add_api_route(
            "/t/raw", raw_body, methods=["POST"], dependencies=[Depends(csrf_protect)]
        )
        self.app.add_api_route(
            "/t/login", login_form, methods=["POST"], dependencies=[Depends(login_csrf_protect)]
        )

    def logged_in(self, phone: str = PHONE_A) -> tuple[TestClient, str]:
        client = self.factory()
        token = session_cookie_for(self.web_db, self.clock, self.settings, phone)
        client.cookies.set(session_cookie_name(self.settings), token)
        return client, client.get("/t/secure").text


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def test_matching_form_token_passes(env: Env) -> None:
    client, token = env.logged_in()
    assert token
    response = client.post("/t/secure", data={"csrf_token": token, "note": "x"})
    assert response.status_code == 200
    assert response.text == "saved x"


def test_matching_header_token_passes(env: Env) -> None:
    client, token = env.logged_in()
    assert client.post("/t/secure", headers={"X-CSRF-Token": token}).status_code == 200


def test_the_header_token_is_enough_so_the_body_is_never_parsed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upload routes can send the token in X-CSRF-Token and avoid spooling the body to disk."""
    from starlette.requests import Request

    async def no_form(self: Request, *args: object, **kwargs: object) -> None:
        raise AssertionError("the body was parsed although the header carried the token")

    client, token = env.logged_in()
    monkeypatch.setattr(Request, "form", no_form)
    response = client.post(
        "/t/raw",
        content=b"--x" + b"A" * 5000,
        headers={"X-CSRF-Token": token, "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert response.status_code == 200
    wrong = client.post(
        "/t/raw",
        content=b"--x",
        headers={"X-CSRF-Token": "wrong", "Content-Type": "multipart/form-data; boundary=x"},
    )
    assert wrong.status_code == 403  # a wrong header is refused without a fallback


@pytest.mark.parametrize(
    "data",
    [{}, {"csrf_token": ""}, {"csrf_token": "wrong"}, {"csrf_token": "é" * 43}, {"other": "1"}],
)
def test_missing_or_wrong_form_token_is_a_403_page(env: Env, data: dict[str, str]) -> None:
    client, _ = env.logged_in()
    response = client.post("/t/secure", data=data)
    assert response.status_code == 403
    assert "Accès refusé" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_a_wrong_header_is_refused_even_with_the_right_field(env: Env) -> None:
    client, token = env.logged_in()
    response = client.post(
        "/t/secure", data={"csrf_token": token}, headers={"X-CSRF-Token": "wrong"}
    )
    assert response.status_code == 403


def test_another_users_token_does_not_work(env: Env) -> None:
    mine, _ = env.logged_in(PHONE_A)
    _, theirs = env.logged_in(PHONE_STAFF_A)
    assert mine.post("/t/secure", data={"csrf_token": theirs}).status_code == 403


def test_a_json_body_cannot_smuggle_a_token(env: Env) -> None:
    client, token = env.logged_in()
    response = client.post("/t/secure", json={"csrf_token": token})
    assert response.status_code == 403


def test_safe_methods_are_not_checked(env: Env) -> None:
    client, _ = env.logged_in()
    assert client.get("/t/secure", headers={"Origin": EVIL}).status_code == 200


def test_failure_is_audited_with_agency_and_actor_but_no_token(env: Env) -> None:
    client, token = env.logged_in()
    client.post("/t/secure", data={"csrf_token": "wrong-token-value"})
    events = audit_events(env.web_db.owner_url, env.seeded.agency_a, "csrf_failed")
    assert len(events) == 1
    event = events[0]
    assert event.actor_user_id == env.seeded.manager_a
    assert event.details["reason"] == "token"
    blob = str(event.details) + str(event.ip)
    assert token not in blob
    assert "wrong-token-value" not in blob


def test_origin_must_match(env: Env) -> None:
    client, token = env.logged_in()
    bad = client.post("/t/secure", data={"csrf_token": token}, headers={"Origin": EVIL})
    assert bad.status_code == 403
    events = audit_events(env.web_db.owner_url, env.seeded.agency_a, "csrf_failed")
    assert events[-1].details["reason"] == "origin"


@pytest.mark.parametrize("origin", ["null", "http://testserver", "https://testserver.evil.example"])
def test_lookalike_origins_are_refused(env: Env, origin: str) -> None:
    client, token = env.logged_in()
    response = client.post("/t/secure", data={"csrf_token": token}, headers={"Origin": origin})
    assert response.status_code == 403


def test_a_post_with_neither_origin_nor_referer_is_refused(env: Env) -> None:
    client, token = env.logged_in()
    client.headers.pop("Origin")
    assert client.post("/t/secure", data={"csrf_token": token}).status_code == 403


def test_referer_is_the_fallback(env: Env) -> None:
    client, token = env.logged_in()
    client.headers.pop("Origin")
    good = client.post(
        "/t/secure", data={"csrf_token": token}, headers={"Referer": f"{ORIGIN}/staff?x=1"}
    )
    assert good.status_code == 200
    bad = client.post(
        "/t/secure", data={"csrf_token": token}, headers={"Referer": f"{EVIL}/{ORIGIN}"}
    )
    assert bad.status_code == 403
    junk = client.post("/t/secure", data={"csrf_token": token}, headers={"Referer": "/staff"})
    assert junk.status_code == 403


def test_cors_allowlist_origins_are_accepted(web_db: WebDb) -> None:
    env = Env(web_db, cors_allowed_origins=["https://partner.example"])
    client, token = env.logged_in()
    ok = client.post(
        "/t/secure", data={"csrf_token": token}, headers={"Origin": "https://partner.example"}
    )
    assert ok.status_code == 200
    other = client.post("/t/secure", data={"csrf_token": token}, headers={"Origin": EVIL})
    assert other.status_code == 403


def test_the_token_comparison_is_constant_time(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return bool(real(a, b))

    monkeypatch.setattr(hmac, "compare_digest", spy)
    client, token = env.logged_in()
    client.post("/t/secure", data={"csrf_token": token})
    assert calls
    assert csrf_module.tokens_match("a", None) is False
    assert csrf_module.tokens_match("", "") is False


def test_anonymous_unsafe_request_is_unauthorised(env: Env) -> None:
    with env.factory() as c:
        assert c.post("/t/secure", data={"csrf_token": "x"}).status_code == 401


# --- the anonymous login form: double-submit cookie ---------------------------------------------


def test_login_token_cookie_helpers() -> None:
    assert len(new_login_token()) >= 43
    assert csrf_module.usable_login_token("short") is None
    assert csrf_module.usable_login_token(None) is None
    token = new_login_token()
    assert csrf_module.usable_login_token(token) == token


def _with_login_cookie(env: Env, value: str | None) -> TestClient:
    client = env.factory()
    if value is not None:
        client.cookies.set(login_csrf_cookie_name(env.settings), value)
    return client


def test_login_form_double_submit_passes(env: Env) -> None:
    token = new_login_token()
    client = _with_login_cookie(env, token)
    response = client.post("/t/login", data={"csrf_token": token, "note": "x"})
    assert response.status_code == 200


@pytest.mark.parametrize(
    ("cookie", "field"),
    [(None, "a" * 43), ("a" * 43, None), ("a" * 43, "b" * 43), ("a" * 43, ""), (None, None)],
)
def test_login_form_mismatch_is_refused_and_logged_but_not_stored(
    env: Env, caplog: pytest.LogCaptureFixture, cookie: str | None, field: str | None
) -> None:
    before = audit_sequence(env.web_db.owner_url)
    client = _with_login_cookie(env, cookie)
    data = {} if field is None else {"csrf_token": field}
    with caplog.at_level(logging.INFO, logger="app.audit"):
        response = client.post("/t/login", data=data)
    assert response.status_code == 403
    assert audit_sequence(env.web_db.owner_url) == before  # an anonymous client writes no rows
    records = [r for r in caplog.records if r.name == "app.audit"]
    assert [r.event_type for r in records] == ["csrf_failed"]  # type: ignore[attr-defined]
    assert records[0].agency_id is None  # type: ignore[attr-defined]


def test_login_form_origin_is_checked_too(env: Env) -> None:
    token = new_login_token()
    client = _with_login_cookie(env, token)
    response = client.post("/t/login", data={"csrf_token": token}, headers={"Origin": EVIL})
    assert response.status_code == 403


def test_body_carries_the_token_for_htmx_without_inline_script() -> None:
    from app.web.templating import render

    html = render("errors/403.html", ref="ABC", csrf_token="tok-123_ABC")  # noqa: S106
    assert 'data-csrf-token="tok-123_ABC"' in html
    assert "data-hx-headers=" in html
    assert "X-CSRF-Token" in html
    assert "hx-on" not in html
    assert "<script>" not in html


def test_body_has_no_token_attributes_when_logged_out() -> None:
    from app.web.templating import render

    html = render("errors/403.html", ref="ABC")
    assert "data-csrf-token" not in html
    assert "hx-headers" not in html


def test_a_hostile_token_cannot_break_out_of_the_attribute() -> None:
    from app.web.templating import render

    html = render("errors/403.html", ref="ABC", csrf_token='"><script>alert(1)</script>')  # noqa: S106
    assert "<script>alert" not in html
