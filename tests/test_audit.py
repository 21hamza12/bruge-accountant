"""The audit helpers, and the account rules the review found untested."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from app import accounts, audit
from app.auth import passwords
from app.models import AuditEvent, TemporaryPassword, User, UserSession
from app.tenancy import rebind_tenant
from tests.web_support import (
    PASSWORD_A,
    PASSWORD_S,
    PHONE_A,
    PHONE_STAFF_A,
    T0,
    FakeClock,
    Seeded,
    WebDb,
    audit_events,
    csrf_for,
    issue_temp,
    login,
    make_client,
    owner_session,
    seed,
)

# --- the secret-key guard ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "Passwd",
        "session_token",
        "TOKEN",
        "client_secret",
        "password_hash",
        "csrf_token",
        "cookie",
        "credentials",
    ],
)
def test_a_secret_looking_detail_key_is_a_programming_error(
    session: Any, key: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.audit"):
        with pytest.raises(ValueError, match="must not carry secrets"):
            audit.record(session, audit.LOGIN_FAILURE, details={key: "x"})
        with pytest.raises(ValueError, match="must not carry secrets"):
            audit.record_and_commit(session, audit.LOGIN_FAILURE, details={key: "x"})
        with pytest.raises(ValueError, match="must not carry secrets"):
            audit.log_only(audit.CSRF_FAILED, details={key: "x"})
    assert not [r for r in caplog.records if r.name == "app.audit"]
    assert session.scalars(select(AuditEvent)).first() is None


def test_ordinary_detail_keys_are_accepted(session: Any) -> None:
    audit.record(session, audit.LOGIN_FAILURE, details={"reason": "bad", "user_id": 3, "path": "/"})
    session.commit()
    assert session.scalars(select(AuditEvent)).one().details == {
        "reason": "bad",
        "user_id": 3,
        "path": "/",
    }


def test_the_injected_clock_stamps_the_event(session: Any) -> None:
    when = datetime(2031, 1, 2, 3, 4, 5, tzinfo=UTC)
    audit.record(session, audit.LOGOUT, now=when)
    audit.record_and_commit(session, audit.LOGOUT, now=when)
    audit.record_and_commit(session, audit.LOGOUT)  # no clock: the database default
    created = sorted(e.created_at for e in session.scalars(select(AuditEvent)))
    assert created.count(when) == 2
    assert created[0] != created[-1] or len(set(created)) == 2


def test_log_only_writes_the_log_and_no_row(session: Any, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.audit"):
        audit.log_only(audit.CSRF_FAILED, ip="203.0.113.9", details={"reason": "token"})
    (record,) = [r for r in caplog.records if r.name == "app.audit"]
    assert record.event_type == "csrf_failed"  # type: ignore[attr-defined]
    assert record.ip == "203.0.113.9"  # type: ignore[attr-defined]
    assert session.scalars(select(AuditEvent)).first() is None


# --- display names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("Amadou​ Ba", "Amadou Ba"),  # zero-width space
        ("Am‍adou", "Amadou"),  # zero-width joiner
        ("﻿Amadou", "Amadou"),  # byte order mark
        ("Ama‮dou", "Amadou"),  # right-to-left override
        ("⁦Amadou⁩", "Amadou"),  # bidi isolates
        ("Mariem  mint   Ali", "Mariem mint Ali"),
        ("مريم بنت علي", "مريم بنت علي"),  # real text is untouched
        ("Zoë Dupont", "Zoë Dupont"),
    ],
)
def test_invisible_format_characters_are_stripped_from_names(raw: str, clean: str) -> None:
    assert accounts.clean_display_name(raw) == clean


@pytest.mark.parametrize("raw", ["", "   ", "​‍", "‮", "a\x00b", "x" * 101])
def test_names_that_are_empty_after_cleaning_or_hold_controls_are_refused(raw: str) -> None:
    with pytest.raises(accounts.AccountError):
        accounts.clean_display_name(raw)


# --- service rules, seen through the web app ---------------------------------------------------


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def test_deactivating_retires_the_unused_temporary_password_with_the_clock(env: Env) -> None:
    issue_temp(env.db, env.clock, env.seeded.agency_a, env.seeded.staff_a, env.seeded.manager_a)
    manager = env.factory()
    assert login(manager, PHONE_A, PASSWORD_A).status_code == 303
    env.clock.advance(minutes=7)
    response = manager.post(
        f"/staff/{env.seeded.staff_a}/deactivate",
        data={"csrf_token": csrf_for(manager, "/staff")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    when = env.clock()
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        assert s.scalars(select(TemporaryPassword)).one().used_at == when
        assert s.get(User, env.seeded.staff_a).updated_at == when  # type: ignore[union-attr]
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "user_deactivated")
    assert event.created_at == when  # the application clock, not the database's


def test_deactivating_revokes_the_users_live_sessions_with_the_clock(env: Env) -> None:
    staff = env.factory()
    assert login(staff, PHONE_STAFF_A, PASSWORD_S).status_code == 303
    env.clock.advance(minutes=3)
    manager = env.factory()
    assert login(manager, PHONE_A, PASSWORD_A).status_code == 303
    manager.post(
        f"/staff/{env.seeded.staff_a}/deactivate",
        data={"csrf_token": csrf_for(manager, "/staff")},
        follow_redirects=False,
    )
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        rows = list(s.scalars(select(UserSession).where(UserSession.user_id == env.seeded.staff_a)))
    assert rows
    assert all(r.revoked_at == env.clock() for r in rows)


class CountingHasher:
    def __init__(self, real: Any) -> None:
        self.real = real
        self.verifications = 0
        self.hashes = 0

    def verify(self, *args: Any) -> bool:
        self.verifications += 1
        return bool(self.real.verify(*args))

    def hash(self, *args: Any) -> str:
        self.hashes += 1
        return str(self.real.hash(*args))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def test_a_duplicate_phone_spends_one_argon2_verification(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = env.factory()
    assert login(manager, PHONE_A, PASSWORD_A).status_code == 303
    token = csrf_for(manager, "/staff")
    counter = CountingHasher(passwords._HASHER)
    monkeypatch.setattr(passwords, "_HASHER", counter)
    response = manager.post(
        "/staff",
        data={
            "display_name": "Copy",
            "phone": PHONE_STAFF_A,  # already taken
            "role": "staff",
            "csrf_token": token,
        },
    )
    assert response.status_code == 400
    assert counter.verifications == 1
    assert counter.hashes == 0
    # a new phone costs one hash instead (the temporary password), and no verification
    counter.verifications = counter.hashes = 0
    ok = manager.post(
        "/staff",
        data={
            "display_name": "Fresh",
            "phone": "+22212345655",
            "role": "staff",
            "csrf_token": token,
        },
    )
    assert ok.status_code == 200
    assert (counter.verifications, counter.hashes) == (0, 1)


def test_events_are_stamped_with_the_injected_clock(env: Env) -> None:
    env.clock.now = T0.replace(year=2030)
    c = env.factory()
    assert login(c, PHONE_A, PASSWORD_A).status_code == 303
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "login_success")
    assert event.created_at == env.clock()
