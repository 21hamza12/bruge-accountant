"""Migration 0017: the partner account tables (SQLite here; the Postgres policies and grants are
covered by tests/test_rls.py when a server is given, which this suite never needs locally)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, func, insert, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import (
    ClosedPeriodError,
    Partner,
    PartnerEntry,
    PartnerEntryDecision,
    PartnerPayment,
    PartnerPeriod,
    ReportDay,
    Sale,
    User,
)
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate

TABLES = ("partner_periods", "partner_entries", "partner_entry_decisions", "partner_payments")
APPEND_ONLY = TABLES[1:]
CLOSED_AT = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


@dataclass(frozen=True)
class W:
    url: str
    a: int
    b: int
    user_a: int
    user_b: int
    partner_a: int
    partner_b: int
    sale_a: int


def _world(url: str) -> W:
    """Two agencies, each with a manager and a partner ("Partenaire Test"); a has one sale."""
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            ids: dict[str, int] = {}
            for key, slug, phone in (
                ("a", "agence-a", "+22212390101"),
                ("b", "agence-b", "+22212390102"),
            ):
                agency = create_agency(s, slug, slug.upper()).id
                rebind_tenant(s, agency)
                user = User(agency_id=agency, phone=phone, role="manager")
                partner = Partner(
                    agency_id=agency, name="Partenaire Test", name_key="partenaire test"
                )
                s.add_all([user, partner])
                s.commit()
                ids.update({key: agency, f"user_{key}": user.id, f"partner_{key}": partner.id})
            rebind_tenant(s, ids["a"])
            day = ReportDay(agency_id=ids["a"], day=date(2026, 10, 3), status="open")
            s.add(day)
            s.commit()
            sale = Sale(
                agency_id=ids["a"],
                report_day_id=day.id,
                staff_sign="0004SV",
                created_by_user_id=ids["user_a"],
            )
            s.add(sale)
            s.commit()
            return W(url, sale_a=sale.id, **ids)
    finally:
        engine.dispose()


def period(w: W, **kw: Any) -> PartnerPeriod:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "partner_id": w.partner_a,
        "start_date": date(2026, 10, 1),
        "end_date": date(2026, 10, 15),
        "status": "open",
    }
    fields.update(kw)
    return PartnerPeriod(**fields)


def frozen(w: W) -> dict[str, Any]:
    return {
        "status": "closed",
        "opening_balance": Decimal("50000.00"),
        "sales_total": Decimal("31968.00"),
        "entries_total": Decimal("-1005.00"),
        "payments_total": Decimal("80000.00"),
        "closing_balance": Decimal("963.00"),
        "closed_at": CLOSED_AT,
        "closed_by_user_id": w.user_a,
        "snapshot": {"sales": [], "entries": [], "payments": []},
    }


def entry(w: W, **kw: Any) -> PartnerEntry:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "partner_id": w.partner_a,
        "entry_date": date(2026, 10, 5),
        "kind": "ADM",
        "amount": Decimal("11306.00"),
        "reason": "pénalité compagnie",
        "source": "manual",
        "created_by_user_id": w.user_a,
    }
    fields.update(kw)
    return PartnerEntry(**fields)


def payment(w: W, **kw: Any) -> PartnerPayment:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "partner_id": w.partner_a,
        "paid_on": date(2026, 10, 7),
        "amount": Decimal("20000.00"),
        "method": "cash",
        "reference": None,
        "received_by_user_id": w.user_a,
        "recorded_by_user_id": w.user_a,
    }
    fields.update(kw)
    return PartnerPayment(**fields)


def _add(w: W, agency: int, *rows: Any) -> list[int]:
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.add_all(rows)
            s.commit()
            return [row.id for row in rows]
    finally:
        engine.dispose()


def _decision(w: W, entry_id: int, **kw: Any) -> PartnerEntryDecision:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "entry_id": entry_id,
        "entry_kind": "AJUSTEMENT",
        "entry_source": "manual",
        "decision": "approve",
        "decided_by_user_id": w.user_a,
    }
    fields.update(kw)
    return PartnerEntryDecision(**fields)


def _close(w: W, period_id: int, agency: int | None = None, **overrides: Any) -> None:
    """Close an open period the only legal way: one UPDATE of its frozen columns."""
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency or w.a)
            row = s.get(PartnerPeriod, period_id)
            assert row is not None
            for name, value in {**frozen(w), **overrides}.items():
                setattr(row, name, value)
            s.commit()
    finally:
        engine.dispose()


def _count(w: W, agency: int, model: Any) -> int:
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            query = select(func.count()).select_from(model).where(model.agency_id == agency)
            return s.scalar(query) or 0
    finally:
        engine.dispose()


def _names(url: str, kind: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = :t"), {"t": kind})
            return {str(n) for n in rows.scalars()}
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# --- chain, grants and shape -------------------------------------------------------------------


def test_0017_follows_0016_and_is_the_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0017")
    assert revision is not None and revision.down_revision == "0016"
    assert len(script.get_heads()) == 1  # 0018 is the head now: see test_migration_0018


def test_grants_follow_the_design() -> None:
    assert GRANTS["partner_periods"] == "SELECT, INSERT, UPDATE"  # closing; never DELETE
    for table in APPEND_ONLY:
        assert GRANTS[table] == "SELECT, INSERT", table


def _ours(name: str) -> bool:
    return any(table in name for table in TABLES)


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0016")
    assert not set(TABLES) & _tables(db_url)
    command.upgrade(cfg, "0017")
    assert set(TABLES) <= _tables(db_url)
    assert {n for n in _names(db_url, "trigger") if _ours(n)} == {
        "trg_partner_entries_no_update",
        "trg_partner_entries_no_delete",
        "trg_partner_entry_decisions_no_update",
        "trg_partner_entry_decisions_no_delete",
        "trg_partner_payments_no_update",
        "trg_partner_payments_no_delete",
        "trg_partner_periods_born_open",
        "trg_partner_periods_only_the_close",
        "trg_partner_periods_no_delete",
        "trg_partner_entries_period_open",
        "trg_partner_payments_period_open",
        "trg_partner_entry_decisions_period_open",
    }
    command.downgrade(cfg, "0016")
    assert not set(TABLES) & _tables(db_url)
    assert not [n for n in _names(db_url, "trigger") if _ours(n)]
    assert not [n for n in _names(db_url, "index") if "partner_entries" in n]
    command.upgrade(cfg, "0017")
    assert set(TABLES) <= _tables(db_url)


def _shape(url: str, table: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {
            "columns": {
                c["name"]: (c["nullable"], str(c["type"]).upper(), str(c["default"]))
                for c in insp.get_columns(table)
            },
            "indexes": {
                i["name"]: (
                    i["column_names"],
                    i["unique"],
                    {k: str(v) for k, v in i.get("dialect_options", {}).items()},
                )
                for i in insp.get_indexes(table)
            },
            "unique": sorted(
                (u["name"], u["column_names"]) for u in insp.get_unique_constraints(table)
            ),
            "foreign_keys": sorted(
                (
                    fk["name"],
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                    fk["options"].get("ondelete"),
                )
                for fk in insp.get_foreign_keys(table)
            ),
            "checks": sorted(
                (c["name"], " ".join(str(c["sqltext"]).split()))
                for c in insp.get_check_constraints(table)
            ),
        }
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", TABLES)
def test_the_migrated_tables_match_the_models(db_url: str, tmp_path: Path, table: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url, revision="0017")
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url, table), _shape(model_url, table)
    assert migrated["checks"] and migrated["foreign_keys"]
    # The date floors came with 0019 (a trigger on SQLite): see tests/test_migration_0019.py.
    model["checks"] = [c for c in model["checks"] if not str(c[0]).endswith("_floor")]
    for part in model:
        assert migrated[part] == model[part], part


def test_the_hot_reads_have_their_indexes(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("read on SQLite")
    migrate(db_url, revision="0017")
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        entries = {i["name"]: i["column_names"] for i in insp.get_indexes("partner_entries")}
        payments = {i["name"]: i["column_names"] for i in insp.get_indexes("partner_payments")}
        periods = {u["name"]: u["column_names"] for u in insp.get_unique_constraints(TABLES[0])}
    finally:
        engine.dispose()
    assert entries["ix_partner_entries_agency_partner_date"] == [
        "agency_id",
        "partner_id",
        "entry_date",
    ]
    assert entries["ix_partner_entries_agency_sale"] == ["agency_id", "sale_id"]
    assert payments["ix_partner_payments_agency_partner_paid_on"] == [
        "agency_id",
        "partner_id",
        "paid_on",
    ]
    assert periods["uq_partner_periods_agency_partner_start"] == [
        "agency_id",
        "partner_id",
        "start_date",
    ]


# --- partner_periods ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 10, 1), date(2026, 10, 15)),
        (date(2026, 10, 16), date(2026, 10, 31)),
        (date(2026, 11, 16), date(2026, 11, 30)),
        (date(2026, 2, 16), date(2026, 2, 28)),
        (date(2028, 2, 16), date(2028, 2, 29)),
        (date(2026, 12, 16), date(2026, 12, 31)),
    ],
)
def test_the_valid_half_months_are_accepted(db_url: str, start: date, end: date) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(w, w.a, period(w, start_date=start, end_date=end))
    assert _count(w, w.a, PartnerPeriod) == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 10, 2), date(2026, 10, 15)),  # does not start on the 1st or the 16th
        (date(2026, 10, 1), date(2026, 10, 14)),
        (date(2026, 10, 1), date(2026, 10, 16)),
        (date(2026, 10, 1), date(2026, 10, 31)),  # a whole month
        (date(2026, 10, 16), date(2026, 10, 30)),  # October has 31 days
        (date(2026, 11, 16), date(2026, 11, 29)),
        (date(2028, 2, 16), date(2028, 2, 28)),  # 2028 is a leap year
        (date(2026, 10, 16), date(2026, 11, 15)),  # crosses into the next month
        (date(2026, 10, 1), date(2027, 10, 15)),  # same day and month, another year
        (date(2026, 10, 16), date(2026, 10, 15)),
    ],
)
def test_other_date_ranges_are_refused(db_url: str, start: date, end: date) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, period(w, start_date=start, end_date=end))


def test_one_period_per_partner_and_start_day(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(w, w.a, period(w))
    with pytest.raises(IntegrityError):
        _add(w, w.a, period(w))
    _add(w, w.a, period(w, start_date=date(2026, 10, 16), end_date=date(2026, 10, 31)))
    _add(w, w.b, period(w, agency_id=w.b, partner_id=w.partner_b))  # another agency's partner


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "draft"},
        {"status": "closed"},  # closed without its frozen columns
        {"closing_balance": Decimal("1.00")},  # open with a frozen column
        {"snapshot": {"sales": []}},
        {"closed_at": CLOSED_AT},
    ],
    ids=lambda o: ",".join(o),
)
def test_period_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    with pytest.raises((IntegrityError, ClosedPeriodError)):  # the ORM refuses a non-open birth
        _add(w, w.a, period(w, **overrides))


@pytest.mark.parametrize(
    "missing",
    [
        "opening_balance",
        "sales_total",
        "entries_total",
        "payments_total",
        "closing_balance",
        "closed_at",
        "closed_by_user_id",
        "snapshot",
    ],
)
def test_a_closed_period_needs_every_frozen_column(db_url: str, missing: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [period_id] = _add(w, w.a, period(w))
    with pytest.raises(IntegrityError):
        _close(w, period_id, **{missing: None})
    assert _status(w, period_id) == "open"


def _status(w: W, period_id: int) -> str | None:
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            return s.scalar(select(PartnerPeriod.status).where(PartnerPeriod.id == period_id))
    finally:
        engine.dispose()


def test_a_period_is_born_open(db_url: str) -> None:
    """Item 4: inserting a period that is already closed is refused, frozen columns or not
    (the SQLite trigger here; ``tenant_insert`` WITH CHECK on Postgres, tests/test_rls.py)."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    row = {
        "agency_id": w.a,
        "partner_id": w.partner_a,
        "start_date": date(2026, 10, 1),
        "end_date": date(2026, 10, 15),
        **frozen(w),
    }
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            with pytest.raises(DBAPIError, match="born open|row-level security"):
                s.execute(insert(PartnerPeriod).values(**row))  # Core: past the ORM guard
    finally:
        engine.dispose()
    assert _count(w, w.a, PartnerPeriod) == 0


def test_the_orm_refuses_to_insert_a_closed_period(session: Session) -> None:
    w = _orm_world(session)
    session.add(period(w, **frozen(w)))
    with pytest.raises(ClosedPeriodError):
        session.flush()
    session.rollback()


@pytest.mark.parametrize(
    "change",
    [
        {"start_date": date(2026, 10, 16), "end_date": date(2026, 10, 31)},
        {"created_at": CLOSED_AT},
        {"status": "open"},  # an update that does not close it
    ],
    ids=lambda c: ",".join(c),
)
def test_an_open_period_changes_only_by_its_close(db_url: str, change: dict[str, Any]) -> None:
    """Item 3 (SQLite trigger): the only UPDATE is the close; who, which partner and which
    half-month never change. Postgres: the trigger and ``tenant_update`` WITH CHECK
    (tests/test_rls.py)."""
    if _is_postgres(db_url):
        pytest.skip("SQLite trigger; Postgres in tests/test_rls.py")
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [period_id] = _add(w, w.a, period(w))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            statement = update(PartnerPeriod).where(PartnerPeriod.id == period_id)
            with pytest.raises(DBAPIError, match="changes only by its close"):
                s.execute(statement.values(**change))
            s.rollback()
            rebind_tenant(s, w.a)
            with pytest.raises(DBAPIError, match="changes only by its close"):  # even on the close
                s.execute(statement.values(**frozen(w), **{"partner_id": w.partner_a + 100}))
    finally:
        engine.dispose()
    assert _status(w, period_id) == "open"


def test_an_open_period_is_closed_by_one_update(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [period_id] = _add(w, w.a, period(w))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            row = s.get(PartnerPeriod, period_id)
            assert row is not None and row.snapshot is None
            for name, value in frozen(w).items():
                setattr(row, name, value)
            s.commit()
            s.expire_all()
            row = s.get(PartnerPeriod, period_id)
            assert row is not None
            assert (row.status, row.closing_balance) == ("closed", Decimal("963.00"))
            assert row.snapshot == {"sales": [], "entries": [], "payments": []}
    finally:
        engine.dispose()


def _closed_period(w: W) -> int:
    [period_id] = _add(w, w.a, period(w))
    _close(w, period_id)
    return period_id


def _orm_world(session: Session) -> W:
    """One agency on a ``create_all`` database (no trigger): the ORM guard alone."""
    agency = create_agency(session, "agence-o", "Agence O").id
    user = User(agency_id=agency, phone="+22212390109", role="manager")
    partner = Partner(agency_id=agency, name="Partenaire Test", name_key="partenaire test")
    session.add_all([user, partner])
    session.commit()
    return W("", agency, 0, user.id, 0, partner.id, 0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"closing_balance": Decimal("0.00")},
        {"status": "open"},
        {"snapshot": {"sales": ["edited"]}},
    ],
    ids=lambda c: ",".join(c),
)
def test_the_orm_refuses_to_change_a_closed_period(
    session: Session, change: dict[str, Any]
) -> None:
    """The ORM guard alone (``create_all``: no trigger on this database)."""
    w = _orm_world(session)
    row = period(w)
    session.add(row)
    session.commit()
    for name, value in frozen(w).items():
        setattr(row, name, value)
    session.commit()
    for name, value in change.items():
        setattr(row, name, value)
    with pytest.raises(ClosedPeriodError):
        session.flush()
    session.rollback()
    with pytest.raises(ClosedPeriodError):
        session.delete(row)
        session.flush()
    session.rollback()
    assert session.scalar(select(PartnerPeriod.closing_balance)) == Decimal("963.00")


def test_the_orm_lets_an_open_period_close_once(session: Session) -> None:
    w = _orm_world(session)
    row = period(w)
    session.add(row)
    session.commit()
    for name, value in frozen(w).items():
        setattr(row, name, value)
    session.commit()  # open -> closed is allowed
    row.closing_balance = Decimal("1.00")
    with pytest.raises(ClosedPeriodError):
        session.commit()
    session.rollback()


def test_the_database_refuses_to_change_or_delete_a_closed_period(db_url: str) -> None:
    """Core statements skip the ORM guard: the trigger (SQLite) or the policy (Postgres) holds."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    period_id = _closed_period(w)
    engine = make_engine(db_url)
    try:
        for statement in (
            update(PartnerPeriod).values(closing_balance=Decimal("0.00")),
            update(PartnerPeriod).values(status="open"),
            delete(PartnerPeriod),
        ):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, w.a)
                if _is_postgres(db_url):
                    s.execute(statement)  # no policy matches: nothing changes
                    s.commit()
                else:
                    with pytest.raises(DBAPIError, match="only by its close|never deleted"):
                        s.execute(statement)
                    s.rollback()
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            row = s.get(PartnerPeriod, period_id)
            assert row is not None
            assert (row.status, row.closing_balance) == ("closed", Decimal("963.00"))
    finally:
        engine.dispose()


def test_an_open_period_cannot_be_deleted_either(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite trigger; Postgres has no DELETE policy (tests/test_rls.py)")
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(w, w.a, period(w))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            with pytest.raises(DBAPIError, match="never deleted"):
                s.execute(delete(PartnerPeriod))
    finally:
        engine.dispose()
    assert _count(w, w.a, PartnerPeriod) == 1


# --- partner_entries ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        ("ADM", "11306.00"),
        ("CHANGEMENT", "1500.00"),
        ("RFND", "-12311.00"),
        ("AJUSTEMENT", "250.00"),
        ("AJUSTEMENT", "-250.00"),
        ("OPENING", "48000.00"),
        ("OPENING", "-2000.00"),
        ("OPENING", "0.00"),
    ],
)
def test_the_signs_that_fit_their_kind_are_accepted(db_url: str, kind: str, amount: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(w, w.a, entry(w, kind=kind, amount=Decimal(amount)))
    assert _count(w, w.a, PartnerEntry) == 1


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        ("ADM", "0.00"),
        ("ADM", "-1.00"),
        ("CHANGEMENT", "0.00"),
        ("CHANGEMENT", "-1.00"),
        ("RFND", "0.00"),
        ("RFND", "1.00"),
        ("AJUSTEMENT", "0.00"),
    ],
)
def test_a_sign_that_does_not_fit_its_kind_is_refused(db_url: str, kind: str, amount: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, entry(w, kind=kind, amount=Decimal(amount)))


def test_a_correction_is_a_non_zero_system_line_for_a_sale(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    reason = "Vente modifiée après clôture du 1-15 oct."
    correction = {"kind": "CORRECTION", "source": "system", "reason": reason}
    _add(w, w.a, entry(w, amount=Decimal("-300.00"), sale_id=w.sale_a, **correction))
    for bad in (
        {"amount": Decimal("0.00"), "sale_id": w.sale_a},
        {"amount": Decimal("300.00"), "sale_id": w.sale_a, "source": "manual"},
    ):
        with pytest.raises(IntegrityError):
            _add(w, w.a, entry(w, **{**correction, **bad}))
    with pytest.raises(IntegrityError):  # only the application writes system lines
        _add(w, w.a, entry(w, source="system"))
    with pytest.raises(IntegrityError):  # a sale belongs on a correction only
        _add(w, w.a, entry(w, sale_id=w.sale_a))


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "BONUS"},
        {"kind": "adm"},
        {"source": "excel"},
        {"reason": "abc"},
        {"reason": "  abcd  "},
        {"reason": ""},
        {"kind": "RFND", "amount": Decimal("-5.00"), "doc_number": "39400000"},  # 8 digits
        {"kind": "RFND", "amount": Decimal("-5.00"), "doc_number": "39400000AB"},
        {"doc_number": "3940000001"},  # a ticket number on an ADM
        {"kind": "RFND", "amount": Decimal("-5.00"), "source": "amadeus"},  # no ticket number
        {"source": "amadeus", "doc_number": "3940000001"},  # an ADM cannot come from Amadeus
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_entry_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, entry(w, **overrides))


def test_a_reason_is_required(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, entry(w, reason=None))


def test_a_refund_proposed_from_amadeus_carries_its_ticket(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(
        w,
        w.a,
        entry(
            w, kind="RFND", amount=Decimal("-2000.00"), source="amadeus", doc_number="3940000001"
        ),
        entry(w, kind="RFND", amount=Decimal("-500.00"), doc_number="3940000002"),  # manual
    )
    assert _count(w, w.a, PartnerEntry) == 2


def test_at_most_one_opening_balance_per_partner(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    opening = {"kind": "OPENING", "amount": Decimal("48000.00"), "reason": "solde du classeur"}
    _add(w, w.a, entry(w, **opening))
    with pytest.raises(IntegrityError):
        _add(w, w.a, entry(w, **{**opening, "amount": Decimal("1.00")}))
    # Other kinds are not limited, and the other agency has its own partner.
    _add(w, w.a, entry(w), entry(w))
    _add(
        w,
        w.b,
        entry(w, agency_id=w.b, partner_id=w.partner_b, created_by_user_id=w.user_b, **opening),
    )
    assert _count(w, w.a, PartnerEntry) == 3


# --- partner_entry_decisions -------------------------------------------------------------------


def test_one_decision_per_entry(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [adjustment] = _add(w, w.a, entry(w, kind="AJUSTEMENT", amount=Decimal("250.00")))
    _add(w, w.a, _decision(w, adjustment))
    with pytest.raises(IntegrityError):
        _add(w, w.a, _decision(w, adjustment, decision="reject"))
    assert _count(w, w.a, PartnerEntryDecision) == 1


# Every line a person or the application can write, as (kind, source, extra columns).
DECIDABLE_LINES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "AJUSTEMENT": ("AJUSTEMENT", "manual", {"amount": Decimal("250.00")}),
    "RFND-amadeus": (
        "RFND",
        "amadeus",
        {"amount": Decimal("-2000.00"), "doc_number": "3940000001"},
    ),
    "RFND-manual": ("RFND", "manual", {"amount": Decimal("-500.00")}),
    "ADM": ("ADM", "manual", {"amount": Decimal("100.00")}),
    "CHANGEMENT": ("CHANGEMENT", "manual", {"amount": Decimal("100.00")}),
    "OPENING": ("OPENING", "manual", {"amount": Decimal("100.00")}),
    "CORRECTION": ("CORRECTION", "system", {"amount": Decimal("100.00")}),
}
ALLOWED_DECISIONS = {
    ("AJUSTEMENT", "approve"),
    ("AJUSTEMENT", "reject"),
    ("RFND-amadeus", "confirm"),
    ("RFND-amadeus", "reject"),
}


@pytest.mark.parametrize(
    ("line", "decision"),
    [(line, decision) for line in DECIDABLE_LINES for decision in ("approve", "confirm", "reject")],
)
def test_a_decision_must_fit_its_entry(db_url: str, line: str, decision: str) -> None:
    """Item 2: an AJUSTEMENT is approved or rejected, an RFND proposed from Amadeus is confirmed
    or rejected; nothing else takes a decision (a CHECK on the kind and source the decision
    copies, held to the entry's own by the composite foreign key)."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    kind, source, extra = DECIDABLE_LINES[line]
    [entry_id] = _add(w, w.a, entry(w, kind=kind, source=source, **extra))
    row = _decision(w, entry_id, entry_kind=kind, entry_source=source, decision=decision)
    if (line, decision) in ALLOWED_DECISIONS:
        _add(w, w.a, row)
        assert _count(w, w.a, PartnerEntryDecision) == 1
    else:
        with pytest.raises(IntegrityError):
            _add(w, w.a, row)


@pytest.mark.parametrize(
    ("kind", "source"),
    [("RFND", "amadeus"), ("AJUSTEMENT", "amadeus"), ("RFND", "manual"), ("ADM", "manual")],
)
def test_a_decision_cannot_lie_about_its_entry(db_url: str, kind: str, source: str) -> None:
    """The kind and source a decision states must be its entry's (the composite foreign key):
    an AJUSTEMENT cannot be 'confirmed' by claiming to be an Amadeus refund."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [adjustment] = _add(w, w.a, entry(w, kind="AJUSTEMENT", amount=Decimal("250.00")))
    decision = "confirm" if source == "amadeus" else "approve"
    with pytest.raises(IntegrityError):
        _add(
            w,
            w.a,
            _decision(w, adjustment, entry_kind=kind, entry_source=source, decision=decision),
        )


@pytest.mark.parametrize("decision", ["accept", "APPROVE", ""])
def test_another_decision_is_refused(db_url: str, decision: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [adjustment] = _add(w, w.a, entry(w, kind="AJUSTEMENT", amount=Decimal("250.00")))
    with pytest.raises(IntegrityError):
        _add(w, w.a, _decision(w, adjustment, decision=decision))


# --- partner_payments --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["cash", "bankily", "masrvi", "sedad", "virement", "cheque"])
def test_every_payment_method_but_credit_is_accepted(db_url: str, method: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(w, w.a, payment(w, method=method, reference="CHQ 0012345"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "credit"},
        {"method": "carte"},
        {"amount": Decimal("0.00")},
        {"amount": Decimal("-20000.00")},
        {"reference": ""},
        {"reference": "   "},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_payment_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, payment(w, **overrides))


def test_lump_payments_are_kept_as_typed(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _add(w, w.a, payment(w), payment(w, amount=Decimal("60000.00"), method="virement"))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            amounts = s.scalars(select(PartnerPayment.amount).order_by(PartnerPayment.id)).all()
    finally:
        engine.dispose()
    assert amounts == [Decimal("20000.00"), Decimal("60000.00")]


# --- append-only -------------------------------------------------------------------------------


def test_lines_decisions_and_payments_are_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies and grants (tests/test_rls.py)")
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [adjustment] = _add(w, w.a, entry(w, kind="AJUSTEMENT", amount=Decimal("250.00")))
    _add(w, w.a, _decision(w, adjustment), payment(w))
    engine = make_engine(db_url)
    try:
        for model, change in (
            (PartnerEntry, {"amount": Decimal("1.00")}),
            (PartnerEntryDecision, {"decision": "reject"}),
            (PartnerPayment, {"amount": Decimal("1.00")}),
        ):
            for statement in (update(model).values(**change), delete(model)):
                with make_session_factory(engine)() as s:
                    rebind_tenant(s, w.a)
                    with pytest.raises(DBAPIError, match="append-only"):
                        s.execute(statement)
                    s.rollback()
    finally:
        engine.dispose()
    for model in (PartnerEntry, PartnerEntryDecision, PartnerPayment):
        assert _count(w, w.a, model) == 1


# --- item 1: nothing lands in a closed period --------------------------------------------------

OCT_5 = date(2026, 10, 5)  # inside the 1-15 Oct period of the world
OCT_20 = date(2026, 10, 20)


def test_a_line_or_payment_dated_in_a_closed_period_is_refused(db_url: str) -> None:
    """The BEFORE INSERT triggers (SQLite here; the plpgsql ones on Postgres run in CI)."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _closed_period(w)  # 1-15 Oct, closed
    for start, end in ((OCT_5, OCT_5), (date(2026, 10, 1), date(2026, 10, 15))):
        for row in (
            entry(w, entry_date=start),
            entry(w, entry_date=end, kind="AJUSTEMENT", amount=Decimal("1.00")),
            payment(w, paid_on=start),
            payment(w, paid_on=end),
        ):
            with pytest.raises(IntegrityError, match="closed"):
                _add(w, w.a, row)
    assert _count(w, w.a, PartnerEntry) == 0 and _count(w, w.a, PartnerPayment) == 0


def test_a_line_or_payment_in_an_open_period_or_without_one_is_accepted(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    _closed_period(w)  # 1-15 Oct, closed
    [_open] = _add(w, w.a, period(w, start_date=date(2026, 10, 16), end_date=date(2026, 10, 31)))
    _add(w, w.a, entry(w, entry_date=OCT_20), payment(w, paid_on=OCT_20))  # open period
    _add(w, w.a, entry(w, entry_date=date(2026, 11, 3)), payment(w, paid_on=date(2026, 11, 3)))
    # Another partner, and another agency, are not held by this partner's closed period.
    _add(
        w,
        w.b,
        entry(w, agency_id=w.b, partner_id=w.partner_b, created_by_user_id=w.user_b),
        payment(
            w,
            agency_id=w.b,
            partner_id=w.partner_b,
            received_by_user_id=w.user_b,
            recorded_by_user_id=w.user_b,
            paid_on=OCT_5,
        ),
    )
    assert _count(w, w.a, PartnerEntry) == 2 and _count(w, w.a, PartnerPayment) == 2


def test_nothing_is_backdated_before_the_end_of_the_last_closed_period(db_url: str) -> None:
    """N3: the guard is "a closed period of this partner ends on or after the date", so a date
    in a half-month that never got a row, before a closed one, is refused too; a late line goes
    into the open period as a correction instead."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [late_october] = _add(
        w, w.a, period(w, start_date=date(2026, 10, 16), end_date=date(2026, 10, 31))
    )
    _close(w, late_october)  # 16-31 Oct closed; 1-15 Oct never had a row
    for day in (OCT_5, date(2026, 9, 2), date(2026, 10, 31)):
        for row in (entry(w, entry_date=day), payment(w, paid_on=day)):
            with pytest.raises(IntegrityError, match="closed"):
                _add(w, w.a, row)
    november = date(2026, 11, 1)  # after the last closed period
    _add(w, w.a, entry(w, entry_date=november), payment(w, paid_on=november))
    assert _count(w, w.a, PartnerEntry) == 1 and _count(w, w.a, PartnerPayment) == 1


def _migration_module() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "migrations/versions/0017_partner_accounts.py"
    spec = importlib.util.spec_from_file_location("migration_0017_source", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_postgres_functions_are_hardened() -> None:
    """Read on any database (the functions themselves only run in the Postgres CI job): N2 every
    plpgsql function pins its search_path; N1 the guard takes the shared advisory lock of the
    partner first and has no FOR SHARE step left; N3 it compares with ``end_date``; and nothing
    in them would be mangled by text(): no bind-parameter colon, no percent sign."""
    functions: dict[str, str] = _migration_module().PG_FUNCTIONS
    assert len(functions) == 5
    for signature, body in functions.items():
        assert "LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$" in body, (
            signature
        )
        assert "SECURITY DEFINER" not in body, signature
        assert ":" not in body and "%" not in body, signature
    guard = functions["partner_period_assert_open(integer, bigint, date)"]
    lock = "pg_advisory_xact_lock_shared(170017, CAST(mod(p_partner, 2147483647) AS integer))"
    assert lock in guard
    assert guard.index(lock) < guard.index("IF EXISTS")
    assert "FOR SHARE" not in guard
    assert "p.end_date >= p_day" in guard and "BETWEEN" not in guard


def test_a_decision_on_a_line_dated_before_a_closed_period_is_refused(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [early] = _add(w, w.a, entry(w, entry_date=OCT_5, kind="AJUSTEMENT", amount=Decimal("1.00")))
    [late_october] = _add(
        w, w.a, period(w, start_date=date(2026, 10, 16), end_date=date(2026, 10, 31))
    )
    _close(w, late_october)  # the line's own half-month (1-15 Oct) has no row
    with pytest.raises(IntegrityError, match="closed"):
        _add(w, w.a, _decision(w, early))


def test_a_decision_on_a_line_of_a_closed_period_is_refused(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [period_id] = _add(w, w.a, period(w))
    [inside, later] = _add(
        w,
        w.a,
        entry(w, entry_date=OCT_5, kind="AJUSTEMENT", amount=Decimal("250.00")),
        entry(w, entry_date=OCT_20, kind="AJUSTEMENT", amount=Decimal("250.00")),
    )
    _close(w, period_id)
    with pytest.raises(IntegrityError, match="closed"):
        _add(w, w.a, _decision(w, inside))
    _add(w, w.a, _decision(w, later))  # its period has no row yet: accepted
    assert _count(w, w.a, PartnerEntryDecision) == 1


# --- item 8: request keys for F2's double clicks -----------------------------------------------


def test_a_request_key_is_used_once_per_agency(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    key = "0f" * 16
    _add(w, w.a, entry(w, request_key=key), payment(w, request_key=key))  # tables are separate
    with pytest.raises(IntegrityError):
        _add(w, w.a, entry(w, request_key=key))
    with pytest.raises(IntegrityError):
        _add(w, w.a, payment(w, request_key=key))
    # Without a key there is no limit, and another agency may use the same key.
    _add(w, w.a, entry(w), entry(w), payment(w), payment(w))
    other = {"agency_id": w.b, "partner_id": w.partner_b}
    _add(
        w,
        w.b,
        entry(w, created_by_user_id=w.user_b, request_key=key, **other),
        payment(
            w, received_by_user_id=w.user_b, recorded_by_user_id=w.user_b, request_key=key, **other
        ),
    )
    assert _count(w, w.a, PartnerEntry) == 3 and _count(w, w.a, PartnerPayment) == 3


@pytest.mark.parametrize("bad", ["", "x" * 65])
def test_a_request_key_is_not_empty_nor_too_long(db_url: str, bad: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    for row in (entry(w, request_key=bad), payment(w, request_key=bad)):
        with pytest.raises((IntegrityError, DBAPIError)):
            _add(w, w.a, row)


# --- tenancy: every reference stays inside its agency ------------------------------------------


def test_rows_cannot_point_at_another_agencys_partner_user_sale_or_entry(db_url: str) -> None:
    migrate(db_url, revision="0017")
    w = _world(db_url)
    [own_entry] = _add(w, w.a, entry(w, kind="AJUSTEMENT", amount=Decimal("250.00")))
    correction = {"kind": "CORRECTION", "source": "system", "amount": Decimal("5.00")}
    forged: list[tuple[int, Any]] = [
        (w.a, period(w, partner_id=w.partner_b)),
        (w.a, entry(w, partner_id=w.partner_b)),
        (w.a, entry(w, created_by_user_id=w.user_b)),
        (
            w.b,
            entry(
                w,
                agency_id=w.b,
                partner_id=w.partner_b,
                created_by_user_id=w.user_b,
                sale_id=w.sale_a,
                **correction,
            ),
        ),  # fmt: skip
        (w.b, _decision(w, own_entry, agency_id=w.b, decided_by_user_id=w.user_b)),
        (w.a, _decision(w, own_entry, decided_by_user_id=w.user_b)),
        (w.a, payment(w, partner_id=w.partner_b)),
        (w.a, payment(w, received_by_user_id=w.user_b)),
        (w.a, payment(w, recorded_by_user_id=w.user_b)),
    ]
    for agency, row in forged:
        with pytest.raises((IntegrityError, DBAPIError)):
            _add(w, agency, row)
    [own_period] = _add(w, w.a, period(w))
    with pytest.raises((IntegrityError, DBAPIError)):  # closed by a user of another agency
        _close(w, own_period, closed_by_user_id=w.user_b)


def test_each_agency_reads_only_its_own_rows(db_url: str) -> None:
    """On SQLite the filter on ``agency_id`` is the guard (tests/test_rls.py proves the
    row-level security on Postgres): each agency's rows carry its own id only."""
    migrate(db_url, revision="0017")
    w = _world(db_url)
    for agency, partner, user in ((w.a, w.partner_a, w.user_a), (w.b, w.partner_b, w.user_b)):
        own = {"agency_id": agency, "partner_id": partner}
        [line] = _add(
            w,
            agency,
            entry(w, kind="AJUSTEMENT", amount=Decimal("1.00"), created_by_user_id=user, **own),
        )
        _add(
            w,
            agency,
            period(w, **own),
            _decision(w, line, agency_id=agency, decided_by_user_id=user),
            payment(w, received_by_user_id=user, recorded_by_user_id=user, **own),
        )
    for model in (PartnerPeriod, PartnerEntry, PartnerEntryDecision, PartnerPayment):
        assert _count(w, w.a, model) == 1 and _count(w, w.b, model) == 1
