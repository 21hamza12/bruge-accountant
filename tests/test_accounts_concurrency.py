"""Two managers removing each other at the same moment must not empty the agency (Postgres)."""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import accounts
from app.accounts import AccountError
from app.db import make_engine, make_session_factory
from app.models import User
from app.tenancy import bind_tenant
from tests.web_support import FakeClock, Seeded, WebDb, add_user, owner_session, seed


def test_concurrent_deactivation_leaves_one_manager(web_db: WebDb) -> None:
    if web_db.mode == "sqlite":
        pytest.skip("needs row locks: Postgres only")
    seeded: Seeded = seed(web_db.owner_url)
    second = add_user(
        web_db.owner_url, seeded.agency_a, "+22212345660", role="manager", display_name="Second"
    )
    clock = FakeClock()
    engines = [make_engine(web_db.app_url) for _ in range(2)]
    outcome: dict[str, str] = {}
    first_holds_locks = threading.Event()

    def fetch(user_id: int, session: Session) -> User:
        return session.scalars(select(User).where(User.id == user_id)).one()

    def first() -> None:
        with make_session_factory(engines[0])() as s:
            bind_tenant(s, seeded.agency_a)
            me = fetch(seeded.manager_a, s)
            target = fetch(seeded.manager_a, s)
            accounts.deactivate_user(s, me, target, clock(), None)  # locks, writes, no commit yet
            first_holds_locks.set()
            time.sleep(1.5)  # the other request is now waiting on the lock
            s.commit()
            outcome["first"] = "ok"

    def second_request() -> None:
        first_holds_locks.wait(10)
        with make_session_factory(engines[1])() as s:
            bind_tenant(s, seeded.agency_a)
            me = fetch(second, s)
            target = fetch(second, s)
            try:
                accounts.deactivate_user(s, me, target, clock(), None)
                s.commit()
                outcome["second"] = "ok"
            except AccountError as exc:
                s.rollback()
                outcome["second"] = str(exc)

    threads = [threading.Thread(target=first), threading.Thread(target=second_request)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
    finally:
        for engine in engines:
            engine.dispose()
    assert outcome["first"] == "ok"
    assert outcome["second"] == accounts.LAST_MANAGER
    with owner_session(web_db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, seeded.agency_a)
        active = [u.id for u in s.scalars(select(User)) if u.is_active and u.role == "manager"]
    assert active == [second]
