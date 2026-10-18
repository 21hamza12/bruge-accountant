"""Step 21: the forced and the voluntary password change."""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.passwords import verify_password
from app.models import User, UserSession
from app.tenancy import rebind_tenant
from app.web.cookies import session_cookie_name
from tests.web_support import (
    NEW_PASSWORD,
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
    owner_session,
    seed,
)

PENDING_PHONE = "+22212345620"
OLD_TEMP = "Old-pending-pass-1"  # noqa: S105


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.pending_id = add_user(
            web_db.owner_url,
            self.seeded.agency_a,
            PENDING_PHONE,
            password=OLD_TEMP,
            must_change=True,
        )
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock)
        self.cookie = session_cookie_name(self.factory.app.state.settings)  # type: ignore[attr-defined]

    def pending_client(self) -> TestClient:
        client = self.factory()
        assert login(client, PENDING_PHONE, OLD_TEMP).status_code == 303
        return client

    def change(
        self,
        client: TestClient,
        current: str,
        new: str,
        confirm: str | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        response: httpx.Response = client.post(
            "/password/change",
            data={
                "current_password": current,
                "new_password": new,
                "confirm_password": new if confirm is None else confirm,
                "csrf_token": token if token is not None else csrf_for(client, "/password/change"),
            },
            follow_redirects=False,
        )
        return response

    def user(self, user_id: int) -> User:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.seeded.agency_a)
            return s.scalars(select(User).where(User.id == user_id)).one()


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def test_the_page_needs_a_login(env: Env) -> None:
    with env.factory() as c:
        response = c.get("/password/change", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_pending_user_sees_the_forced_notice(env: Env) -> None:
    page = env.pending_client().get("/password/change")
    assert page.status_code == 200
    assert "Vous devez choisir un nouveau mot de passe" in page.text


def test_a_normal_user_sees_no_forced_notice(env: Env) -> None:
    c = env.factory()
    login(c, PHONE_A, PASSWORD_A)
    page = c.get("/password/change")
    assert page.status_code == 200
    assert "Vous devez choisir" not in page.text


def test_a_successful_change_rotates_the_token_and_clears_the_flag(env: Env) -> None:
    c = env.pending_client()
    old_token = c.cookies.get(env.cookie)
    response = env.change(c, OLD_TEMP, NEW_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    new_token = c.cookies.get(env.cookie)
    assert new_token and new_token != old_token
    user = env.user(env.pending_id)
    assert user.must_change_password is False
    assert user.password_hash and NEW_PASSWORD not in user.password_hash
    assert verify_password(NEW_PASSWORD, user.password_hash)
    assert c.get("/", follow_redirects=False).status_code == 200  # the gate is lifted


def test_the_old_token_is_dead_after_the_change(env: Env) -> None:
    c = env.pending_client()
    old_token = c.cookies.get(env.cookie)
    env.change(c, OLD_TEMP, NEW_PASSWORD)
    stale = env.factory()
    stale.cookies.set(env.cookie, old_token or "")
    assert stale.get("/", follow_redirects=False).headers["location"] == "/login"


def test_every_other_session_of_the_user_is_revoked_but_not_other_users(env: Env) -> None:
    mine = env.pending_client()
    other_device = env.pending_client()
    colleague = env.factory()
    login(colleague, PHONE_STAFF_A, PASSWORD_S)
    env.change(mine, OLD_TEMP, NEW_PASSWORD)
    assert other_device.get("/password/change", follow_redirects=False).status_code == 303
    assert colleague.get("/", follow_redirects=False).status_code == 200
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        live = [r.user_id for r in s.scalars(select(UserSession)) if r.revoked_at is None]
    assert sorted(live) == sorted([env.pending_id, env.seeded.staff_a])


def test_new_password_works_and_old_one_does_not(env: Env) -> None:
    env.change(env.pending_client(), OLD_TEMP, NEW_PASSWORD)
    with env.factory() as fresh:
        assert login(fresh, PENDING_PHONE, NEW_PASSWORD).status_code == 303
    with env.factory() as fresh:
        assert login(fresh, PENDING_PHONE, OLD_TEMP).status_code == 401


def test_a_voluntary_change_by_a_normal_user(env: Env) -> None:
    c = env.factory()
    login(c, PHONE_A, PASSWORD_A)
    assert env.change(c, PASSWORD_A, NEW_PASSWORD).status_code == 303
    with env.factory() as fresh:
        assert login(fresh, PHONE_A, NEW_PASSWORD).status_code == 303


def test_wrong_current_password_changes_nothing(env: Env) -> None:
    c = env.pending_client()
    before = env.user(env.pending_id).password_hash
    response = env.change(c, "not-the-current-1", NEW_PASSWORD)
    assert response.status_code == 400
    assert "Le mot de passe actuel est incorrect." in response.text
    user = env.user(env.pending_id)
    assert user.password_hash == before
    assert user.must_change_password is True


@pytest.mark.parametrize(
    ("new", "confirm", "message"),
    [
        ("short", "short", "au moins 10 caractères"),
        (PENDING_PHONE, PENDING_PHONE, "numéro de téléphone"),
        ("2221234 5620", "2221234 5620", "numéro de téléphone"),
        ("password123", "password123", "trop courant"),
        ("aaaaaaaaaaaa", "aaaaaaaaaaaa", "caractère répété"),
        (NEW_PASSWORD, NEW_PASSWORD + "x", "pas identiques"),
        (OLD_TEMP, OLD_TEMP, "différent de l&#39;ancien"),
    ],
)
def test_policy_violations_are_refused_in_french(
    env: Env, new: str, confirm: str, message: str
) -> None:
    c = env.pending_client()
    response = env.change(c, OLD_TEMP, new, confirm)
    assert response.status_code == 400
    assert message in response.text
    assert env.user(env.pending_id).must_change_password is True


def test_guessing_the_current_password_is_rate_limited(env: Env) -> None:
    c = env.pending_client()
    statuses = [
        env.change(c, f"wrong-guess-number-{n}", NEW_PASSWORD).status_code for n in range(5)
    ]
    assert statuses == [400] * 5
    blocked = env.change(c, OLD_TEMP, NEW_PASSWORD)  # even the right one now
    assert blocked.status_code == 429
    assert "Trop de tentatives" in blocked.text
    assert env.user(env.pending_id).must_change_password is True
    env.clock.advance(minutes=15)
    assert env.change(c, OLD_TEMP, NEW_PASSWORD).status_code == 303


def test_a_post_without_a_valid_csrf_token_is_403(env: Env) -> None:
    c = env.pending_client()
    assert env.change(c, OLD_TEMP, NEW_PASSWORD, token="wrong").status_code == 403  # noqa: S106
    assert env.user(env.pending_id).must_change_password is True


def test_the_change_is_audited_without_secrets(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    c = env.pending_client()
    with caplog.at_level(logging.DEBUG):
        env.change(c, OLD_TEMP, NEW_PASSWORD)
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "password_changed")
    assert len(events) == 1
    assert events[0].actor_user_id == env.pending_id
    blob = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert NEW_PASSWORD not in blob
    assert OLD_TEMP not in blob


def test_the_gate_still_blocks_everything_else_until_the_change(env: Env) -> None:
    c = env.pending_client()
    assert c.get("/", follow_redirects=False).headers["location"] == "/password/change"
    assert c.get("/password/change").status_code == 200
