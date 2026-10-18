"""Step 23: temporary passwords (30 minutes, single use, never in the clear)."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import temp_passwords
from app.auth.passwords import verify_password
from app.db import make_engine, make_session_factory
from app.models import TemporaryPassword, User, UserSession
from app.tenancy import bind_tenant, rebind_tenant
from tests.web_support import (
    NEW_PASSWORD,
    PASSWORD_S,
    PHONE_STAFF_A,
    FakeClock,
    Seeded,
    WebDb,
    audit_events,
    csrf_for,
    database_text,
    form_token,
    issue_temp,
    login,
    make_client,
    owner_session,
    seed,
)


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock)
        self.agency = self.seeded.agency_a
        self.user = self.seeded.staff_a

    def issue(self) -> str:
        return issue_temp(
            self.db, self.clock, self.agency, self.user, actor_id=self.seeded.manager_a
        )

    def try_login(self, password: str) -> httpx.Response:
        with self.factory() as c:
            return login(c, PHONE_STAFF_A, password)

    def user_row(self) -> User:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.agency)
            return s.scalars(select(User).where(User.id == self.user)).one()

    def temp_rows(self) -> list[TemporaryPassword]:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.agency)
            return list(s.scalars(select(TemporaryPassword).order_by(TemporaryPassword.id)))


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


# --- issuing --------------------------------------------------------------------------------------


def test_issuing_clears_the_password_and_stores_only_an_argon2_hash(env: Env) -> None:
    plaintext = env.issue()
    user = env.user_row()
    assert user.password_hash is None
    assert user.must_change_password is True
    (row,) = env.temp_rows()
    assert row.password_hash.startswith("$argon2id$")
    assert plaintext not in row.password_hash
    assert verify_password(plaintext, row.password_hash)
    assert row.expires_at == env.clock() + timedelta(minutes=30)
    assert row.used_at is None


def test_the_old_password_stops_working_at_once(env: Env) -> None:
    assert env.try_login(PASSWORD_S).status_code == 303
    env.issue()
    assert env.try_login(PASSWORD_S).status_code == 401


def test_issuing_revokes_the_users_sessions_only(env: Env) -> None:
    mine = env.factory()
    login(mine, PHONE_STAFF_A, PASSWORD_S)
    assert mine.get("/", follow_redirects=False).status_code == 200
    env.issue()
    assert mine.get("/", follow_redirects=False).status_code == 303
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.agency)
        assert all(r.revoked_at is not None for r in s.scalars(select(UserSession)))


def test_issuing_is_audited_without_the_value(env: Env) -> None:
    plaintext = env.issue()
    (event,) = audit_events(env.db.owner_url, env.agency, "temp_password_issued")
    assert event.actor_user_id == env.seeded.manager_a
    assert event.details == {"user_id": env.user}
    assert plaintext not in str(event.details)


def test_reissuing_retires_the_previous_one(env: Env) -> None:
    first = env.issue()
    second = env.issue()
    assert first != second
    assert env.try_login(first).status_code == 401
    assert env.try_login(second).status_code == 303
    rows = env.temp_rows()
    assert len(rows) == 2
    assert all(r.used_at is not None for r in rows)


# --- using ----------------------------------------------------------------------------------------


def test_login_with_the_temporary_password_leads_to_the_forced_change(env: Env) -> None:
    plaintext = env.issue()
    with env.factory() as c:
        response = login(c, PHONE_STAFF_A, plaintext)
        assert response.status_code == 303
        assert c.get("/", follow_redirects=False).headers["location"] == "/password/change"
        page = c.get("/password/change")
        assert "Vous devez choisir un nouveau mot de passe" in page.text


@pytest.mark.parametrize(
    ("minutes", "seconds", "works"),
    [(0, 0, True), (29, 59, True), (30, 0, False), (30, 1, False), (31, 0, False)],
)
def test_the_30_minute_expiry_uses_the_injected_clock(
    env: Env, minutes: int, seconds: int, works: bool
) -> None:
    plaintext = env.issue()
    env.clock.advance(minutes=minutes, seconds=seconds)
    assert env.try_login(plaintext).status_code == (303 if works else 401)


def test_an_expired_temporary_password_is_audited_with_its_reason(env: Env) -> None:
    plaintext = env.issue()
    env.clock.advance(minutes=30, seconds=1)
    env.try_login(plaintext)
    (event,) = audit_events(env.db.owner_url, env.agency, "login_failure")
    assert event.details["reason"] == "temp_password_expired_or_used"


def test_a_temporary_password_works_once(env: Env) -> None:
    plaintext = env.issue()
    assert env.try_login(plaintext).status_code == 303
    assert env.try_login(plaintext).status_code == 401  # second use, well inside 30 minutes
    (row,) = env.temp_rows()
    assert row.used_at == env.clock()


def test_wrong_guesses_do_not_burn_the_temporary_password(env: Env) -> None:
    plaintext = env.issue()
    for n in range(3):
        assert env.try_login(f"wrong-guess-{n}-xx").status_code == 401
    assert env.try_login(plaintext).status_code == 303


def test_the_spend_is_atomic_across_two_engines(env: Env) -> None:
    env.issue()
    (row,) = env.temp_rows()
    results = []
    engines = [make_engine(env.db.app_url) for _ in range(2)]
    try:
        for engine in engines:
            with make_session_factory(engine)() as s:
                bind_tenant(s, env.agency)
                results.append(temp_passwords.consume_temporary_password(s, row.id, env.clock()))
                s.commit()
    finally:
        for engine in engines:
            engine.dispose()
    assert results == [True, False]


def test_consume_refuses_at_the_exact_expiry_instant(env: Env) -> None:
    env.issue()
    (row,) = env.temp_rows()
    engine = make_engine(env.db.app_url)
    try:
        with make_session_factory(engine)() as s:
            bind_tenant(s, env.agency)
            at_expiry = env.clock() + timedelta(minutes=30)
            assert temp_passwords.consume_temporary_password(s, row.id, at_expiry) is False
            assert temp_passwords.consume_temporary_password(
                s, row.id, at_expiry - timedelta(microseconds=1)
            )
    finally:
        engine.dispose()


def test_a_deactivated_user_cannot_use_a_temporary_password(env: Env) -> None:
    plaintext = env.issue()
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.agency)
        s.scalars(select(User).where(User.id == env.user)).one().is_active = False
        s.commit()
    assert env.try_login(plaintext).status_code == 401


def test_the_full_journey_ends_with_a_normal_login(env: Env) -> None:
    plaintext = env.issue()
    with env.factory() as c:
        assert login(c, PHONE_STAFF_A, plaintext).status_code == 303
        token = csrf_for(c, "/password/change")
        response = c.post(
            "/password/change",
            data={
                "current_password": plaintext,
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert c.get("/", follow_redirects=False).status_code == 200
    assert env.try_login(NEW_PASSWORD).status_code == 303
    assert env.try_login(plaintext).status_code == 401


def test_the_change_page_does_not_accept_a_made_up_current_password(env: Env) -> None:
    plaintext = env.issue()
    with env.factory() as c:
        login(c, PHONE_STAFF_A, plaintext)
        token = csrf_for(c, "/password/change")
        response = c.post(
            "/password/change",
            data={
                "current_password": "invented-by-an-attacker",
                "new_password": NEW_PASSWORD,
                "confirm_password": NEW_PASSWORD,
                "csrf_token": token,
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert env.user_row().password_hash is None


# --- the plaintext is nowhere -----------------------------------------------------------------


def _urls_and_locations(c: TestClient, responses: list[httpx.Response]) -> str:
    seen: list[str] = []
    for response in responses:
        seen.append(str(response.request.url))
        seen.append(response.headers.get("location", ""))
        for hop in response.history:
            seen.extend([str(hop.request.url), hop.headers.get("location", "")])
    return "\n".join(seen)


def test_the_plaintext_appears_nowhere_it_should_not(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    plaintext = env.issue()
    responses: list[httpx.Response] = []
    with caplog.at_level(logging.DEBUG), env.factory() as c:
        page = c.get("/login")
        responses.append(page)
        post = c.post(
            "/login",
            data={
                "phone": PHONE_STAFF_A,
                "password": plaintext,
                "csrf_token": form_token(page),
            },
            follow_redirects=False,
        )
        responses.append(post)
        for path in ("/", "/password/change", "/login", "/nope"):
            responses.append(c.get(path))
        bad = c.post(
            "/password/change",
            data={
                "current_password": plaintext,
                "new_password": "short",
                "confirm_password": "short",
                "csrf_token": csrf_for(c, "/password/change"),
            },
        )
        responses.append(bad)
    assert post.status_code == 303
    # later pages (including the one that just re-displays a form after an error)
    for response in responses:
        assert plaintext not in response.text
        assert plaintext not in str(response.headers)
    assert plaintext not in _urls_and_locations(c, responses)
    # the log lines of every request and audit event
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert plaintext not in logged
    # every readable column of every login table (the dump is not empty: it holds the phone)
    assert PHONE_STAFF_A in database_text(env.db.owner_url, [env.seeded.agency_a])
    assert plaintext not in database_text(env.db.owner_url, [env.seeded.agency_a])
    # and the DB keeps no trace under another encoding either
    assert plaintext.lower() not in database_text(env.db.owner_url, [env.seeded.agency_a]).lower()
