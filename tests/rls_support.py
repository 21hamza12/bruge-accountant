"""Helpers for the row-level security tests: seeded tenants and catalogue queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Connection, select, text
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import (
    AuditEvent,
    ComparisonFlag,
    ComparisonRun,
    DayAcknowledgement,
    DayEvent,
    FlagDecision,
    Partner,
    PartnerAlias,
    PartnerFee,
    ReportDay,
    Sale,
    SaleTicket,
    SaleVersion,
    StaffSignLink,
    TemporaryPassword,
    Upload,
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
    partner_id: int = 0
    report_day_id: int = 0
    sale_id: int = 0
    run_id: int = 0  # a comparison run of the seeded day (it also has one flag)
    upload_id: int = 0
    flag_id: int = 0  # the flag of that run (it also has a decision)


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
            partner = Partner(agency_id=agency_id, name="BADI", name_key="badi")
            s.add(partner)
            s.commit()
            s.add(
                PartnerFee(
                    agency_id=agency_id,
                    partner_id=partner.id,
                    fee=Decimal("500"),
                    valid_from=date(2026, 1, 1),
                    set_by_user_id=user.id,
                )
            )
            s.add(
                PartnerAlias(
                    agency_id=agency_id,
                    partner_id=partner.id,
                    alias="BADI VOYAGES",
                    alias_key="badi voyages",
                    proposed_by_user_id=user.id,
                )
            )
            s.commit()
            rows = [ticket_row(doc=f"{doc_base + n}") for n in range(2)]
            import_report(s, agency_id, parse_report(build_workbook(rows)), "seed.xlsx", SHA)
            day_id, sale_id = _seed_sale(s, agency_id, user.id, partner.id, doc_base)
            upload_id, run_id, flag_id = _seed_comparison(s, agency_id, user.id, day_id, sale_id)
            return Tenant(
                agency_id, slug, phone, token_hash, user.id, partner.id, day_id, sale_id,
                run_id, upload_id, flag_id,
            )  # fmt: skip
    finally:
        engine.dispose()


def _seed_sale(
    s: Session, agency_id: int, user_id: int, partner_id: int, doc_base: int
) -> tuple[int, int]:
    """A report day (with its event), and a partner sale with one version and one ticket."""
    day = ReportDay(agency_id=agency_id, day=date(2026, 10, 18), status="open")
    s.add(day)
    s.commit()
    s.add(
        DayEvent(
            agency_id=agency_id,
            report_day_id=day.id,
            to_status="open",
            kind="open",
            actor_user_id=user_id,
        )
    )
    sale = Sale(
        agency_id=agency_id,
        report_day_id=day.id,
        staff_sign="0004SV",
        created_by_user_id=user_id,
        current_version=1,
        status="active",
    )
    s.add(sale)
    s.commit()
    s.add(
        SaleVersion(
            agency_id=agency_id,
            sale_id=sale.id,
            version_no=1,
            report_day_id=day.id,
            staff_sign="0004SV",
            ag_kind="partner",
            partner_id=partner_id,
            ag_label="BADI",
            fee_applied=Decimal("500"),
            pb=Decimal("1500"),
            cost_total=Decimal("1000"),
            payment_method="credit",
            tickets=[],
            source="form",
            actor_user_id=user_id,
        )
    )
    s.add(SaleTicket(agency_id=agency_id, sale_id=sale.id, doc_number=f"{doc_base}", position=1))
    s.commit()
    return day.id, sale.id


def _seed_comparison(
    s: Session, agency_id: int, user_id: int, day_id: int, sale_id: int
) -> tuple[int, int, int]:
    """A comparison run of the seeded day on the seeded upload, with one flag, one decision on it
    and one acknowledgement."""
    upload_id = s.scalars(select(Upload.id).where(Upload.agency_id == agency_id)).first()
    assert upload_id is not None
    run = ComparisonRun(
        agency_id=agency_id,
        report_day_id=day_id,
        upload_id=upload_id,
        started_by_user_id=user_id,
        matched_count=0,
        flag_count=1,
        blocking_count=1,
        cancelled_count=0,
        move_count=0,
        partner_missing_count=0,
    )
    s.add(run)
    s.commit()
    flag = ComparisonFlag(
        agency_id=agency_id,
        report_day_id=day_id,
        run_id=run.id,
        doc_number="3999999999",
        fingerprint="f" * 16,
        kind="not_sent",
        sale_id=sale_id,
        responsible_sign="0004SV",
    )
    s.add(flag)
    s.commit()
    s.add(
        FlagDecision(
            agency_id=agency_id,
            flag_id=flag.id,
            stage="agent",
            choice="acknowledged",
            actor_user_id=user_id,
        )
    )
    s.add(
        DayAcknowledgement(
            agency_id=agency_id,
            report_day_id=day_id,
            kind="partner_list",
            run_id=run.id,
            digest="d" * 16,
            actor_user_id=user_id,
        )
    )
    s.commit()
    return upload_id, run.id, flag.id


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
