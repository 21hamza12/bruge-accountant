"""Migration 0013: flag decisions and day acknowledgements (SQLite; the Postgres policies are
checked only when a server is given, which this suite never needs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
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
    ComparisonFlag,
    ComparisonRun,
    DayAcknowledgement,
    FlagDecision,
    ReportDay,
    Sale,
    Upload,
    User,
)
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate

TABLES = ("flag_decisions", "day_acknowledgements")
DAY = date(2026, 10, 20)


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


@dataclass(frozen=True)
class W:
    url: str
    a: int
    b: int
    user_a: int
    user_b: int
    day_a: int
    day_b: int
    sale_a: int
    sale_b: int
    run_a: int
    run_b: int
    flag_a: int
    flag_b: int


def _world(url: str) -> W:
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
                upload = Upload(
                    agency_id=agency,
                    filename="x.xlsx",
                    sha256="a" * 64,
                    report_range_raw="20OCT",
                    report_date=DAY,
                    generated_on=DAY,
                    ticket_count=0,
                    warnings=[],
                )
                day = ReportDay(agency_id=agency, day=DAY, status="open")
                s.add_all([user, upload, day])
                s.commit()
                sale = Sale(
                    agency_id=agency,
                    report_day_id=day.id,
                    staff_sign="0004SV",
                    created_by_user_id=user.id,
                    current_version=1,
                    status="active",
                )
                run = ComparisonRun(
                    agency_id=agency,
                    report_day_id=day.id,
                    upload_id=upload.id,
                    started_by_user_id=user.id,
                    matched_count=0,
                    flag_count=1,
                    blocking_count=1,
                    cancelled_count=0,
                    move_count=0,
                    partner_missing_count=0,
                    created_at=datetime(2026, 10, 22, tzinfo=UTC),
                )
                s.add_all([sale, run])
                s.commit()
                flag = ComparisonFlag(
                    agency_id=agency,
                    report_day_id=day.id,
                    run_id=run.id,
                    doc_number="3940000001",
                    kind="different",
                    field="total_doc",
                    app_value="100",
                    amadeus_value="90",
                    fingerprint="f" * 16,
                    sale_id=sale.id,
                    responsible_sign="0004SV",
                    status="open",
                )
                s.add(flag)
                s.commit()
                ids.update(
                    {
                        key: agency,
                        f"user_{key}": user.id,
                        f"day_{key}": day.id,
                        f"sale_{key}": sale.id,
                        f"run_{key}": run.id,
                        f"flag_{key}": flag.id,
                    }
                )
            return W(
                url, ids["a"], ids["b"], ids["user_a"], ids["user_b"], ids["day_a"], ids["day_b"],
                ids["sale_a"], ids["sale_b"], ids["run_a"], ids["run_b"], ids["flag_a"],
                ids["flag_b"],
            )  # fmt: skip
    finally:
        engine.dispose()


def decision(w: W, **kw: Any) -> FlagDecision:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "flag_id": w.flag_a,
        "stage": "agent",
        "choice": "mine_right",
        "reason": "Tarif négocié",
        "old_value": "100",
        "new_value": "90",
        "actor_user_id": w.user_a,
    }
    fields.update(kw)
    return FlagDecision(**fields)


def ack(w: W, **kw: Any) -> DayAcknowledgement:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "report_day_id": w.day_a,
        "kind": "partner_list",
        "run_id": w.run_a,
        "digest": "d" * 16,
        "actor_user_id": w.user_a,
    }
    fields.update(kw)
    return DayAcknowledgement(**fields)


def _add(w: W, agency: int, *rows: Any) -> None:
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


def _names(url: str, kind: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = :t"), {"t": kind})
            return [str(n) for n in rows.scalars()]
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


# --- chain and shape ---------------------------------------------------------------------------


def test_0013_follows_0012_and_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0013")
    assert revision is not None and revision.down_revision == "0012"
    assert script.get_heads() == ["0013"]


def test_grants_follow_the_design() -> None:
    assert GRANTS["flag_decisions"] == "SELECT, INSERT"
    assert GRANTS["day_acknowledgements"] == "SELECT, INSERT"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0012")
    assert not set(TABLES) & _tables(db_url)
    command.upgrade(cfg, "0013")
    assert set(TABLES) <= _tables(db_url)
    assert {
        n for n in _names(db_url, "trigger") if n.startswith("trg_flag_") or "_acknow" in n
    } == {
        "trg_flag_decisions_no_update",
        "trg_flag_decisions_no_delete",
        "trg_day_acknowledgements_no_update",
        "trg_day_acknowledgements_no_delete",
    }
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("flag_decisions")}
        assert indexes["ix_flag_decisions_agency_flag"] == ["agency_id", "flag_id", "id"]
    finally:
        engine.dispose()
    command.downgrade(cfg, "0012")
    assert not set(TABLES) & _tables(db_url)
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0012"
    command.upgrade(cfg, "0013")
    assert set(TABLES) <= _tables(db_url)


def _shape(url: str) -> dict[str, dict[str, Any]]:
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
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url, revision="0013")
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["flag_decisions"]["checks"]) == 4, "the comparison must see every CHECK"
    assert len(migrated["day_acknowledgements"]["checks"]) == 4
    assert len(migrated["flag_decisions"]["foreign_keys"]) == 3
    assert len(migrated["day_acknowledgements"]["foreign_keys"]) == 5
    for table in TABLES:
        for part in model[table]:
            assert migrated[table][part] == model[table][part], (table, part)


# --- constraints -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"stage": "weird"},
        {"choice": "weird"},
        {"stage": "validator"},  # mine_right is an agent choice
        {"stage": "agent", "choice": "validated", "reason": None},
        {"choice": "mine_right", "reason": None},
        {"choice": "mine_right", "reason": "ok"},
        {"choice": "mine_right", "reason": "     "},
        {"stage": "validator", "choice": "rejected", "reason": None},
        {"choice": "void_entry", "reason": ""},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_decision_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0013")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, decision(w, **overrides))


def test_the_valid_decisions_are_accepted(db_url: str) -> None:
    migrate(db_url, revision="0013")
    w = _world(db_url)
    _add(
        w,
        w.a,
        decision(w),
        decision(w, choice="amadeus_right", reason=None),
        decision(w, choice="acknowledged", reason=None),
        decision(w, choice="void_entry", reason="Saisie par erreur"),
        decision(w, stage="validator", choice="validated", reason=None),
        decision(w, stage="validator", choice="rejected", reason="Justificatif manquant"),
    )
    assert _scalar(db_url, "SELECT count(*) FROM flag_decisions") == 6


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "weird"},
        {"run_id": None},  # a partner list names its run
        {"kind": "override", "run_id": None},  # an override names its sale
        {"kind": "override", "run_id": None, "sale_id": 1, "version_no": None},
        {"version_no": 1},  # a version without a sale
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_acknowledgement_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0013")
    w = _world(db_url)
    if overrides.get("sale_id") == 1:
        overrides = {**overrides, "sale_id": w.sale_a}
    with pytest.raises(IntegrityError):
        _add(w, w.a, ack(w, **overrides))


def test_the_valid_acknowledgements_are_accepted(db_url: str) -> None:
    migrate(db_url, revision="0013")
    w = _world(db_url)
    _add(
        w,
        w.a,
        ack(w),
        ack(w, kind="override", run_id=None, sale_id=w.sale_a, version_no=1),
    )
    assert _scalar(db_url, "SELECT count(*) FROM day_acknowledgements") == 2


def test_nothing_can_point_into_another_agency(db_url: str) -> None:
    migrate(db_url, revision="0013")
    w = _world(db_url)
    with pytest.raises(IntegrityError):  # a flag of agency b
        _add(w, w.a, decision(w, flag_id=w.flag_b))
    with pytest.raises(IntegrityError):  # a user of agency b
        _add(w, w.a, decision(w, actor_user_id=w.user_b))
    with pytest.raises(IntegrityError):  # a day of agency b
        _add(w, w.a, ack(w, report_day_id=w.day_b))
    with pytest.raises(IntegrityError):  # a run of agency b
        _add(w, w.a, ack(w, run_id=w.run_b))
    with pytest.raises(IntegrityError):  # a sale of agency b
        _add(w, w.a, ack(w, kind="override", run_id=None, sale_id=w.sale_b, version_no=1))
    with pytest.raises(IntegrityError):  # a user of agency b
        _add(w, w.a, ack(w, actor_user_id=w.user_b))


def test_both_tables_are_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies and grants")
    migrate(db_url, revision="0013")
    w = _world(db_url)
    _add(w, w.a, decision(w), ack(w))
    engine = make_engine(db_url)
    try:
        for statement in (
            update(FlagDecision).values(reason="changed afterwards"),
            delete(FlagDecision),
            update(DayAcknowledgement).values(digest="e" * 16),
            delete(DayAcknowledgement),
        ):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, w.a)
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(statement)
                s.rollback()
    finally:
        engine.dispose()
    assert _scalar(db_url, "SELECT reason FROM flag_decisions") == "Tarif négocié"


def test_the_flag_a_decision_names_cannot_be_deleted(db_url: str) -> None:
    migrate(db_url, revision="0013")
    w = _world(db_url)
    _add(w, w.a, decision(w))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            with pytest.raises((IntegrityError, DBAPIError)):
                s.execute(delete(ComparisonFlag))
                s.commit()
    finally:
        engine.dispose()
