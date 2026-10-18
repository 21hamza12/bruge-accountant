"""Helpers for the row-level security tests: seeded tenants and catalogue queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import (
    AuditEvent,
    StaffSignLink,
    TemporaryPassword,
    User,
    UserSession,
)
from app.parsing.amadeus import parse_report
from app.repository import create_agency, import_report
from tests.builders import build_workbook, ticket_row
from tests.conftest import SHA

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
# Not agency-scoped on purpose: the migration bookkeeping and the shared rate-limit counters.
UNSCOPED_TABLES = frozenset({"alembic_version", "rate_limit_buckets"})


@dataclass(frozen=True)
class Tenant:
    agency_id: int
    slug: str
    phone: str
    token_hash: str
    user_id: int


def seed_tenant(owner_url: str, slug: str, phone: str, token_hash: str, doc_base: int) -> Tenant:
    """One agency with a row in every agency-scoped table, written through its own tenant."""
    engine = make_engine(owner_url)
    try:
        with make_session_factory(engine)() as s:
            agency_id = create_agency(s, slug, slug.upper()).id
            user = User(agency_id=agency_id, phone=phone, role="manager")
            s.add(user)
            s.commit()
            s.add(StaffSignLink(agency_id=agency_id, user_id=user.id, staff_sign="SV"))
            s.add(
                UserSession(
                    agency_id=agency_id,
                    user_id=user.id,
                    token_hash=token_hash,
                    csrf_token="c" * 43,
                    last_seen_at=NOW,
                    idle_expires_at=NOW + timedelta(hours=12),
                    absolute_expires_at=NOW + timedelta(days=7),
                )
            )
            s.add(
                TemporaryPassword(
                    agency_id=agency_id,
                    user_id=user.id,
                    password_hash="x",  # noqa: S106
                    expires_at=NOW + timedelta(minutes=30),
                )
            )
            s.add(AuditEvent(agency_id=agency_id, event_type="seed", details={}))
            s.commit()
            rows = [ticket_row(doc=f"{doc_base + n}") for n in range(2)]
            import_report(s, agency_id, parse_report(build_workbook(rows)), "seed.xlsx", SHA)
            return Tenant(agency_id, slug, phone, token_hash, user.id)
    finally:
        engine.dispose()


def insert_unscoped_audit_event(session: Session, event_type: str = "login_failed") -> None:
    """An audit row with no agency (a failed login for an unknown phone)."""
    session.add(AuditEvent(event_type=event_type, details={}))
    session.flush()


def tables_missing_rls(conn: Connection) -> list[str]:
    """Tables that hold tenant data (an ``agency_id`` column, or ``agencies``) without a
    forced row-level security policy; plus any other table nobody declared unscoped."""
    rows = conn.execute(
        text(
            """
            SELECT c.relname,
                   c.relrowsecurity AND c.relforcerowsecurity AS forced,
                   (SELECT count(*) FROM pg_policies p
                     WHERE p.schemaname = n.nspname AND p.tablename = c.relname) AS policies
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
            """
        )
    ).all()
    problems = []
    for name, forced, policies in rows:
        if name in UNSCOPED_TABLES:
            continue
        if not forced or policies < 1:
            problems.append(name)
    return sorted(problems)
