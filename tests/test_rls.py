"""Row-level security (migration 0003), on a real Postgres, in both deployment modes.

``rls_db`` runs every test twice: ``app-role`` (the application is a separate non-owner role)
and ``single-role`` (no second role; the application is the table owner and only FORCE ROW
LEVEL SECURITY keeps the tenants apart). Skipped on SQLite, which has no row-level security.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, delete, func, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.orm import Session

from app.db import make_engine, make_session_factory
from app.models import (
    Agency,
    AgencyAiSettings,
    AgencyDetails,
    AiUsage,
    AmadeusLine,
    AuditEvent,
    ChatMessage,
    ComparisonFlag,
    ComparisonRun,
    DayAcknowledgement,
    DayEvent,
    FlagDecision,
    Partner,
    PartnerAlias,
    PartnerEntry,
    PartnerEntryDecision,
    PartnerFee,
    PartnerPayment,
    PartnerPeriod,
    RateLimitBucket,
    ReportDay,
    Sale,
    SaleTicket,
    SaleVersion,
    StaffSignLink,
    TemporaryPassword,
    Ticket,
    Upload,
    User,
    UserSession,
)
from app.tenancy import bind_tenant, lookup_scope
from tests.conftest import RlsDatabase
from tests.rls_support import (
    NOW,
    Tenant,
    insert_unscoped_audit_event,
    seed_tenant,
    tables_missing_rls,
)

World = tuple[RlsDatabase, Tenant, Tenant]

SCOPED_MODELS: list[Any] = [
    Agency,
    Upload,
    Ticket,
    User,
    StaffSignLink,
    UserSession,
    TemporaryPassword,
    AuditEvent,
    AmadeusLine,
    Partner,
    PartnerAlias,
    PartnerFee,
    ReportDay,
    DayEvent,
    Sale,
    SaleTicket,
    SaleVersion,
    ComparisonRun,
    ComparisonFlag,
    FlagDecision,
    DayAcknowledgement,
    AiUsage,
    AgencyAiSettings,
    ChatMessage,
    PartnerPeriod,
    PartnerEntry,
    PartnerEntryDecision,
    PartnerPayment,
    AgencyDetails,
]


def scope_column(model: Any) -> Any:
    return model.id if model is Agency else model.agency_id


@pytest.fixture
def world(rls_db: RlsDatabase) -> World:
    a = seed_tenant(
        rls_db.owner_url, "agency-a", "+22211111111", "a1" * 32, 3940000000, ledger=True
    )
    b = seed_tenant(
        rls_db.owner_url, "agency-b", "+22222222222", "b2" * 32, 3950000000, ledger=True
    )
    return rls_db, a, b


@contextmanager
def app_session(db: RlsDatabase) -> Iterator[Session]:
    engine = make_engine(db.app_url)
    try:
        with make_session_factory(engine)() as s:
            yield s
    finally:
        engine.dispose()


def count(s: Session, model: Any) -> int:
    return s.scalar(select(func.count()).select_from(model)) or 0


def affected(result: Any) -> int:
    rowcount: int = result.rowcount
    return rowcount


def violates_rls(exc: BaseException) -> bool:
    return "row-level security" in str(exc)


# --- fail closed ---------------------------------------------------------------------


def test_unbound_session_sees_no_rows_in_any_scoped_table(world: World) -> None:
    db, _, _ = world
    with app_session(db) as s:
        assert {m.__tablename__: count(s, m) for m in SCOPED_MODELS} == {
            m.__tablename__: 0 for m in SCOPED_MODELS
        }


def test_current_agency_helper_fails_closed(world: World) -> None:
    db, _, _ = world
    with app_session(db) as s:
        assert s.scalar(text("SELECT app_current_agency_id()")) is None
        s.execute(text("SELECT set_config('app.agency_id', '', true)"))
        assert s.scalar(text("SELECT app_current_agency_id()")) is None
        s.execute(text("SELECT set_config('app.agency_id', '7', true)"))
        assert s.scalar(text("SELECT app_current_agency_id()")) == 7
        s.rollback()
        # A value that is not a number is an error, never "all rows".
        s.execute(text("SELECT set_config('app.agency_id', 'all', true)"))
        with pytest.raises(DBAPIError):
            s.scalar(text("SELECT app_current_agency_id()"))


def test_an_emptied_setting_after_commit_still_means_zero_rows(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        s.execute(text("SELECT set_config('app.agency_id', :v, true)"), {"v": str(a.agency_id)})
        assert count(s, User) == 1
        s.commit()  # the transaction-local setting is now '' on this connection
        assert count(s, User) == 0


# --- tenant isolation ----------------------------------------------------------------


@pytest.mark.parametrize("model", SCOPED_MODELS, ids=lambda m: m.__tablename__)
def test_bound_session_sees_only_its_own_agency(world: World, model: Any) -> None:
    db, a, b = world
    for tenant, other in ((a, b), (b, a)):
        with app_session(db) as s:
            bind_tenant(s, tenant.agency_id)
            seen = set(s.scalars(select(scope_column(model))).all())
            assert seen == {tenant.agency_id}
            assert other.agency_id not in seen


def test_bound_session_never_sees_unscoped_audit_rows(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        insert_unscoped_audit_event(s)
        s.commit()
        bind_tenant(s, a.agency_id)
        assert s.scalar(select(func.count()).where(AuditEvent.agency_id.is_(None))) == 0
        assert set(s.scalars(select(AuditEvent.event_type))) == {"seed"}


def test_rate_limit_buckets_are_shared_not_tenant_scoped(world: World) -> None:
    db, _, _ = world
    with app_session(db) as s:
        s.add(RateLimitBucket(key="login:ip:1.2.3.4", window_start=NOW, count=1))
        s.commit()
    with app_session(db) as s:  # not bound to anybody, still readable and writable
        assert count(s, RateLimitBucket) == 1
        s.execute(update(RateLimitBucket).values(count=2))
        s.commit()


def test_tenant_binding_is_per_session_not_shared_between_connections(world: World) -> None:
    db, a, b = world
    with app_session(db) as one, app_session(db) as two:
        bind_tenant(one, a.agency_id)
        bind_tenant(two, b.agency_id)
        assert set(one.scalars(select(User.agency_id))) == {a.agency_id}
        assert set(two.scalars(select(User.agency_id))) == {b.agency_id}


# --- WITH CHECK ----------------------------------------------------------------------


def sale_version(owner: Tenant, *, version_no: int, partner_id: int | None = None) -> SaleVersion:
    return SaleVersion(
        agency_id=owner.agency_id,
        sale_id=owner.sale_id,
        version_no=version_no,
        report_day_id=owner.report_day_id,
        staff_sign="0004SV",
        ag_kind="partner",
        partner_id=partner_id or owner.partner_id,
        ag_label="BADI",
        fee_applied=Decimal("500"),
        pb=Decimal("1500"),
        cost_total=Decimal("1000"),
        payment_method="credit",
        tickets=[],
        source="form",
        actor_user_id=owner.user_id,
    )


def rows_for_agency_b(b: Tenant) -> dict[str, Any]:
    return {
        "users": User(agency_id=b.agency_id, phone="+22233333333", role="staff"),
        "staff_sign_links": StaffSignLink(
            agency_id=b.agency_id, user_id=b.user_id, staff_sign="ZZ"
        ),
        "user_sessions": UserSession(
            agency_id=b.agency_id,
            user_id=b.user_id,
            token_hash="d" * 64,
            csrf_token="c" * 43,
            idle_expires_at=NOW,
            absolute_expires_at=NOW,
        ),
        "temporary_passwords": TemporaryPassword(
            agency_id=b.agency_id,
            user_id=b.user_id,
            password_hash="x",  # noqa: S106
            expires_at=NOW,
        ),
        "audit_events": AuditEvent(agency_id=b.agency_id, event_type="forged", details={}),
        "partners": Partner(agency_id=b.agency_id, name="Forged", name_key="forged"),
        "partner_fees": PartnerFee(
            agency_id=b.agency_id,
            partner_id=b.partner_id,
            fee=Decimal("1"),
            valid_from=date(2026, 1, 1),
            set_by_user_id=b.user_id,
        ),
        "partner_aliases": PartnerAlias(
            agency_id=b.agency_id,
            partner_id=b.partner_id,
            alias="Forged",
            alias_key="forged",
            proposed_by_user_id=b.user_id,
        ),
        "report_days": ReportDay(agency_id=b.agency_id, day=date(2026, 10, 19), status="open"),
        "day_events": DayEvent(
            agency_id=b.agency_id,
            report_day_id=b.report_day_id,
            to_status="open",
            kind="open",
            actor_user_id=b.user_id,
        ),
        "sales": Sale(
            agency_id=b.agency_id,
            report_day_id=b.report_day_id,
            staff_sign="0004SV",
            created_by_user_id=b.user_id,
            current_version=1,
            status="active",
        ),
        "sale_tickets": SaleTicket(
            agency_id=b.agency_id, sale_id=b.sale_id, doc_number="3999999990", position=2
        ),
        "sale_versions": sale_version(b, version_no=2),
        "comparison_runs": ComparisonRun(
            agency_id=b.agency_id,
            report_day_id=b.report_day_id,
            upload_id=b.upload_id,
            matched_count=0,
            flag_count=0,
            blocking_count=0,
            cancelled_count=0,
            move_count=0,
            partner_missing_count=0,
        ),
        "comparison_flags": ComparisonFlag(
            agency_id=b.agency_id,
            report_day_id=b.report_day_id,
            run_id=b.run_id,
            doc_number="3999999998",
            fingerprint="f" * 16,
            kind="not_sent",
            responsible_sign="0004SV",
        ),
        "flag_decisions": FlagDecision(
            agency_id=b.agency_id,
            flag_id=b.flag_id,
            stage="agent",
            choice="acknowledged",
            actor_user_id=b.user_id,
        ),
        "day_acknowledgements": DayAcknowledgement(
            agency_id=b.agency_id,
            report_day_id=b.report_day_id,
            kind="partner_list",
            run_id=b.run_id,
            digest="d" * 16,
            actor_user_id=b.user_id,
        ),
        "ai_usage": AiUsage(
            agency_id=b.agency_id,
            user_id=b.user_id,
            purpose="screenshot",
            model="claude-haiku-5-5",
            input_tokens=1,
            output_tokens=1,
            cost_micro_usd=1,
            outcome="ok",
        ),
        "agency_ai_settings": AgencyAiSettings(
            agency_id=b.agency_id, enabled=True, monthly_budget_micro_usd=1
        ),
        "chat_messages": ChatMessage(
            agency_id=b.agency_id, user_id=b.user_id, role="user", text="forged"
        ),
        "partner_periods": PartnerPeriod(
            agency_id=b.agency_id,
            partner_id=b.partner_id,
            start_date=date(2026, 10, 1),
            end_date=date(2026, 10, 15),
            status="open",
        ),
        "partner_entries": PartnerEntry(
            agency_id=b.agency_id,
            partner_id=b.partner_id,
            entry_date=date(2026, 10, 18),
            kind="ADM",
            amount=Decimal("100.00"),
            reason="forged line",
            source="manual",
            created_by_user_id=b.user_id,
        ),
        "partner_entry_decisions": PartnerEntryDecision(
            agency_id=b.agency_id,
            entry_id=b.entry_id,
            entry_kind="AJUSTEMENT",
            entry_source="manual",
            decision="reject",
            decided_by_user_id=b.user_id,
        ),
        "partner_payments": PartnerPayment(
            agency_id=b.agency_id,
            partner_id=b.partner_id,
            paid_on=date(2026, 10, 18),
            amount=Decimal("1.00"),
            method="cash",
            received_by_user_id=b.user_id,
            recorded_by_user_id=b.user_id,
        ),
        "agency_details": AgencyDetails(
            agency_id=b.agency_id, name_fr="Forged", updated_by_user_id=b.user_id
        ),
        "uploads": Upload(
            agency_id=b.agency_id,
            filename="x.xlsx",
            sha256="e" * 64,
            report_range_raw="01OCT",
            report_date=NOW.date(),
            generated_on=NOW.date(),
            ticket_count=0,
            warnings=[],
        ),
    }


@pytest.mark.parametrize(
    "table",
    [
        "users",
        "staff_sign_links",
        "user_sessions",
        "temporary_passwords",
        "audit_events",
        "uploads",
        "partners",
        "partner_fees",
        "partner_aliases",
        "report_days",
        "day_events",
        "sales",
        "sale_tickets",
        "sale_versions",
        "comparison_runs",
        "comparison_flags",
        "flag_decisions",
        "day_acknowledgements",
        "ai_usage",
        "chat_messages",
        "partner_periods",
        "partner_entries",
        "partner_entry_decisions",
        "partner_payments",
        "agency_details",
    ],
)
def test_insert_for_another_agency_is_rejected(world: World, table: str) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(rows_for_agency_b(b)[table])
        with pytest.raises(DBAPIError) as info:
            s.commit()
        assert violates_rls(info.value)


def test_an_amadeus_line_for_another_agency_is_rejected(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        upload_id = s.scalars(select(Upload.id)).one()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(
            AmadeusLine(
                agency_id=b.agency_id,
                upload_id=upload_id,
                seq_no="1",
                seq_order=1,
                doc_number="3999999999",
                airline="199",
                pax_name="X",
                pax_type="ADT",
                staff_sign="S",
                rloc="R",
                trnc="TKTT",
                flags=[],
                source_row=6,
            )
        )
        with pytest.raises(DBAPIError) as info:
            s.commit()
        assert violates_rls(info.value)


def test_moving_a_row_to_another_agency_is_rejected(world: World) -> None:
    db, a, b = world
    for model in (User, Ticket, Upload):
        with app_session(db) as s:
            bind_tenant(s, a.agency_id)
            with pytest.raises(DBAPIError) as info:
                s.execute(update(model).values(agency_id=b.agency_id))
            assert violates_rls(info.value)


def test_other_agencys_rows_cannot_be_updated_or_deleted(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert (
            affected(s.execute(update(User).where(User.id == b.user_id).values(role="staff"))) == 0
        )
        assert (
            affected(s.execute(delete(UserSession).where(UserSession.agency_id == b.agency_id)))
            == 0
        )
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.scalar(select(User.role).where(User.id == b.user_id)) == "manager"
        assert count(s, UserSession) == 1


# --- the narrow pre-tenant lookups ---------------------------------------------------


def test_login_lookup_returns_only_the_exact_phone(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        lookup_scope(s, "app.login_phone", a.phone)
        assert s.scalars(select(User.phone)).all() == [a.phone]
        # Nothing else opens up with it.
        assert count(s, Agency) == 0
        assert count(s, UserSession) == 0
        assert count(s, Ticket) == 0
    for wrong in (a.phone[:-1], a.phone + "0", "%", "+222%", "", "x", b.phone.replace("2", "1")):
        with app_session(db) as s:
            lookup_scope(s, "app.login_phone", wrong)
            assert s.scalars(select(User.phone)).all() == [], wrong


def test_login_lookup_is_read_only(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        lookup_scope(s, "app.login_phone", a.phone)
        assert affected(s.execute(update(User).values(role="staff"))) == 0


def test_session_lookup_returns_only_the_exact_token_hash(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        lookup_scope(s, "app.session_token_hash", a.token_hash)
        assert s.scalars(select(UserSession.token_hash)).all() == [a.token_hash]
        assert count(s, User) == 0
        assert count(s, Agency) == 0
    for wrong in (a.token_hash[:-1], a.token_hash.upper(), "%", "", b.token_hash[::-1]):
        with app_session(db) as s:
            lookup_scope(s, "app.session_token_hash", wrong)
            assert s.scalars(select(UserSession.token_hash)).all() == [], wrong


def test_agency_slug_lookup_returns_only_that_slug(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        lookup_scope(s, "app.agency_slug", a.slug)
        assert s.scalars(select(Agency.slug)).all() == [a.slug]
        assert count(s, Ticket) == 0
        assert count(s, User) == 0
    for wrong in ("agency", "agency-a ", "AGENCY-A", "%", ""):
        with app_session(db) as s:
            lookup_scope(s, "app.agency_slug", wrong)
            assert s.scalars(select(Agency.slug)).all() == [], wrong


def test_a_lookup_does_not_survive_the_transaction(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        lookup_scope(s, "app.login_phone", a.phone)
        assert count(s, User) == 1
        s.commit()
        assert count(s, User) == 0


# --- unscoped audit events -----------------------------------------------------------


def test_anyone_may_insert_an_audit_row_without_an_agency(world: World) -> None:
    db, a, _ = world
    with app_session(db) as unbound:
        insert_unscoped_audit_event(unbound)
        unbound.commit()
    with app_session(db) as bound:
        bind_tenant(bound, a.agency_id)
        insert_unscoped_audit_event(bound)
        bound.commit()
    # ...but nobody can read them back through the tenant policy.
    with app_session(db) as s:
        assert count(s, AuditEvent) == 0
        bind_tenant(s, a.agency_id)
        assert s.scalar(select(func.count()).where(AuditEvent.agency_id.is_(None))) == 0


def test_an_unbound_session_cannot_write_an_audit_row_for_an_agency(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        s.add(AuditEvent(agency_id=a.agency_id, event_type="forged", details={}))
        with pytest.raises(DBAPIError) as info:
            s.commit()
        assert violates_rls(info.value)


def test_the_orm_can_write_an_unscoped_audit_row_but_not_ask_for_it_back(world: World) -> None:
    """AuditEvent turns implicit RETURNING off: the id comes from the sequence beforehand, because
    RETURNING needs SELECT on a row that no policy lets anybody read."""
    db, _, _ = world
    with app_session(db) as s:
        event = AuditEvent(event_type="login_failed", details={})
        s.add(event)
        s.commit()
        assert isinstance(event.id, int)
    with app_session(db) as s:
        with pytest.raises(DBAPIError) as info:
            s.execute(
                insert(AuditEvent).values(event_type="x", details={}).returning(AuditEvent.id)
            )
        assert violates_rls(info.value)


def test_audit_rows_are_append_only_for_the_tenant(world: World) -> None:
    """The only DELETE policy on audit_events is the 400-day retention one, so a recent row cannot
    be edited or removed, not even by the owner in single-role mode."""
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        if db.mode == "app-role":
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(update(AuditEvent).values(event_type="edited"))
            s.rollback()
        else:
            assert affected(s.execute(update(AuditEvent).values(event_type="edited"))) == 0
        assert affected(s.execute(delete(AuditEvent))) == 0  # granted, but no policy matches
        s.rollback()
        assert s.scalars(select(AuditEvent.event_type)).all() == ["seed"]


def test_amadeus_lines_are_append_only_for_the_tenant(world: World) -> None:
    """No UPDATE and no DELETE policy: a stored line cannot be edited or removed, not even by the
    owner in single-role mode (the app role has no such privilege at all)."""
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        before = s.execute(select(AmadeusLine.id, AmadeusLine.total_doc)).all()
        assert len(before) == 2
        for statement in (
            update(AmadeusLine).values(total_doc=1),
            delete(AmadeusLine),
        ):
            if db.mode == "app-role":
                with pytest.raises(ProgrammingError, match="permission denied"):
                    s.execute(statement)
                s.rollback()
                bind_tenant(s, a.agency_id)
            else:
                assert affected(s.execute(statement)) == 0
        s.rollback()
        assert s.execute(select(AmadeusLine.id, AmadeusLine.total_doc)).all() == before


def test_amadeus_lines_grants_are_select_and_insert_only(app_role_world: World) -> None:
    db, _, _ = app_role_world
    engine = create_engine(db.owner_url)
    try:
        with engine.connect() as conn:
            privileges = set(
                conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'amadeus_lines' AND grantee = :g"
                    ),
                    {"g": make_url(db.app_url).username},
                ).scalars()
            )
    finally:
        engine.dispose()
    assert privileges == {"SELECT", "INSERT"}


def test_chat_messages_grants_are_select_and_insert_only(app_role_world: World) -> None:
    db, _, _ = app_role_world
    engine = create_engine(db.owner_url)
    try:
        with engine.connect() as conn:
            privileges = set(
                conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'chat_messages' AND grantee = :g"
                    ),
                    {"g": make_url(db.app_url).username},
                ).scalars()
            )
    finally:
        engine.dispose()
    assert privileges == {"SELECT", "INSERT"}


def test_a_chat_message_of_another_agency_is_invisible_and_cannot_be_forged_for_a_user(
    world: World,
) -> None:
    """Row-level security is per agency; the per-person rule (nobody reads another person's chat)
    is the application's: the composite key still ties every message to a user of its own agency."""
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert set(s.scalars(select(ChatMessage.text))) == {"aide"}
        s.add(ChatMessage(agency_id=a.agency_id, user_id=b.user_id, role="user", text="x"))
        with pytest.raises(DBAPIError):  # a user of agency b is not a user of agency a
            s.commit()


# --- partner accounts (migration 0017) -----------------------------------------------


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("partner_periods", {"SELECT", "INSERT", "UPDATE"}),
        ("partner_entries", {"SELECT", "INSERT"}),
        ("partner_entry_decisions", {"SELECT", "INSERT"}),
        ("partner_payments", {"SELECT", "INSERT"}),
    ],
)
def test_partner_account_grants(app_role_world: World, table: str, expected: set[str]) -> None:
    db, _, _ = app_role_world
    engine = create_engine(db.owner_url)
    try:
        with engine.connect() as conn:
            privileges = set(
                conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = :t AND grantee = :g"
                    ),
                    {"t": table, "g": make_url(db.app_url).username},
                ).scalars()
            )
    finally:
        engine.dispose()
    assert privileges == expected


def _close(s: Session, tenant: Tenant) -> int:
    statement = update(PartnerPeriod).where(PartnerPeriod.id == tenant.period_id)
    return affected(s.execute(statement.values(**_frozen(tenant))))


def _frozen(tenant: Tenant) -> dict[str, Any]:
    """A close that reconciles: 0 + 0 + 250 - 20 000 = -19 750."""
    return {
        "status": "closed",
        "opening_balance": Decimal("0.00"),
        "sales_total": Decimal("0.00"),
        "entries_total": Decimal("250.00"),
        "payments_total": Decimal("20000.00"),
        "closing_balance": Decimal("-19750.00"),
        "closed_at": NOW,
        "closed_by_user_id": tenant.user_id,
        "snapshot": {"sales": [], "entries": [], "payments": []},
    }


def test_the_tenant_closes_its_open_period_once_and_never_again(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert _close(s, a) == 1
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        # tenant_update only matches open rows: a closed period is out of reach of any UPDATE.
        assert affected(s.execute(update(PartnerPeriod).values(status="open"))) == 0
        assert (
            affected(s.execute(update(PartnerPeriod).values(closing_balance=Decimal("0.00")))) == 0
        )
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert s.execute(select(PartnerPeriod.status, PartnerPeriod.closing_balance)).all() == [
            ("closed", Decimal("-19750.00"))
        ]


def test_a_period_is_never_deleted(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        if db.mode == "app-role":
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(delete(PartnerPeriod))
            s.rollback()
            bind_tenant(s, a.agency_id)
        else:  # the owner: no DELETE policy, so nothing matches
            assert affected(s.execute(delete(PartnerPeriod))) == 0
            s.commit()
        assert count(s, PartnerPeriod) == 1


def test_another_agencys_period_cannot_be_closed_or_moved(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert _close(s, b) == 0  # b's open period is invisible to a
        with pytest.raises(DBAPIError) as info:
            s.execute(
                update(PartnerPeriod)
                .where(PartnerPeriod.id == a.period_id)
                .values(agency_id=b.agency_id)
            )
        # The identity trigger runs before the policy's WITH CHECK; either one refuses.
        assert "changes only by its close" in str(info.value) or violates_rls(info.value)
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.scalars(select(PartnerPeriod.status)).all() == ["open"]


def test_a_period_is_born_open_on_postgres(world: World) -> None:
    """Item 4: ``tenant_insert`` on partner_periods admits open rows only."""
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(DBAPIError) as info:
            s.execute(
                insert(PartnerPeriod).values(
                    agency_id=a.agency_id,
                    partner_id=a.partner_id,
                    start_date=date(2026, 10, 1),
                    end_date=date(2026, 10, 15),
                    **_frozen(a),
                )
            )
        assert violates_rls(info.value)


@pytest.mark.parametrize("column", ["dates", "partner_id", "created_at"])
def test_the_close_cannot_change_the_identity_of_a_period(world: World, column: str) -> None:
    """Item 3: the BEFORE UPDATE trigger; even the close keeps partner, dates and creation."""
    db, a, _ = world
    changes: dict[str, dict[str, Any]] = {
        "dates": {"start_date": date(2026, 11, 16), "end_date": date(2026, 11, 30)},
        "partner_id": {"partner_id": a.partner_id + 1000},
        "created_at": {"created_at": NOW},
    }
    change = changes[column]
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        statement = update(PartnerPeriod).where(PartnerPeriod.id == a.period_id)
        with pytest.raises(DBAPIError, match="changes only by its close"):
            s.execute(statement.values(**{**_frozen(a), **change}))


def test_an_update_that_does_not_close_the_period_is_refused(world: World) -> None:
    """Item 3: ``tenant_update`` WITH CHECK admits closed rows only; the trigger agrees."""
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(DBAPIError) as info:
            s.execute(update(PartnerPeriod).values(status="open"))
        assert "changes only by its close" in str(info.value) or violates_rls(info.value)


def test_a_close_that_does_not_reconcile_is_refused(world: World) -> None:
    """Item 3, Postgres only: closing = opening + sales + entries - payments, exactly."""
    db, a, _ = world
    wrong = {**_frozen(a), "closing_balance": Decimal("-19749.99")}
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        statement = update(PartnerPeriod).where(PartnerPeriod.id == a.period_id)
        with pytest.raises(DBAPIError, match="closing_reconciles"):
            s.execute(statement.values(**wrong))


def test_nothing_lands_in_a_closed_period_on_postgres(world: World) -> None:
    """Item 1 and N3: the plpgsql BEFORE INSERT triggers (advisory lock, then the check), as the
    app role and as the owner in single-role mode. Nothing dated on or before the end of the
    last closed period lands, even in a half-month that never had a row."""
    db, a, _ = world
    inside = date(2026, 10, 20)  # in the seeded 16-31 Oct period
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add_all(
            [_line(a, inside, kind="AJUSTEMENT", amount=Decimal("10.00")), _payment(a, inside)]
        )
        s.commit()  # the period is open: accepted
        pending = s.scalars(select(PartnerEntry.id).where(PartnerEntry.entry_date == inside)).one()
        assert _close(s, a) == 1
        s.commit()
    late_decision = PartnerEntryDecision(
        agency_id=a.agency_id,
        entry_id=pending,
        entry_kind="AJUSTEMENT",
        entry_source="manual",
        decision="approve",
        decided_by_user_id=a.user_id,
    )
    backdated = date(2026, 10, 5)  # 1-15 Oct never had a row, but 16-31 Oct is closed
    for row in (
        _line(a, inside),
        _payment(a, inside),
        late_decision,
        _line(a, backdated),
        _payment(a, backdated),
    ):
        with app_session(db) as s:
            bind_tenant(s, a.agency_id)
            s.add(row)
            with pytest.raises(DBAPIError, match="closed partner period ends on or after"):
                s.commit()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add_all([_line(a, date(2026, 11, 2)), _payment(a, date(2026, 11, 2))])
        s.commit()  # after the last closed period: accepted


def _line(owner: Tenant, day: date, **kw: Any) -> PartnerEntry:
    fields: dict[str, Any] = {
        "agency_id": owner.agency_id,
        "partner_id": owner.partner_id,
        "entry_date": day,
        "kind": "ADM",
        "amount": Decimal("100.00"),
        "reason": "pénalité compagnie",
        "source": "manual",
        "created_by_user_id": owner.user_id,
    }
    fields.update(kw)
    return PartnerEntry(**fields)


def _payment(owner: Tenant, day: date) -> PartnerPayment:
    return PartnerPayment(
        agency_id=owner.agency_id,
        partner_id=owner.partner_id,
        paid_on=day,
        amount=Decimal("1.00"),
        method="cash",
        received_by_user_id=owner.user_id,
        recorded_by_user_id=owner.user_id,
    )


def test_a_line_cannot_be_written_for_another_agencys_partner(world: World) -> None:
    """The composite key ties every line, decision and payment to a row of its own agency."""
    db, a, b = world
    forged = [
        PartnerEntry(
            agency_id=a.agency_id,
            partner_id=b.partner_id,
            entry_date=date(2026, 10, 18),
            kind="ADM",
            amount=Decimal("1.00"),
            reason="forged line",
            source="manual",
            created_by_user_id=a.user_id,
        ),
        PartnerEntryDecision(
            agency_id=a.agency_id,
            entry_id=b.entry_id,
            entry_kind="AJUSTEMENT",
            entry_source="manual",
            decision="approve",
            decided_by_user_id=a.user_id,
        ),
        PartnerPayment(
            agency_id=a.agency_id,
            partner_id=b.partner_id,
            paid_on=date(2026, 10, 18),
            amount=Decimal("1.00"),
            method="cash",
            received_by_user_id=a.user_id,
            recorded_by_user_id=a.user_id,
        ),
    ]
    for row in forged:
        with app_session(db) as s:
            bind_tenant(s, a.agency_id)
            s.add(row)
            with pytest.raises(DBAPIError):
                s.commit()


# --- partners (migration 0007) -------------------------------------------------------


def test_partner_rows_of_another_agency_cannot_be_updated_or_deleted(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        other = update(Partner).where(Partner.id == b.partner_id).values(is_active=False)
        assert affected(s.execute(other)) == 0
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        other_delete = delete(Partner).where(Partner.id == b.partner_id)
        if db.mode == "app-role":  # no DELETE privilege at all
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(other_delete)
        else:  # the owner: no policy row matches
            assert affected(s.execute(other_delete)) == 0
        s.rollback()
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.scalar(select(Partner.is_active).where(Partner.id == b.partner_id)) is True


def test_a_partner_cannot_be_moved_to_another_agency(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(DBAPIError) as info:
            s.execute(update(Partner).values(agency_id=b.agency_id))
        assert violates_rls(info.value)


def test_a_fee_for_a_partner_of_another_agency_is_refused(world: World) -> None:
    """Even with the right agency_id on the row, the composite foreign key refuses it."""
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(
            PartnerFee(
                agency_id=a.agency_id,
                partner_id=b.partner_id,
                fee=Decimal("1"),
                valid_from=date(2026, 1, 1),
                set_by_user_id=a.user_id,
            )
        )
        with pytest.raises(DBAPIError):
            s.commit()


def test_partner_fees_are_append_only_for_the_tenant(world: World) -> None:
    """tenant_read and tenant_insert only: no UPDATE or DELETE policy, so a fee entry cannot be
    edited or removed, not even by the owner in single-role mode (the app role also lacks the
    privileges)."""
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        if db.mode == "app-role":
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(update(PartnerFee).values(fee=Decimal("1")))
            s.rollback()
            bind_tenant(s, a.agency_id)
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(delete(PartnerFee))
            s.rollback()
            bind_tenant(s, a.agency_id)
        else:
            assert affected(s.execute(update(PartnerFee).values(fee=Decimal("1")))) == 0
            assert affected(s.execute(delete(PartnerFee))) == 0
        s.rollback()
        bind_tenant(s, a.agency_id)
        assert s.scalars(select(PartnerFee.fee)).all() == [Decimal("500.00")]


def test_partner_fees_can_be_inserted_for_the_bound_tenant(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(
            PartnerFee(
                agency_id=a.agency_id,
                partner_id=a.partner_id,
                fee=Decimal("300"),
                valid_from=date(2026, 7, 16),
                set_by_user_id=a.user_id,
            )
        )
        s.commit()
        assert count(s, PartnerFee) == 2


def test_the_app_role_cannot_delete_partners(app_role_world: World) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(delete(Partner))


def test_the_app_role_cannot_delete_partner_aliases(app_role_world: World) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(delete(PartnerAlias))


# --- report days and sales (migration 0008) -----------------------------------------


def test_a_sale_row_cannot_be_moved_to_another_agency(world: World) -> None:
    db, a, b = world
    for model in (ReportDay, Sale, SaleTicket):
        with app_session(db) as s:
            bind_tenant(s, a.agency_id)
            with pytest.raises(DBAPIError) as info:
                s.execute(update(model).values(agency_id=b.agency_id))
            if model is SaleTicket and db.mode == "app-role":  # no UPDATE privilege at all
                assert "permission denied" in str(info.value)
            else:
                assert violates_rls(info.value)


def test_sales_of_another_agency_cannot_be_updated_or_deleted(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        voided = update(Sale).where(Sale.id == b.sale_id).values(status="voided")
        assert affected(s.execute(voided)) == 0
        closed = update(ReportDay).where(ReportDay.id == b.report_day_id).values(status="closed")
        assert affected(s.execute(closed)) == 0
        assert affected(s.execute(delete(SaleTicket).where(SaleTicket.sale_id == b.sale_id))) == 0
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.scalar(select(Sale.status).where(Sale.id == b.sale_id)) == "active"
        assert count(s, SaleTicket) == 1


def test_the_tenant_may_edit_its_sale_and_delete_its_tickets(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert affected(s.execute(update(Sale).values(current_version=2))) == 1
        assert affected(s.execute(update(ReportDay).values(status="compared"))) == 1
        assert affected(s.execute(delete(SaleTicket))) == 1
        s.commit()
        assert count(s, SaleTicket) == 0


@pytest.mark.parametrize(
    "model",
    [
        DayEvent,
        SaleVersion,
        ComparisonRun,
        AiUsage,
        ChatMessage,
        PartnerEntry,
        PartnerEntryDecision,
        PartnerPayment,
    ],
    ids=lambda m: m.__tablename__,
)
def test_day_events_and_sale_versions_are_append_only_for_the_tenant(
    world: World, model: Any
) -> None:
    """tenant_read and tenant_insert only: no UPDATE or DELETE policy, so a row cannot be edited
    or removed, not even by the owner in single-role mode (the app role lacks the privileges)."""
    db, a, _ = world
    changes: dict[Any, dict[str, Any]] = {
        DayEvent: {"reason": "edited"},
        SaleVersion: {"pb": Decimal("1")},
        ComparisonRun: {"matched_count": 9},
        AiUsage: {"cost_micro_usd": 0},
        ChatMessage: {"text": "edited"},
        PartnerEntry: {"amount": Decimal("1.00")},
        PartnerEntryDecision: {"decision": "reject"},
        PartnerPayment: {"amount": Decimal("1.00")},
    }
    change = changes[model]
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        before = count(s, model)
        assert before == (2 if model is PartnerEntry else 1)
        for statement in (update(model).values(**change), delete(model)):
            if db.mode == "app-role":
                with pytest.raises(ProgrammingError, match="permission denied"):
                    s.execute(statement)
                s.rollback()
                bind_tenant(s, a.agency_id)
            else:
                assert affected(s.execute(statement)) == 0
        s.rollback()
        bind_tenant(s, a.agency_id)
        assert count(s, model) == before
        if model is SaleVersion:
            assert s.scalars(select(SaleVersion.pb)).all() == [Decimal("1500.00")]


def test_the_tenant_reads_its_own_ai_settings_and_cannot_write_them(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert s.scalars(select(AgencyAiSettings.agency_id)).all() == [a.agency_id]
        if db.mode == "app-role":  # SELECT only: the ai-budget command writes it as the owner
            for statement in (
                update(AgencyAiSettings).values(enabled=False),
                delete(AgencyAiSettings),
            ):
                with pytest.raises(ProgrammingError, match="permission denied"):
                    s.execute(statement)
                s.rollback()
                bind_tenant(s, a.agency_id)
            s.add(rows_for_agency_b(b)["agency_ai_settings"])
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.commit()
        else:  # single-role mode has no grants: row-level security alone finds nothing of B's
            other = update(AgencyAiSettings).where(AgencyAiSettings.agency_id == b.agency_id)
            assert affected(s.execute(other.values(monthly_budget_micro_usd=0))) == 0
            s.rollback()
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.scalars(select(AgencyAiSettings.monthly_budget_micro_usd)).all() == [5_000_000]


def test_the_month_to_date_sum_only_counts_the_tenant(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert s.scalar(select(func.sum(AiUsage.cost_micro_usd))) == 795


def test_chat_usage_rows_are_tenant_isolated(world: World) -> None:
    """Phase E part 2: the policies of 0014 cover the new purpose (0016 only widens a CHECK)."""
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(
            AiUsage(
                agency_id=a.agency_id,
                user_id=a.user_id,
                purpose="chat",
                model="claude-haiku-5-5",
                input_tokens=2500,
                output_tokens=100,
                cost_micro_usd=300,
                outcome="ok",
            )
        )
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.scalars(select(AiUsage).where(AiUsage.purpose == "chat")).all() == []
        assert s.scalar(select(func.sum(AiUsage.cost_micro_usd))) == 795  # B's own row only
        s.add(
            AiUsage(
                agency_id=a.agency_id,  # a chat row for A written as B
                user_id=a.user_id,
                purpose="chat",
                model="claude-haiku-5-5",
                input_tokens=1,
                output_tokens=1,
                cost_micro_usd=1,
                outcome="ok",
            )
        )
        with pytest.raises(DBAPIError):
            s.commit()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert s.scalar(select(func.sum(AiUsage.cost_micro_usd))) == 795 + 300


def test_ai_usage_of_another_agencys_user_is_refused_by_the_foreign_key(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(
            AiUsage(
                agency_id=a.agency_id,
                user_id=b.user_id,
                purpose="screenshot",
                model="claude-haiku-5-5",
                input_tokens=1,
                output_tokens=1,
                cost_micro_usd=1,
                outcome="ok",
            )
        )
        with pytest.raises(DBAPIError) as info:
            s.commit()
        assert "foreign key" in str(info.value)


def test_a_sale_version_can_be_appended_for_the_bound_tenant(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(sale_version(a, version_no=2))
        s.commit()
        assert count(s, SaleVersion) == 2


def test_a_version_for_a_partner_of_another_agency_is_refused(world: World) -> None:
    """The row passes the policy (right agency_id) and the composite foreign key refuses it."""
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(sale_version(a, version_no=2, partner_id=b.partner_id))
        with pytest.raises(DBAPIError) as info:
            s.commit()
        assert "foreign key" in str(info.value)


def test_a_day_compared_with_an_upload_of_another_agency_is_refused(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        upload_id = s.scalars(select(Upload.id)).first()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        s.add(
            ReportDay(
                agency_id=a.agency_id,
                day=date(2026, 10, 17),
                status="compared",
                compared_upload_id=upload_id,
            )
        )
        with pytest.raises(DBAPIError) as info:
            s.commit()
        assert "foreign key" in str(info.value)


def test_a_ticket_number_is_unique_per_agency_under_rls(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        taken = s.scalars(select(SaleTicket.doc_number)).one()
        s.add(SaleTicket(agency_id=a.agency_id, sale_id=a.sale_id, doc_number=taken, position=2))
        with pytest.raises(DBAPIError, match="uq_sale_tickets_agency_doc_number"):
            s.commit()
    with app_session(db) as s:  # the same number in another agency is a different ticket
        bind_tenant(s, b.agency_id)
        s.add(SaleTicket(agency_id=b.agency_id, sale_id=b.sale_id, doc_number=taken, position=2))
        s.commit()


def test_the_app_role_has_delete_only_on_sale_tickets(app_role_world: World) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        for model in (Sale, ReportDay):
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(delete(model))
            s.rollback()
            bind_tenant(s, a.agency_id)
        assert affected(s.execute(delete(SaleTicket))) == 1


# --- what the app role may not do (separate role only) -------------------------------


@pytest.fixture
def app_role_world(world: World) -> World:
    if world[0].mode != "app-role":
        pytest.skip("grants only exist when a separate app role is configured")
    return world


@pytest.mark.parametrize("model", [Ticket, Upload, User, Agency, AmadeusLine])
def test_app_role_cannot_delete_where_deleting_is_not_needed(
    app_role_world: World, model: Any
) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(delete(model))


@pytest.mark.parametrize("statement", ["TRUNCATE tickets", "CREATE TABLE intruder (id int)"])
def test_app_role_cannot_truncate_or_create(app_role_world: World, statement: str) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(text(statement))


def test_app_role_cannot_create_agencies_or_edit_the_schema_bookkeeping(
    app_role_world: World,
) -> None:
    db, _, _ = app_role_world
    with app_session(db) as s:
        s.add(Agency(slug="new-one", name="New"))
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.commit()
    with app_session(db) as s:
        assert s.scalar(text("SELECT count(*) FROM alembic_version")) == 1
        with pytest.raises(ProgrammingError, match="permission denied"):
            s.execute(text("UPDATE alembic_version SET version_num = 'x'"))


def test_app_role_can_delete_sessions_sign_links_and_rate_limit_buckets(
    app_role_world: World,
) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        s.add(RateLimitBucket(key="k", window_start=NOW, count=1))
        s.commit()
        bind_tenant(s, a.agency_id)
        assert affected(s.execute(delete(UserSession))) == 1
        assert affected(s.execute(delete(StaffSignLink))) == 1
        assert affected(s.execute(delete(RateLimitBucket))) == 1
        s.commit()


def test_app_role_can_update_tickets_in_its_tenant(app_role_world: World) -> None:
    db, a, _ = app_role_world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert affected(s.execute(update(Ticket).values(airline="999"))) == 2
        s.commit()


# --- the guard that protects Phase 3 tables ------------------------------------------


def test_every_table_with_tenant_data_has_forced_rls_and_a_policy(world: World) -> None:
    db, _, _ = world
    engine = create_engine(db.owner_url)
    try:
        with engine.connect() as conn:
            assert tables_missing_rls(conn) == []
            with_agency_id = set(
                conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND column_name = 'agency_id'"
                    )
                ).scalars()
            )
            flags = {
                name: (rls, forced)
                for name, rls, forced in conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'"
                    )
                )
            }
    finally:
        engine.dispose()
    assert {"uploads", "tickets", "users", "audit_events"} <= with_agency_id
    for table in with_agency_id | {"agencies"}:
        assert flags[table] == (True, True), table
    assert flags["rate_limit_buckets"] == (False, False)


def test_the_guard_catches_a_future_table_without_rls(world: World) -> None:
    db, _, _ = world
    engine = create_engine(db.owner_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE phase3_prices (id int, agency_id int)"))
            conn.execute(text("CREATE TABLE phase3_half (id int, agency_id int)"))
            conn.execute(text("ALTER TABLE phase3_half ENABLE ROW LEVEL SECURITY"))
            conn.execute(text("ALTER TABLE phase3_half FORCE ROW LEVEL SECURITY"))
            assert tables_missing_rls(conn) == ["phase3_half", "phase3_prices"]  # no policy yet
            conn.execute(
                text("CREATE POLICY p ON phase3_half USING (agency_id = app_current_agency_id())")
            )
            assert tables_missing_rls(conn) == ["phase3_prices"]
            conn.rollback()
    finally:
        engine.dispose()


def test_the_exact_policies_installed(world: World) -> None:
    db, _, _ = world
    engine = create_engine(db.owner_url)
    try:
        with engine.connect() as conn:
            installed = {
                (t, n, cmd)
                for t, n, cmd in conn.execute(
                    text(
                        "SELECT tablename, policyname, cmd FROM pg_policies "
                        "WHERE schemaname = 'public'"
                    )
                )
            }
    finally:
        engine.dispose()
    expected = {
        (t, "tenant_isolation", "ALL")
        for t in (
            "agencies",
            "uploads",
            "tickets",
            "users",
            "staff_sign_links",
            "user_sessions",
            "temporary_passwords",
            "partners",
            "partner_aliases",
            "report_days",
            "sales",
            "sale_tickets",
            "comparison_flags",
            "agency_ai_settings",
        )
    } | {
        ("partner_fees", "tenant_read", "SELECT"),
        ("partner_fees", "tenant_insert", "INSERT"),
        ("users", "login_lookup", "SELECT"),
        ("user_sessions", "session_lookup", "SELECT"),
        ("agencies", "slug_lookup", "SELECT"),
        ("audit_events", "tenant_read", "SELECT"),
        ("audit_events", "tenant_or_unscoped_insert", "INSERT"),
        ("user_sessions", "purge_expired", "DELETE"),
        ("temporary_passwords", "purge_expired", "DELETE"),
        ("audit_events", "retention_purge", "DELETE"),
        ("amadeus_lines", "tenant_read", "SELECT"),
        ("amadeus_lines", "tenant_insert", "INSERT"),
        ("day_events", "tenant_read", "SELECT"),
        ("day_events", "tenant_insert", "INSERT"),
        ("sale_versions", "tenant_read", "SELECT"),
        ("sale_versions", "tenant_insert", "INSERT"),
        ("day_exports", "tenant_read", "SELECT"),
        ("day_exports", "tenant_insert", "INSERT"),
        ("comparison_runs", "tenant_read", "SELECT"),
        ("comparison_runs", "tenant_insert", "INSERT"),
        ("flag_decisions", "tenant_read", "SELECT"),
        ("flag_decisions", "tenant_insert", "INSERT"),
        ("day_acknowledgements", "tenant_read", "SELECT"),
        ("day_acknowledgements", "tenant_insert", "INSERT"),
        ("ai_usage", "tenant_read", "SELECT"),
        ("ai_usage", "tenant_insert", "INSERT"),
        ("chat_messages", "tenant_read", "SELECT"),
        ("chat_messages", "tenant_insert", "INSERT"),
        ("partner_periods", "tenant_read", "SELECT"),
        ("partner_periods", "tenant_insert", "INSERT"),
        ("partner_periods", "tenant_update", "UPDATE"),
        ("partner_entries", "tenant_read", "SELECT"),
        ("partner_entries", "tenant_insert", "INSERT"),
        ("partner_entry_decisions", "tenant_read", "SELECT"),
        ("partner_entry_decisions", "tenant_insert", "INSERT"),
        ("partner_payments", "tenant_read", "SELECT"),
        ("partner_payments", "tenant_insert", "INSERT"),
        ("agency_details", "tenant_read", "SELECT"),
        ("agency_details", "tenant_insert", "INSERT"),
        ("agency_details", "tenant_update", "UPDATE"),
    }
    assert installed == expected


# --- the agency's details (migration 0018) ----------------------------------------------


def test_agency_details_grants(app_role_world: World) -> None:
    db, _, _ = app_role_world
    engine = create_engine(db.owner_url)
    try:
        with engine.connect() as conn:
            privileges = set(
                conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'agency_details' AND grantee = :g"
                    ),
                    {"g": make_url(db.app_url).username},
                ).scalars()
            )
    finally:
        engine.dispose()
    assert privileges == {"SELECT", "INSERT", "UPDATE"}


def test_the_tenant_updates_its_own_details_only(world: World) -> None:
    db, a, b = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert affected(s.execute(update(AgencyDetails).values(nif="11111111"))) == 1
        assert (
            affected(
                s.execute(
                    update(AgencyDetails)
                    .where(AgencyDetails.agency_id == b.agency_id)
                    .values(name_fr="Forged")
                )
            )
            == 0
        )  # b's row is invisible to a
        s.commit()
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        with pytest.raises(DBAPIError) as info:
            s.execute(update(AgencyDetails).values(agency_id=b.agency_id))
        assert violates_rls(info.value) or "foreign key" in str(info.value)
    with app_session(db) as s:
        bind_tenant(s, b.agency_id)
        assert s.execute(select(AgencyDetails.name_fr, AgencyDetails.nif)).all() == [
            ("Agence Test", "00000000")
        ]
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        assert s.scalars(select(AgencyDetails.nif)).all() == ["11111111"]


def test_the_details_are_never_deleted(world: World) -> None:
    db, a, _ = world
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        if db.mode == "app-role":
            with pytest.raises(ProgrammingError, match="permission denied"):
                s.execute(delete(AgencyDetails))
            s.rollback()
            bind_tenant(s, a.agency_id)
        else:  # the owner: no DELETE policy, so nothing matches
            assert affected(s.execute(delete(AgencyDetails))) == 0
            s.commit()
        assert count(s, AgencyDetails) == 1
