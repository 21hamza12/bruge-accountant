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
    AmadeusLine,
    AuditEvent,
    ComparisonFlag,
    ComparisonRun,
    DayEvent,
    Partner,
    PartnerFee,
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
    PartnerFee,
    ReportDay,
    DayEvent,
    Sale,
    SaleTicket,
    SaleVersion,
    ComparisonRun,
    ComparisonFlag,
]


def scope_column(model: Any) -> Any:
    return model.id if model is Agency else model.agency_id


@pytest.fixture
def world(rls_db: RlsDatabase) -> World:
    a = seed_tenant(rls_db.owner_url, "agency-a", "+22211111111", "a1" * 32, 3940000000)
    b = seed_tenant(rls_db.owner_url, "agency-b", "+22222222222", "b2" * 32, 3950000000)
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
        "report_days",
        "day_events",
        "sales",
        "sale_tickets",
        "sale_versions",
        "comparison_runs",
        "comparison_flags",
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
    "model", [DayEvent, SaleVersion, ComparisonRun], ids=lambda m: m.__tablename__
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
    }
    change = changes[model]
    with app_session(db) as s:
        bind_tenant(s, a.agency_id)
        before = count(s, model)
        assert before == 1
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
            "report_days",
            "sales",
            "sale_tickets",
            "comparison_flags",
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
        ("comparison_runs", "tenant_read", "SELECT"),
        ("comparison_runs", "tenant_insert", "INSERT"),
    }
    assert installed == expected
