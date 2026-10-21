"""Step 20: current_user, require_staff_admin and the must-change-password gate."""

from __future__ import annotations

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, current_user, require_login, require_staff_admin
from tests.web_support import (
    PASSWORD_A,
    PASSWORD_S,
    PHONE_A,
    PHONE_STAFF_A,
    FakeClock,
    Seeded,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    login,
    make_client,
    seed,
)

PENDING_PHONE = "+22212345620"


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        add_user(web_db.owner_url, self.seeded.agency_a, PENDING_PHONE, must_change=True)
        self.factory = make_client(web_db, FakeClock())
        app: FastAPI = self.factory.app  # type: ignore[attr-defined]

        def user_page(auth: Annotated[AuthContext, Depends(current_user)]) -> PlainTextResponse:
            return PlainTextResponse(f"user {auth.user.id}")

        def manager_page(
            auth: Annotated[AuthContext, Depends(require_staff_admin)],
        ) -> PlainTextResponse:
            return PlainTextResponse(f"manager {auth.user.id}")

        def pending_ok(auth: Annotated[AuthContext, Depends(require_login)]) -> PlainTextResponse:
            return PlainTextResponse(f"login {auth.user.id}")

        for method in ("GET", "POST"):
            deps = [Depends(csrf_protect)] if method == "POST" else []
            app.add_api_route("/t/user", user_page, methods=[method], dependencies=deps)
            app.add_api_route("/t/manager", manager_page, methods=[method], dependencies=deps)
            app.add_api_route("/t/login-only", pending_ok, methods=[method], dependencies=deps)

    def logged_in(self, phone: str, password: str = PASSWORD_S) -> TestClient:
        client = self.factory()
        assert login(client, phone, password).status_code == 303
        return client


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


# --- anonymous ----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/t/user", "/t/manager", "/t/login-only"])
def test_anonymous_html_get_is_redirected_to_login(env: Env, path: str) -> None:
    with env.factory() as c:
        response = c.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/t/user", "/t/manager"])
def test_anonymous_htmx_request_gets_401_with_hx_redirect(env: Env, path: str) -> None:
    with env.factory() as c:
        response = c.get(path, headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"
    assert "location" not in response.headers


def test_anonymous_htmx_post_also_gets_hx_redirect(env: Env) -> None:
    with env.factory() as c:
        response = c.post("/t/user", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login"


@pytest.mark.parametrize("path", ["/t/user", "/t/manager"])
def test_anonymous_stale_post_is_the_401_page(env: Env, path: str) -> None:
    with env.factory() as c:
        response = c.post(path, data={"csrf_token": "stale"}, follow_redirects=False)
    assert response.status_code == 401
    assert "Connexion requise" in response.text
    assert "location" not in response.headers


def test_a_stale_cookie_is_treated_as_anonymous(env: Env) -> None:
    with env.factory() as c:
        c.cookies.set("__Host-session", "x" * 43)
        assert c.get("/t/user", follow_redirects=False).status_code == 303


# --- roles --------------------------------------------------------------------------------------


def test_manager_reaches_manager_routes(env: Env) -> None:
    c = env.logged_in(PHONE_A, PASSWORD_A)
    assert c.get("/t/manager").text == f"manager {env.seeded.manager_a}"
    assert c.get("/t/user").text == f"user {env.seeded.manager_a}"


def test_staff_on_a_manager_route_gets_403_and_an_audit_event(env: Env) -> None:
    c = env.logged_in(PHONE_STAFF_A)
    assert c.get("/t/user").status_code == 200
    response = c.get("/t/manager")
    assert response.status_code == 403
    assert "Accès refusé" in response.text
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert len(events) == 1
    assert events[0].actor_user_id == env.seeded.staff_a
    assert events[0].details == {"path": "/t/manager", "method": "GET"}


def test_staff_post_on_a_manager_route_with_a_valid_token_is_403_and_audited(env: Env) -> None:
    c = env.logged_in(PHONE_STAFF_A)
    token = csrf_for(c)
    response = c.post("/t/manager", data={"csrf_token": token})
    assert response.status_code == 403
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")) == 1


def test_staff_htmx_request_on_a_manager_route_is_403_without_redirect_loop(env: Env) -> None:
    c = env.logged_in(PHONE_STAFF_A)
    response = c.get("/t/manager", headers={"HX-Request": "true"})
    assert response.status_code == 403
    assert "hx-redirect" not in response.headers


# --- must change password ------------------------------------------------------------------------


def test_a_pending_user_is_sent_to_the_change_page(env: Env) -> None:
    c = env.logged_in(PENDING_PHONE)
    for path in ("/t/user", "/t/manager"):
        response = c.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/password/change"


def test_a_pending_user_on_htmx_or_post_cannot_slip_through(env: Env) -> None:
    c = env.logged_in(PENDING_PHONE)
    htmx = c.get("/t/user", headers={"HX-Request": "true"}, follow_redirects=False)
    assert htmx.status_code == 403
    assert htmx.headers["hx-redirect"] == "/password/change"
    post = c.post("/t/user", data={"csrf_token": "x"}, follow_redirects=False)
    assert post.status_code == 403  # CSRF fails first, and the gate would refuse anyway


def test_a_pending_user_can_still_use_the_login_only_routes(env: Env) -> None:
    c = env.logged_in(PENDING_PHONE)
    assert c.get("/t/login-only").status_code == 200


def test_home_is_gated_too(env: Env) -> None:
    c = env.logged_in(PENDING_PHONE)
    response = c.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/password/change"
