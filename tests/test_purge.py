"""Purging expired rows: ``purge_expired`` works on an unbound session, in both RLS modes.

Postgres: three DELETE-only policies decide what may go (expired or revoked sessions, used or
expired temporary passwords, audit events older than 400 days); the repository sends an
unqualified DELETE, because a WHERE clause would also need SELECT access to the very rows the
policies keep hidden. SQLite has no row-level security, so there the statements carry the same
conditions as a WHERE clause. The tests below pin both to the same outcome.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db import Base, make_engine, make_session_factory
from app.models import AuditEvent, TemporaryPassword, User, UserSession
from app.repository import PurgeCounts, create_agency, purge_expired
from app.tenancy import TenantBindingError, bind_tenant
from tests.conftest import RlsDatabase

DAY = timedelta(days=1)
TABLES = ("user_sessions", "temporary_passwords", "audit_events")


@dataclass
class Rows:
    """Primary keys per table, split into what must survive a purge and what must go."""

    live: dict[str, set[int]] = field(default_factory=lambda: {t: set() for t in TABLES})
    dead: dict[str, set[int]] = field(default_factory=lambda: {t: set() for t in TABLES})


def _session_row(agency_id: int, user_id: int, n: int, **kw: object) -> UserSession:
    now = datetime.now(UTC)
    fields: dict[str, object] = {
        "agency_id": agency_id,
        "user_id": user_id,
        "token_hash": f"{n:064x}",
        "csrf_token": "c" * 43,
        "last_seen_at": now,
        "idle_expires_at": now + timedelta(hours=12),
        "absolute_expires_at": now + 7 * DAY,
    }
    fields.update(kw)
    return UserSession(**fields)


def seed_agency(factory_url: str, slug: str, phone: str, base: int) -> tuple[int, Rows]:
    """One agency with live and dead rows in each purgeable table."""
    now = datetime.now(UTC)
    rows = Rows()
    engine = make_engine(factory_url)
    try:
        with make_session_factory(engine)() as s:
            agency_id = create_agency(s, slug, slug.upper()).id
            user = User(agency_id=agency_id, phone=phone, role="manager")
            s.add(user)
            s.commit()

            sessions = {
                "live": _session_row(agency_id, user.id, base + 1),
                "expired": _session_row(
                    agency_id, user.id, base + 2, absolute_expires_at=now - timedelta(minutes=1)
                ),
                "revoked": _session_row(agency_id, user.id, base + 3, revoked_at=now - DAY),
            }
            passwords = {
                "live": TemporaryPassword(
                    agency_id=agency_id,
                    user_id=user.id,
                    password_hash="h",  # noqa: S106
                    expires_at=now + timedelta(minutes=30),
                ),
                "expired": TemporaryPassword(
                    agency_id=agency_id,
                    user_id=user.id,
                    password_hash="h",  # noqa: S106
                    expires_at=now - timedelta(minutes=1),
                ),
                "used": TemporaryPassword(
                    agency_id=agency_id,
                    user_id=user.id,
                    password_hash="h",  # noqa: S106
                    expires_at=now + timedelta(minutes=30),
                    used_at=now - timedelta(minutes=5),
                ),
            }
            audits = {
                "live": AuditEvent(agency_id=agency_id, event_type="recent", details={}),
                "live-399": AuditEvent(
                    agency_id=agency_id,
                    event_type="old",
                    details={},
                    created_at=now - 399 * DAY,
                ),
                "expired-401": AuditEvent(
                    agency_id=agency_id,
                    event_type="ancient",
                    details={},
                    created_at=now - 401 * DAY,
                ),
            }
            for table, group in (
                ("user_sessions", sessions),
                ("temporary_passwords", passwords),
                ("audit_events", audits),
            ):
                s.add_all(group.values())
                s.commit()
                for name, obj in group.items():
                    bucket = rows.live if name.startswith("live") else rows.dead
                    bucket[table].add(obj.id)
            return agency_id, rows
    finally:
        engine.dispose()


@contextmanager
def bound_session(url: str, agency_id: int) -> Iterator[Session]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            bind_tenant(s, agency_id)
            yield s
    finally:
        engine.dispose()


def surviving(url: str, agency_id: int) -> dict[str, set[int]]:
    with bound_session(url, agency_id) as s:
        return {
            "user_sessions": set(s.scalars(select(UserSession.id))),
            "temporary_passwords": set(s.scalars(select(TemporaryPassword.id))),
            "audit_events": set(s.scalars(select(AuditEvent.id))),
        }


def run_purge(url: str) -> PurgeCounts:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:  # nobody is bound
            return purge_expired(s)
    finally:
        engine.dispose()


def check_purge(owner_url: str, run_as_url: str) -> None:
    a, rows_a = seed_agency(owner_url, "agency-a", "+22211111111", 0x100)
    b, rows_b = seed_agency(owner_url, "agency-b", "+22222222222", 0x200)

    counts = run_purge(run_as_url)

    assert counts == PurgeCounts(user_sessions=4, temporary_passwords=4, audit_events=2)
    assert counts.total == 10
    for agency_id, rows in ((a, rows_a), (b, rows_b)):
        left = surviving(owner_url, agency_id)
        for table in TABLES:
            assert left[table] >= rows.live[table], (agency_id, table, "a live row was deleted")
            assert not left[table] & rows.dead[table], (agency_id, table, "a dead row survived")
    # A second run finds nothing more.
    assert run_purge(run_as_url) == PurgeCounts(0, 0, 0)


# --- SQLite -----------------------------------------------------------------------------


def test_sqlite_purge_deletes_exactly_the_expired_rows(db_url: str) -> None:
    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite only")
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    check_purge(db_url, db_url)


def test_sqlite_purge_deletes_in_chunks_not_in_one_statement(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import event

    import app.repository as repository

    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite only: Postgres lets the row-level security policies pick the rows")
    monkeypatch.setattr(repository, "PURGE_CHUNK", 1)
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    seed_agency(db_url, "agency-a", "+22211111111", 0x100)
    seed_agency(db_url, "agency-b", "+22222222222", 0x200)
    statements: list[str] = []
    engine = make_engine(db_url)

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_c: object, _cur: object, statement: str, *_a: object) -> None:
        if statement.lstrip().upper().startswith("DELETE"):
            statements.append(statement)

    try:
        with make_session_factory(engine)() as s:
            counts = purge_expired(s)
    finally:
        engine.dispose()
    assert counts == PurgeCounts(user_sessions=4, temporary_passwords=4, audit_events=2)
    # 4 + 4 + 2 rows, one row per statement, plus the final empty check of each table
    assert len(statements) == 10 + 3


# --- Postgres, in both deployment modes -------------------------------------------------


def test_purge_in_both_modes_deletes_expired_rows_and_keeps_live_ones(rls_db: RlsDatabase) -> None:
    check_purge(rls_db.owner_url, rls_db.app_url)


def test_purge_by_the_owner_gives_the_same_result(rls_db: RlsDatabase) -> None:
    check_purge(rls_db.owner_url, rls_db.owner_url)


def test_the_purge_policies_do_not_expose_the_rows_they_delete(rls_db: RlsDatabase) -> None:
    """DELETE-only: an unbound session still reads nothing, dead rows included."""
    seed_agency(rls_db.owner_url, "agency-a", "+22211111111", 0x100)
    engine = make_engine(rls_db.app_url)
    try:
        with make_session_factory(engine)() as s:
            for table in TABLES:
                assert s.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0  # noqa: S608
    finally:
        engine.dispose()


def test_the_retention_boundary_is_400_days(rls_db: RlsDatabase) -> None:
    agency_id, rows = seed_agency(rls_db.owner_url, "agency-a", "+22211111111", 0x100)
    run_purge(rls_db.app_url)
    with bound_session(rls_db.owner_url, agency_id) as s:
        ages = {
            e.event_type for e in s.scalars(select(AuditEvent))
        }  # recent and 399-day events stay
        assert ages == {"recent", "old"}
    assert rows.dead["audit_events"]


# --- guards -----------------------------------------------------------------------------


def test_purge_refuses_a_bound_session_on_every_engine(db_url: str) -> None:
    """Bound, the tenant's own DELETE policy would apply as well: refuse instead of guessing."""
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as s:
            bind_tenant(s, 3)
            with pytest.raises(TenantBindingError, match="unbound"):
                purge_expired(s)
    finally:
        engine.dispose()


def test_purge_refuses_to_run_unqualified_without_row_level_security(db_url: str) -> None:
    """On Postgres the DELETE has no WHERE (the policies are the filter). A schema built without
    them (create_all) must therefore be refused, or the purge would empty three tables."""
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    try:
        with make_session_factory(engine)() as s:
            s.add(_agency_less_audit_row())
            s.commit()
            with pytest.raises(RuntimeError, match="row-level security"):
                purge_expired(s)
            assert s.scalar(select(AuditEvent.id)) is not None
    finally:
        engine.dispose()


def _agency_less_audit_row() -> AuditEvent:
    return AuditEvent(event_type="ancient", details={}, created_at=datetime.now(UTC) - 500 * DAY)
