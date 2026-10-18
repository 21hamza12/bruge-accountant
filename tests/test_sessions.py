"""Step 17: server-side sessions (token, lookup under row-level security, touch, revoke)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import sessions
from app.auth.sessions import SessionPolicy
from app.db import make_engine, make_session_factory
from app.models import User, UserSession
from app.tenancy import TenantBindingError, bound_tenant, lookup_scope, rebind_tenant
from tests.web_support import T0, Seeded, WebDb, add_user, owner_session, seed

POLICY = SessionPolicy(idle=timedelta(hours=12), absolute=timedelta(days=7))


@pytest.fixture
def world(web_db: WebDb) -> Seeded:
    return seed(web_db.owner_url)


@pytest.fixture
def db(web_db: WebDb, world: Seeded) -> Iterator[Session]:
    """A request-style session on the application connection: no tenant bound yet."""
    engine = make_engine(web_db.app_url)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def _fetch_user(db: Session, world: Seeded, which: str) -> User:
    phone = {"a": "+22212345601", "b": "+22212345602", "staff": "+22212345603"}[which]
    lookup_scope(db, "app.login_phone", phone)
    user = db.scalars(select(User).where(User.phone == phone)).one()
    return user


def _create(
    db: Session, world: Seeded, which: str = "a", now: datetime = T0
) -> tuple[str, UserSession]:
    user = _fetch_user(db, world, which)
    created = sessions.create_session(db, user, now, POLICY)
    db.commit()
    return created.token, created.row


def test_token_is_random_urlsafe_and_only_its_hash_is_stored(
    db: Session, world: Seeded, web_db: WebDb
) -> None:
    token, row = _create(db, world)
    assert len(token) >= 43
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.token_hash != token
    with owner_session(web_db.owner_url) as s:
        rebind_tenant(s, world.agency_a)
        stored = s.scalars(select(UserSession)).one()
        blob = " ".join(str(v) for v in (stored.token_hash, stored.csrf_token))
    assert token not in blob
    assert stored.csrf_token != token


def test_two_sessions_never_share_a_token_or_a_csrf_secret(db: Session, world: Seeded) -> None:
    t1, r1 = _create(db, world)
    t2, r2 = _create(db, world)
    assert t1 != t2
    assert r1.csrf_token != r2.csrf_token


def test_lifetimes_come_from_the_policy(db: Session, world: Seeded) -> None:
    _, row = _create(db, world)
    assert row.idle_expires_at == T0 + timedelta(hours=12)
    assert row.absolute_expires_at == T0 + timedelta(days=7)


def test_lookup_finds_the_session_and_binds_the_tenant(web_db: WebDb, world: Seeded) -> None:
    engine = make_engine(web_db.app_url)
    try:
        with make_session_factory(engine)() as creator:
            token, _ = _create(creator, world)
        with make_session_factory(engine)() as request_db:
            assert bound_tenant(request_db) is None
            found = sessions.lookup_session(request_db, token, T0 + timedelta(minutes=1), POLICY)
            assert found is not None
            assert found.user.id == world.manager_a
            assert found.session.agency_id == world.agency_a
            assert bound_tenant(request_db) == world.agency_a
    finally:
        engine.dispose()


@pytest.mark.parametrize("token", ["", "nope", "x" * 43, "x" * 5000, "é" * 10])
def test_unknown_tokens_find_nothing_and_bind_nothing(
    db: Session, world: Seeded, token: str
) -> None:
    _create(db, world)
    fresh = db
    fresh.rollback()
    fresh.info.clear()  # a request session starts with no tenant
    assert sessions.lookup_session(fresh, token, T0, POLICY) is None
    assert bound_tenant(fresh) is None


def test_one_second_before_the_idle_limit_is_still_valid(db: Session, world: Seeded) -> None:
    token, _ = _create(db, world)
    limit = T0 + timedelta(hours=12) - timedelta(seconds=1)
    assert sessions.lookup_session(db, token, limit, POLICY) is not None


def test_idle_expiry_is_enforced_and_does_not_bind(db: Session, world: Seeded) -> None:
    token, _ = _create(db, world)
    db.rollback()
    db.info.clear()
    assert sessions.lookup_session(db, token, T0 + timedelta(hours=12, seconds=1), POLICY) is None
    assert bound_tenant(db) is None


def test_absolute_expiry_beats_activity(db: Session, world: Seeded) -> None:
    token, _ = _create(db, world)
    now = T0
    for _ in range(14):  # active every 11 hours: never idle, but 7 days is the ceiling
        now += timedelta(hours=11)
        if now >= T0 + timedelta(days=7):
            break
        db.rollback()
        assert sessions.lookup_session(db, token, now, POLICY) is not None
    db.rollback()
    assert sessions.lookup_session(db, token, T0 + timedelta(days=7, seconds=1), POLICY) is None


def test_touch_writes_at_most_every_five_minutes(db: Session, world: Seeded) -> None:
    token, row = _create(db, world)
    db.rollback()
    first = sessions.lookup_session(db, token, T0 + timedelta(minutes=4), POLICY)
    assert first is not None
    assert first.session.last_seen_at == T0  # too soon: nothing written
    db.rollback()
    later = sessions.lookup_session(db, token, T0 + timedelta(minutes=5), POLICY)
    assert later is not None
    assert later.session.last_seen_at == T0 + timedelta(minutes=5)
    assert later.session.idle_expires_at == T0 + timedelta(minutes=5, hours=12)


def test_touch_never_extends_past_the_absolute_limit(db: Session, world: Seeded) -> None:
    token, _ = _create(db, world)
    for hours in range(10, 161, 10):  # kept active, so the idle limit never hits
        db.rollback()
        assert sessions.lookup_session(db, token, T0 + timedelta(hours=hours), POLICY)
    db.rollback()
    near_end = T0 + timedelta(days=7) - timedelta(minutes=10)
    found = sessions.lookup_session(db, token, near_end, POLICY)
    assert found is not None
    assert found.session.idle_expires_at == found.session.absolute_expires_at


def test_revoked_session_is_dead(db: Session, world: Seeded) -> None:
    token, _ = _create(db, world)
    db.rollback()
    found = sessions.lookup_session(db, token, T0, POLICY)
    assert found is not None
    sessions.revoke_session(db, found.session, T0)
    db.commit()
    db.rollback()
    db.info.clear()
    assert sessions.lookup_session(db, token, T0, POLICY) is None


def test_inactive_user_cannot_use_a_session(db: Session, world: Seeded, web_db: WebDb) -> None:
    token, _ = _create(db, world, "staff")
    with owner_session(web_db.owner_url) as s:
        rebind_tenant(s, world.agency_a)
        s.get(User, world.staff_a).is_active = False  # type: ignore[union-attr]
        s.commit()
    db.rollback()
    db.info.clear()
    assert sessions.lookup_session(db, token, T0, POLICY) is None
    assert bound_tenant(db) is None


def test_revoke_all_for_user_can_spare_one_session(db: Session, world: Seeded) -> None:
    _, keep = _create(db, world)
    _, other1 = _create(db, world)
    _, other2 = _create(db, world)
    count = sessions.revoke_all_for_user(
        db, world.agency_a, world.manager_a, T0, except_session_id=keep.id
    )
    db.commit()
    assert count == 2
    rows = {r.id: r.revoked_at for r in db.scalars(select(UserSession))}
    assert rows[keep.id] is None
    assert rows[other1.id] == T0
    assert rows[other2.id] == T0
    # idempotent: nothing left to revoke
    assert sessions.revoke_all_for_user(db, world.agency_a, world.manager_a, T0) == 1


def test_revoke_all_does_not_touch_other_users_or_agencies(
    db: Session, world: Seeded, web_db: WebDb
) -> None:
    _create(db, world, "a")
    _create(db, world, "staff")
    sessions.revoke_all_for_user(db, world.agency_a, world.manager_a, T0)
    db.commit()
    live = [r for r in db.scalars(select(UserSession)) if r.revoked_at is None]
    assert [r.user_id for r in live] == [world.staff_a]


def test_a_session_cannot_be_created_for_another_agency_than_the_bound_one(
    db: Session, world: Seeded
) -> None:
    user_a = _fetch_user(db, world, "a")
    sessions.create_session(db, user_a, T0, POLICY)
    db.commit()
    user_b = _fetch_user(db, world, "b")
    with pytest.raises(TenantBindingError):
        sessions.create_session(db, user_b, T0, POLICY)


def test_helper_users_exist(world: Seeded, web_db: WebDb) -> None:
    assert add_user(web_db.owner_url, world.agency_a, "+22212345699") > 0
