"""Migration 0011: comparison runs and flags (SQLite always; Postgres when a server is given)."""

from __future__ import annotations

import os
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
    ReportDay,
    Sale,
    Upload,
    User,
)
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import TEST_APP_DATABASE_URL_ENV, alembic_config, app_role_env, migrate

TABLES = ("comparison_runs", "comparison_flags")
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
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
    upload_a: int
    upload_b: int
    day_a: int
    day_b: int
    sale_a: int
    sale_b: int
    run_a: int


def _upload(agency_id: int) -> Upload:
    return Upload(
        agency_id=agency_id,
        filename="x.xlsx",
        sha256="a" * 64,
        report_range_raw="20OCT",
        report_date=DAY,
        generated_on=DAY,
        ticket_count=0,
        warnings=[],
    )


def _run(w_agency: int, day: int, upload: int, user: int | None, **kw: Any) -> ComparisonRun:
    fields: dict[str, Any] = {
        "agency_id": w_agency,
        "report_day_id": day,
        "upload_id": upload,
        "started_by_user_id": user,
        "matched_count": 3,
        "flag_count": 2,
        "blocking_count": 1,
        "cancelled_count": 0,
        "move_count": 0,
        "partner_missing_count": 0,
    }
    fields.update(kw)
    return ComparisonRun(**fields)


def flag(w: W, **kw: Any) -> ComparisonFlag:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "report_day_id": w.day_a,
        "run_id": w.run_a,
        "doc_number": "3940000001",
        "kind": "different",
        "field": "total_doc",
        "app_value": "100",
        "amadeus_value": "90",
        "fingerprint": "f" * 16,
        "sale_id": w.sale_a,
        "amadeus_seq": 1,
        "responsible_sign": "0004SV",
        "needs_validation": True,
        "blocking": True,
        "status": "open",
    }
    fields.update(kw)
    return ComparisonFlag(**fields)


def _world(url: str) -> W:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            ids: dict[str, int] = {}
            for key, slug, phone in (
                ("a", "agence-a", "+22212380101"),
                ("b", "agence-b", "+22212380102"),
            ):
                agency = create_agency(s, slug, slug.upper()).id
                rebind_tenant(s, agency)
                user = User(agency_id=agency, phone=phone, role="manager")
                upload = _upload(agency)
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
                s.add(sale)
                s.commit()
                ids.update(
                    {
                        key: agency,
                        f"user_{key}": user.id,
                        f"upload_{key}": upload.id,
                        f"day_{key}": day.id,
                        f"sale_{key}": sale.id,
                    }
                )
            rebind_tenant(s, ids["a"])
            run = _run(ids["a"], ids["day_a"], ids["upload_a"], ids["user_a"])
            s.add(run)
            s.commit()
            return W(
                url, ids["a"], ids["b"], ids["user_a"], ids["user_b"], ids["upload_a"],
                ids["upload_b"], ids["day_a"], ids["day_b"], ids["sale_a"], ids["sale_b"], run.id,
            )  # fmt: skip
    finally:
        engine.dispose()


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


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
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


# --- chain and shape ---------------------------------------------------------------------------


def test_0011_follows_0009_for_now_and_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0011")
    # TODO rebase onto 0010 (B6 wiring partner_aliases) before merge: then this reads "0010".
    assert revision is not None and revision.down_revision == "0009"
    assert script.get_heads() == ["0011"]


def test_grants_follow_the_design() -> None:
    assert GRANTS["comparison_runs"] == "SELECT, INSERT"
    assert GRANTS["comparison_flags"] == "SELECT, INSERT, UPDATE"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects; the Postgres tests are below")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0009")
    assert not set(TABLES) & _tables(db_url)
    command.upgrade(cfg, "0011")
    assert set(TABLES) <= _tables(db_url)
    assert {n for n in _names(db_url, "trigger") if "comparison" in n} == {
        "trg_comparison_runs_no_update",
        "trg_comparison_runs_no_delete",
    }
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        flag_indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("comparison_flags")}
        assert flag_indexes["ix_comparison_flags_agency_day_status"] == [
            "agency_id",
            "report_day_id",
            "status",
        ]
        assert flag_indexes["ix_comparison_flags_agency_sign_status"] == [
            "agency_id",
            "responsible_sign",
            "status",
        ]
        run_indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("comparison_runs")}
        assert run_indexes["ix_comparison_runs_agency_day"] == ["agency_id", "report_day_id", "id"]
    finally:
        engine.dispose()
    command.downgrade(cfg, "0009")
    assert not set(TABLES) & _tables(db_url)
    assert not [n for n in _names(db_url, "trigger") if "comparison" in n]
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0009"
    command.upgrade(cfg, "head")
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
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url)
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["comparison_flags"]["checks"]) == 4, "the comparison must see every CHECK"
    assert len(migrated["comparison_flags"]["foreign_keys"]) == 4
    assert len(migrated["comparison_runs"]["foreign_keys"]) == 4
    for table in TABLES:
        for part in model[table]:
            assert migrated[table][part] == model[table][part], (table, part)


# --- constraints -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "weird"},
        {"status": "weird"},
        {"field": "weird"},
        {"field": None},  # a "different" flag names its field
        {"kind": "not_sent", "field": "total_doc"},  # only different / bad_number have a field
        {"kind": "bad_number", "field": None},
    ],
    ids=lambda o: ",".join(f"{k}={v}" for k, v in o.items()),
)
def test_flag_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, flag(w, **overrides))


def test_the_valid_shapes_are_accepted(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    _add(
        w,
        w.a,
        flag(w),
        flag(w, doc_number="3940000002", kind="not_sent", field=None, sale_id=None, app_value=None),
        flag(w, doc_number="3940000003", kind="not_in_amadeus", field=None),
        flag(w, doc_number="3940000004", kind="elsewhere", field=None),
        flag(w, doc_number="12AB", kind="bad_number", field="doc_number", sale_id=None),
        flag(w, status="superseded"),  # the same key again, no longer live
    )
    assert _scalar(db_url, "SELECT count(*) FROM comparison_flags") == 6


@pytest.mark.parametrize(
    "overrides",
    [
        {"matched_count": -1},
        {"flag_count": -1, "blocking_count": -1},
        {"cancelled_count": -1},
        {"move_count": -1},
        {"partner_missing_count": -1},
        {"flag_count": 1, "blocking_count": 2},
    ],
    ids=lambda o: ",".join(o),
)
def test_run_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, _run(w.a, w.day_a, w.upload_a, w.user_a, **overrides))


def test_a_run_by_the_system_has_no_user(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    _add(w, w.a, _run(w.a, w.day_a, w.upload_a, None))
    assert (
        _scalar(db_url, "SELECT count(*) FROM comparison_runs WHERE started_by_user_id IS NULL")
        == 1
    )


def test_nothing_can_point_into_another_agency(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises(IntegrityError):  # a day of agency b
        _add(w, w.a, _run(w.a, w.day_b, w.upload_a, w.user_a))
    with pytest.raises(IntegrityError):  # an upload of agency b
        _add(w, w.a, _run(w.a, w.day_a, w.upload_b, w.user_a))
    with pytest.raises(IntegrityError):  # a user of agency b
        _add(w, w.a, _run(w.a, w.day_a, w.upload_a, w.user_b))
    with pytest.raises(IntegrityError):  # a sale of agency b
        _add(w, w.a, flag(w, sale_id=w.sale_b))
    with pytest.raises(IntegrityError):  # a day of agency b
        _add(w, w.a, flag(w, report_day_id=w.day_b))
    with pytest.raises(IntegrityError):  # a run of agency a on a flag of agency b
        _add(w, w.b, flag(w, agency_id=w.b, report_day_id=w.day_b, sale_id=w.sale_b))


def test_rows_with_dependents_cannot_be_deleted(db_url: str) -> None:
    migrate(db_url)
    w = _world(db_url)
    _add(w, w.a, flag(w))
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


def test_runs_are_append_only_on_sqlite_and_flags_stay_editable(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies and grants")
    migrate(db_url)
    w = _world(db_url)
    _add(w, w.a, flag(w))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(update(ComparisonRun).values(matched_count=99))
            s.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(delete(ComparisonRun))
            s.rollback()
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            s.execute(update(ComparisonFlag).values(status="answered"))
            s.commit()
    finally:
        engine.dispose()
    assert _scalar(db_url, "SELECT matched_count FROM comparison_runs") == 3
    assert _scalar(db_url, "SELECT status FROM comparison_flags") == "answered"


# --- Postgres only -----------------------------------------------------------------------------


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


def test_postgres_allows_one_live_flag_per_key(pg_url: str) -> None:
    migrate(pg_url)
    w = _world(pg_url)
    _add(w, w.a, flag(w))
    with pytest.raises(IntegrityError):
        _add(w, w.a, flag(w, status="answered"))  # same key, still live
    with pytest.raises(IntegrityError):
        _add(w, w.a, flag(w, status="resolved"))
    _add(w, w.a, flag(w, status="superseded"), flag(w, status="moved"))  # history is free
    _add(w, w.a, flag(w, field="rloc"))  # another field is another key
    _add(w, w.a, flag(w, doc_number="3940000002", kind="not_sent", field=None))
    with pytest.raises(IntegrityError):  # a NULL field is a key too (coalesce)
        _add(w, w.a, flag(w, doc_number="3940000002", kind="not_sent", field=None))


def test_postgres_forces_rls_with_the_expected_policies(pg_url: str) -> None:
    migrate(pg_url)
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            forced = {
                n: (rls, force)
                for n, rls, force in conn.execute(
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
    assert forced == {t: (True, True) for t in TABLES}
    assert policies == {
        ("comparison_runs", "tenant_read", "SELECT"),
        ("comparison_runs", "tenant_insert", "INSERT"),
        ("comparison_flags", "tenant_isolation", "ALL"),
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
    with app_role_env(role):
        command.downgrade(alembic_config(pg_url), "0009")
    assert not set(TABLES) & _tables(pg_url)
    assert _privileges(pg_url, role, "sales") == {"SELECT", "INSERT", "UPDATE"}  # untouched
    migrate(pg_url, app_role=role)
    assert set(TABLES) <= _tables(pg_url)


def test_postgres_single_role_mode_grants_nothing_and_does_not_fail(pg_url: str) -> None:
    role = _app_role()
    migrate(pg_url, app_role=None)
    for table in TABLES:
        assert _privileges(pg_url, role, table) == set(), table
