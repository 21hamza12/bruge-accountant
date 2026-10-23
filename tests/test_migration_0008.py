"""Migration 0008: report days and sales (SQLite always; Postgres when a server is given)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import Base, make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import (
    DayEvent,
    Partner,
    ReportDay,
    Sale,
    SaleTicket,
    SaleVersion,
    Upload,
    User,
)
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import TEST_APP_DATABASE_URL_ENV, alembic_config, app_role_env, migrate

TABLES = ("report_days", "day_events", "sales", "sale_tickets", "sale_versions")
APPEND_ONLY = ("day_events", "sale_versions")
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
DAY = date(2026, 10, 22)


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


# --- a small world written through the models -----------------------------------------------


@dataclass(frozen=True)
class W:
    url: str
    a: int  # agency a
    b: int  # agency b
    user_a: int
    user_b: int
    partner_a: int
    partner_b: int
    upload_b: int
    day_a: int
    sale_a: int


def _upload(agency_id: int) -> Upload:
    return Upload(
        agency_id=agency_id,
        filename="x.xlsx",
        sha256="a" * 64,
        report_range_raw="22OCT",
        report_date=DAY,
        generated_on=DAY,
        ticket_count=0,
        warnings=[],
    )


def version(w_agency: int, sale_id: int, day_id: int, user_id: int, **kw: Any) -> SaleVersion:
    fields: dict[str, Any] = {
        "agency_id": w_agency,
        "sale_id": sale_id,
        "version_no": 1,
        "report_day_id": day_id,
        "staff_sign": "0004SV",
        "ag_kind": "direct",
        "ag_label": "BRUGE",
        "pb": Decimal("100"),
        "cost_total": Decimal("90"),
        "clt": "Client",
        "payment_method": "cash",
        "tickets": [],
        "source": "form",
        "actor_user_id": user_id,
    }
    fields.update(kw)
    return SaleVersion(**fields)


def _world(url: str) -> W:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            a = create_agency(s, "agence-a", "Agence A").id
            b = create_agency(s, "agence-b", "Agence B").id
            ids: dict[str, int] = {}
            for key, agency, phone in (("a", a, "+22212360101"), ("b", b, "+22212360102")):
                rebind_tenant(s, agency)
                user = User(agency_id=agency, phone=phone, role="manager")
                partner = Partner(agency_id=agency, name="P", name_key="p")
                s.add_all([user, partner])
                s.commit()
                ids[f"user_{key}"], ids[f"partner_{key}"] = user.id, partner.id
            rebind_tenant(s, b)
            upload_b = _upload(b)
            s.add(upload_b)
            s.commit()
            rebind_tenant(s, a)
            day = ReportDay(agency_id=a, day=DAY, status="open")
            s.add(day)
            s.commit()
            sale = Sale(
                agency_id=a,
                report_day_id=day.id,
                staff_sign="0004SV",
                created_by_user_id=ids["user_a"],
                current_version=1,
                status="active",
            )
            s.add(sale)
            s.commit()
            s.add(version(a, sale.id, day.id, ids["user_a"]))
            s.add(SaleTicket(agency_id=a, sale_id=sale.id, doc_number="3940000001", position=1))
            s.commit()
            return W(
                url, a, b, ids["user_a"], ids["user_b"], ids["partner_a"], ids["partner_b"],
                upload_b.id, day.id, sale.id,
            )  # fmt: skip
    finally:
        engine.dispose()


def _add(w: W, agency: int, *rows: Any) -> None:
    """Add rows as ``agency``'s tenant and commit; the database's refusal surfaces here."""
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.add_all(rows)
            s.commit()
    finally:
        engine.dispose()


def _scalar(url: str, sql: str) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# --- chain and shape ------------------------------------------------------------------------


def test_0008_follows_0007_and_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0008")
    assert revision is not None and revision.down_revision == "0007"
    assert len(script.get_heads()) == 1
    later = script.get_revision("0009")
    assert later is not None and later.down_revision == "0008"


def test_grants_follow_the_design() -> None:
    assert GRANTS["report_days"] == "SELECT, INSERT, UPDATE"
    assert GRANTS["sales"] == "SELECT, INSERT, UPDATE"
    assert GRANTS["day_events"] == "SELECT, INSERT"
    assert GRANTS["sale_versions"] == "SELECT, INSERT"
    # The one DELETE of the phase: current membership, history lives in sale_versions.
    assert GRANTS["sale_tickets"] == "SELECT, INSERT, DELETE"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects; the Postgres tests are below")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0007")
    assert not set(TABLES) & _tables(db_url)
    command.upgrade(cfg, "0008")
    assert set(TABLES) <= _tables(db_url)
    triggers = {
        n
        for n in _names(db_url, "trigger")
        if n.startswith(("trg_day_events", "trg_sale_versions"))
    }
    assert triggers == {
        "trg_day_events_no_update",
        "trg_day_events_no_delete",
        "trg_sale_versions_no_update",
        "trg_sale_versions_no_delete",
    }
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        sales_indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("sales")}
        assert sales_indexes == {
            "ix_sales_agency_day_sign": ["agency_id", "report_day_id", "staff_sign"]
        }
        for table, name, columns in (
            ("sale_versions", "ix_sale_versions_agency_partner", ["agency_id", "partner_id"]),
            (
                "report_days",
                "ix_report_days_agency_compared_upload",
                ["agency_id", "compared_upload_id"],
            ),
        ):
            found = {i["name"]: i["column_names"] for i in insp.get_indexes(table)}
            assert found[name] == columns, (table, name)
        uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("sale_tickets")}
        assert uniques == {("agency_id", "doc_number"), ("agency_id", "sale_id", "position")}
    finally:
        engine.dispose()
    command.downgrade(cfg, "0007")
    assert not set(TABLES) & _tables(db_url)
    assert not [n for n in _names(db_url, "trigger") if "sale_versions" in n or "day_events" in n]
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0007"
    command.upgrade(cfg, "head")
    assert set(TABLES) <= _tables(db_url)


def _names(url: str, kind: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = :t"), {"t": kind})
            return [str(n) for n in rows.scalars()]
    finally:
        engine.dispose()


def _shape(url: str) -> dict[str, dict[str, Any]]:
    """What the five tables look like to the inspector, comparable between two databases."""
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        out: dict[str, dict[str, Any]] = {}
        for name in TABLES:
            out[name] = {
                "columns": {
                    c["name"]: (c["nullable"], str(c["type"]).upper(), str(c["default"]))
                    for c in insp.get_columns(name)
                },
                "indexes": {
                    i["name"]: (i["column_names"], i["unique"]) for i in insp.get_indexes(name)
                },
                "uniques": sorted(
                    (u["name"], u["column_names"]) for u in insp.get_unique_constraints(name)
                ),
                "foreign_keys": sorted(
                    (
                        fk["name"],
                        fk["constrained_columns"],
                        fk["referred_table"],
                        fk["referred_columns"],
                        fk["options"].get("ondelete"),
                    )
                    for fk in insp.get_foreign_keys(name)
                ),
                "checks": sorted(
                    (c["name"], " ".join(str(c["sqltext"]).split()))
                    for c in insp.get_check_constraints(name)
                ),
            }
        return out
    finally:
        engine.dispose()


def test_the_migrated_tables_match_the_models(db_url: str, tmp_path: Path) -> None:
    """Columns, types, defaults, indexes, uniques, foreign keys and CHECK texts of the migrated
    tables equal the models' (the migration is written by hand: this keeps the two in step)."""
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url)
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["sale_versions"]["checks"]) == 15, "the comparison must see every CHECK"
    assert len(migrated["sale_versions"]["foreign_keys"]) == 5
    for table in TABLES:
        for part in model[table]:
            assert migrated[table][part] == model[table][part], (table, part)


# --- constraints ----------------------------------------------------------------------------


def test_the_ticket_number_guard_is_per_agency(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises(IntegrityError):  # same agency, same number
        _add(
            w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="3940000001", position=2)
        )
    with pytest.raises(IntegrityError):  # same position in the same sale
        _add(
            w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="3940000002", position=1)
        )
    _add(w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="3940000002", position=2))


@pytest.mark.parametrize(
    "doc",
    [
        "1234567",
        "123456789",
        "12345678901",
        "4953940281465",  # prefixed: the application cuts the prefix off before storing
        "1234567890123456",
        "3940000AB1",
        "",
        "394 000001",
    ],
)
def test_a_ticket_number_must_be_exactly_10_digits(db_url: str, doc: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises((IntegrityError, DBAPIError)):  # too long: a DataError on Postgres
        _add(w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number=doc, position=2))


def test_a_ticket_position_starts_at_one(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(
            w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="3940000009", position=0)
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"pb": Decimal("-1")},
        {"cost_total": Decimal("-1")},
        {"clt": None},  # a direct sale has a client
        {"clt": "   "},
        {"payment_method": "paypal"},
        {"source": "telepathy"},
        {"ag_kind": "other"},
        {"ag_kind": "partner", "partner_id": None, "fee_applied": Decimal("1")},
        {"partner_id": "partner_a"},  # a direct sale has no partner
        {"ap": "12345678"},  # not E.164
        {"ap": "+22212345678", "client_refused_phone": True},
        {"reason": "abc"},
        {"fee_override_reason": "abc", "fee_applied": Decimal("1")},
        {"fee_applied": Decimal("-1")},
        {"version_no": 0},
        {"staff_sign": "A"},
        {"version_no": 1},  # a second row with version 1 for the same sale
    ],
    ids=lambda o: ",".join(o),
)
def test_sale_version_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url)
    w = _world(db_url)
    values = dict(overrides)
    if values.get("partner_id") == "partner_a":
        values["partner_id"] = w.partner_a
    row = version(w.a, w.sale_a, w.day_a, w.user_a, **values)
    with pytest.raises(IntegrityError):
        _add(w, w.a, row)


def test_a_partner_sale_version_needs_a_partner_and_a_fee(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    partner = dict(ag_kind="partner", ag_label="P", clt=None, version_no=2)
    with pytest.raises(IntegrityError):
        _add(w, w.a, version(w.a, w.sale_a, w.day_a, w.user_a, **partner, partner_id=w.partner_a))
    ok = version(
        w.a,
        w.sale_a,
        w.day_a,
        w.user_a,
        **partner,
        partner_id=w.partner_a,
        fee_applied=Decimal("0"),
    )
    _add(w, w.a, ok)


def test_a_reopen_event_needs_a_reason_and_a_closed_day_a_stamp(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)

    def event(**kw: Any) -> DayEvent:
        fields: dict[str, Any] = {
            "agency_id": w.a,
            "report_day_id": w.day_a,
            "from_status": "closed",
            "to_status": "open",
            "kind": "reopen",
            "actor_user_id": w.user_a,
        }
        fields.update(kw)
        return DayEvent(**fields)

    for bad in (
        {"reason": None},
        {"reason": "   "},
        {"reason": "abcd"},
        {"kind": "nope", "reason": "abcde"},
        {"to_status": "weird", "reason": "abcde"},
        {"from_status": "weird", "reason": "abcde"},
    ):
        with pytest.raises(IntegrityError):
            _add(w, w.a, event(**bad))
    _add(w, w.a, event(reason="abcde"))
    _add(w, w.a, event(kind="open", from_status=None, to_status="open"))
    with pytest.raises(IntegrityError):
        _add(w, w.a, ReportDay(agency_id=w.a, day=date(2026, 10, 20), status="closed"))
    with pytest.raises(IntegrityError):
        _add(w, w.a, ReportDay(agency_id=w.a, day=date(2026, 10, 20), status="weird"))
    with pytest.raises(IntegrityError):  # one day per agency and date
        _add(w, w.a, ReportDay(agency_id=w.a, day=DAY, status="open"))
    _add(w, w.b, ReportDay(agency_id=w.b, day=DAY, status="open"))  # another agency may
    _add(
        w,
        w.a,
        ReportDay(
            agency_id=w.a,
            day=date(2026, 10, 20),
            status="closed",
            closed_at=NOW,
            closed_by_user_id=w.user_a,
        ),
    )


def test_a_sale_cannot_point_into_another_agency(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    other = {"agency_id": w.a}
    with pytest.raises(IntegrityError):  # a partner of agency b
        _add(
            w,
            w.a,
            version(
                w.a, w.sale_a, w.day_a, w.user_a,
                ag_kind="partner", ag_label="P", clt=None, version_no=2,
                partner_id=w.partner_b, fee_applied=Decimal("1"),
            ),
        )  # fmt: skip
    with pytest.raises(IntegrityError):  # a user of agency b
        _add(w, w.a, version(w.a, w.sale_a, w.day_a, w.user_b, version_no=2))
    with pytest.raises(IntegrityError):  # an upload of agency b compared on a day of agency a
        _add(
            w,
            w.a,
            ReportDay(
                **other, day=date(2026, 10, 19), status="compared", compared_upload_id=w.upload_b
            ),
        )
    with pytest.raises(IntegrityError):  # a sale on a day of agency b
        _add(
            w,
            w.b,
            Sale(
                agency_id=w.b,
                report_day_id=w.day_a,
                staff_sign="0004SV",
                created_by_user_id=w.user_b,
                current_version=1,
                status="active",
            ),
        )
    with pytest.raises(IntegrityError):  # a ticket in a sale of agency b
        _add(
            w, w.b, SaleTicket(agency_id=w.b, sale_id=w.sale_a, doc_number="3940000007", position=1)
        )
    with pytest.raises(IntegrityError):  # a day event of agency a on a day of agency b
        _add(
            w,
            w.b,
            DayEvent(
                agency_id=w.b,
                report_day_id=w.day_a,
                to_status="open",
                kind="open",
                actor_user_id=w.user_b,
            ),
        )


def test_sale_head_checks(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    for bad in ({"current_version": 0}, {"status": "weird"}, {"staff_sign": "A"}):
        fields: dict[str, Any] = {
            "agency_id": w.a, "report_day_id": w.day_a, "staff_sign": "0004SV",
            "created_by_user_id": w.user_a, "current_version": 1, "status": "active",
        }  # fmt: skip
        fields.update(bad)
        with pytest.raises(IntegrityError):
            _add(w, w.a, Sale(**fields))


def test_rows_with_dependents_cannot_be_deleted(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    engine = make_engine(db_url)
    try:
        for statement in (
            delete(Sale).where(Sale.id == w.sale_a),
            delete(ReportDay).where(ReportDay.id == w.day_a),
        ):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, w.a)
                with pytest.raises((IntegrityError, DBAPIError)):
                    s.execute(statement)
                    s.commit()
    finally:
        engine.dispose()


def test_the_tickets_of_a_sale_can_be_deleted_and_the_number_reused(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            s.execute(delete(SaleTicket).where(SaleTicket.sale_id == w.sale_a))
            s.commit()
    finally:
        engine.dispose()
    _add(w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="3940000001", position=1))


# --- append-only (SQLite triggers) ----------------------------------------------------------


def test_versions_and_day_events_are_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies (tests/test_rls.py)")
    migrate(db_url)
    w = _world(db_url)
    _add(
        w,
        w.a,
        DayEvent(
            agency_id=w.a,
            report_day_id=w.day_a,
            to_status="open",
            kind="open",
            actor_user_id=w.user_a,
        ),
    )
    engine = make_engine(db_url)
    try:
        for model, column, value in (
            (SaleVersion, SaleVersion.pb, Decimal("1")),
            (DayEvent, DayEvent.reason, "changed"),
        ):
            with make_session_factory(engine)() as s:
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(update(model).values({column.key: value}))
                s.rollback()
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(delete(model))
                s.rollback()
        # ...while the mutable tables stay editable.
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            s.execute(
                update(Sale).where(Sale.id == w.sale_a).values(status="voided", current_version=2)
            )
            s.execute(update(ReportDay).where(ReportDay.id == w.day_a).values(status="compared"))
            s.commit()
    finally:
        engine.dispose()
    assert _scalar(db_url, "SELECT count(*) FROM sale_versions") == 1
    assert _scalar(db_url, "SELECT pb FROM sale_versions") == 100
    assert _scalar(db_url, "SELECT count(*) FROM day_events") == 1


# --- Postgres only --------------------------------------------------------------------------


@pytest.fixture
def pg_url(db_url: str) -> str:
    if not _is_postgres(db_url):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    return db_url


def _privileges(url: str, role: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                p
                for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": table, "p": p}
                ).scalar()
            }
    finally:
        engine.dispose()


def _app_role() -> str:
    url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    if not url:
        pytest.skip("needs TEST_APP_DATABASE_URL")
    return make_url(url).username or ""


def test_postgres_forces_rls_with_the_expected_policies(pg_url: str) -> None:
    migrate(pg_url)
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            flags = {
                n: (rls, forced)
                for n, rls, forced in conn.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = ANY(:t)"
                    ),
                    {"t": list(TABLES)},
                )
            }
            policies = {
                (t, n, cmd)
                for t, n, cmd in conn.execute(
                    text(
                        "SELECT tablename, policyname, cmd FROM pg_policies "
                        "WHERE tablename = ANY(:t)"
                    ),
                    {"t": list(TABLES)},
                )
            }
    finally:
        engine.dispose()
    assert flags == {t: (True, True) for t in TABLES}
    assert policies == {
        ("report_days", "tenant_isolation", "ALL"),
        ("sales", "tenant_isolation", "ALL"),
        ("sale_tickets", "tenant_isolation", "ALL"),
        ("day_events", "tenant_read", "SELECT"),
        ("day_events", "tenant_insert", "INSERT"),
        ("sale_versions", "tenant_read", "SELECT"),
        ("sale_versions", "tenant_insert", "INSERT"),
    }


def test_postgres_grants_follow_the_map_and_a_downgrade_revokes_only_these_tables(
    pg_url: str,
) -> None:
    role = _app_role()
    migrate(pg_url, app_role=role)
    for table in TABLES:
        assert _privileges(pg_url, role, table) == set(GRANTS[table].replace(" ", "").split(",")), (
            table
        )
    assert _privileges(pg_url, role, "sale_tickets") == {"SELECT", "INSERT", "DELETE"}
    for table in APPEND_ONLY:
        assert not {"UPDATE", "DELETE", "TRUNCATE"} & _privileges(pg_url, role, table)
    with app_role_env(role):
        command.downgrade(alembic_config(pg_url), "0007")
    assert _privileges(pg_url, role, "partners") == {"SELECT", "INSERT", "UPDATE"}
    assert _privileges(pg_url, role, "users") == {"SELECT", "INSERT", "UPDATE"}  # untouched
    assert not set(TABLES) & _tables(pg_url)
    assert (
        _scalar(pg_url, "SELECT count(*) FROM pg_policies WHERE tablename = 'sale_versions'") == 0
    )
    migrate(pg_url, app_role=role)  # and up again
    assert set(TABLES) <= _tables(pg_url)


def test_postgres_single_role_mode_grants_nothing_and_does_not_fail(pg_url: str) -> None:
    role = _app_role()
    migrate(pg_url, app_role=None)
    for table in TABLES:
        assert _privileges(pg_url, role, table) == set(), table


def test_postgres_constraints_hold_too(pg_url: str) -> None:
    migrate(pg_url)
    w = _world(pg_url)
    with pytest.raises(IntegrityError):
        _add(
            w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="3940000001", position=2)
        )
    with pytest.raises(IntegrityError):
        _add(w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="12AB5678", position=2))
    with pytest.raises(IntegrityError):
        _add(w, w.a, version(w.a, w.sale_a, w.day_a, w.user_a, version_no=2, pb=Decimal("-1")))
    with pytest.raises(IntegrityError):
        _add(
            w,
            w.a,
            ReportDay(
                agency_id=w.a,
                day=date(2026, 10, 19),
                status="compared",
                compared_upload_id=w.upload_b,
            ),
        )


def test_0009_downgrade_widens_the_rule_and_upgrade_tightens_it_again(db_url: str) -> None:
    from alembic import command

    migrate(db_url)
    cfg = alembic_config(db_url)
    command.downgrade(cfg, "0008")
    # the models know the column 0013 added (it is gone after the downgrade): put it back so that
    # the ORM can write the world; the upgrade below stops at 0009, before 0013 would add it again
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE sale_versions ADD COLUMN override_digest VARCHAR(16)"))
    finally:
        engine.dispose()
    w = _world(db_url)
    # 8 to 15 digits are valid again under 0008
    _add(
        w, w.a, SaleTicket(agency_id=w.a, sale_id=w.sale_a, doc_number="4953940281465", position=2)
    )
    with app_role_env(None), pytest.raises(Exception, match="doc_number|CHECK|check"):
        command.upgrade(cfg, "head")  # a 13-digit row stops the tightening: loud, not silent
