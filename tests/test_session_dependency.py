"""Step 17: the session as a FastAPI dependency and the cookie attributes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import sessions
from app.settings import Settings
from app.tenancy import bound_tenant
from app.web.cookies import session_cookie_name
from app.web.deps import AuthContext, get_auth, get_session
from tests.web_support import (
    PHONE_A,
    FakeClock,
    Seeded,
    WebDb,
    make_client,
    seed,
    session_cookie_for,
)

Factory = Callable[[], TestClient]


@pytest.fixture
def env(web_db: WebDb) -> tuple[Factory, FakeClock, Seeded, WebDb]:
    seeded = seed(web_db.owner_url)
    clock = FakeClock()
    factory = make_client(web_db, clock)
    app: FastAPI = factory.app  # type: ignore[attr-defined]

    def whoami(
        db: Annotated[Session, Depends(get_session)],
        auth: Annotated[AuthContext | None, Depends(get_auth)],
    ) -> JSONResponse:
        return JSONResponse(
            {
                "user": auth.user.id if auth else None,
                "bound": bound_tenant(db),
                "csrf": auth.session.csrf_token if auth else None,
            }
        )

    app.add_api_route("/t/whoami", whoami)
    return factory, clock, seeded, web_db


def _cookie_for_manager_a(web_db: WebDb, clock: FakeClock, settings: Settings) -> str:
    return session_cookie_for(web_db, clock, settings, PHONE_A)


def _settings(factory: Factory) -> Settings:
    app: FastAPI = factory.app  # type: ignore[attr-defined]
    settings: Settings = app.state.settings
    return settings


def test_anonymous_request_has_no_user_and_no_tenant(
    env: tuple[Factory, FakeClock, Seeded, WebDb],
) -> None:
    factory, *_ = env
    with factory() as c:
        body = c.get("/t/whoami").json()
    assert body == {"user": None, "bound": None, "csrf": None}


def test_valid_cookie_gives_the_user_and_binds_the_tenant(
    env: tuple[Factory, FakeClock, Seeded, WebDb],
) -> None:
    factory, clock, seeded, web_db = env
    settings = _settings(factory)
    token = _cookie_for_manager_a(web_db, clock, settings)
    with factory() as c:
        c.cookies.set(session_cookie_name(settings), token)
        body = c.get("/t/whoami").json()
    assert body["user"] == seeded.manager_a
    assert body["bound"] == seeded.agency_a
    assert len(body["csrf"]) >= 43


@pytest.mark.parametrize("value", ["garbage", "x" * 43, "x" * 5000, ""])
def test_a_bad_cookie_is_anonymous_and_binds_nothing(
    env: tuple[Factory, FakeClock, Seeded, WebDb], value: str
) -> None:
    factory, _, _, _ = env
    settings = _settings(factory)
    with factory() as c:
        c.cookies.set(session_cookie_name(settings), value)
        body = c.get("/t/whoami").json()
    assert body == {"user": None, "bound": None, "csrf": None}


def test_the_injected_clock_decides_expiry(
    env: tuple[Factory, FakeClock, Seeded, WebDb],
) -> None:
    factory, clock, seeded, web_db = env
    settings = _settings(factory)
    token = _cookie_for_manager_a(web_db, clock, settings)
    with factory() as c:
        c.cookies.set(session_cookie_name(settings), token)
        assert c.get("/t/whoami").json()["user"] == seeded.manager_a
        clock.advance(hours=12, seconds=1)
        assert c.get("/t/whoami").json()["user"] is None


def test_session_cookie_attributes() -> None:
    from starlette.responses import Response

    from app.web.cookies import clear_session_cookie, set_session_cookie

    settings = Settings()
    response = Response()
    policy = sessions.SessionPolicy.from_settings(settings)
    set_session_cookie(response, "tok", settings, policy)
    header = response.headers["set-cookie"]
    assert header.startswith("__Host-session=tok;")
    assert "HttpOnly" in header
    assert "Secure" in header
    assert "SameSite=lax" in header
    assert "Path=/" in header
    assert "Domain" not in header
    assert f"Max-Age={7 * 86400}" in header
    cleared = Response()
    clear_session_cookie(cleared, settings)
    assert "Max-Age=0" in cleared.headers["set-cookie"]
    assert "__Host-session=" in cleared.headers["set-cookie"]


def test_insecure_development_cookie_drops_the_host_prefix() -> None:
    from starlette.responses import Response

    from app.web.cookies import set_session_cookie

    settings = Settings(cookie_secure=False)
    response = Response()
    set_session_cookie(
        response, "tok", settings, sessions.SessionPolicy(timedelta(hours=1), timedelta(days=1))
    )
    header = response.headers["set-cookie"]
    assert header.startswith("session=tok;")
    assert "Secure" not in header
    assert "HttpOnly" in header


def test_settings_lifetimes_are_configurable() -> None:
    settings = Settings(session_idle_hours=2, session_absolute_days=3)
    policy = sessions.SessionPolicy.from_settings(settings)
    assert policy.idle == timedelta(hours=2)
    assert policy.absolute == timedelta(days=3)
    with pytest.raises(ValueError, match="session_idle_hours"):
        Settings(session_idle_hours=0)
