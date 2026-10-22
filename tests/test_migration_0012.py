"""Migration 0012: the append-only ``day_exports`` table (SQLite always; Postgres when given)."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import Base, make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import DayExport, ReportDay, User
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import TEST_APP_DATABASE_URL_ENV, alembic_config, app_role_env, migrate

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
DAY = date(2026, 10, 20)
CONTENT = b"PK-not-really-a-workbook"


def affected(result: Any) -> int:
    rowcount: int = result.rowcount
    return rowcount


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


class W:
    def __init__(self, url: str) -> None:
        self.url = url
        engine = make_engine(url)
        try:
            with make_session_factory(engine)() as s:
                self.a = create_agency(s, "agence-a", "Agence A").id
                self.b = create_agency(s, "agence-b", "Agence B").id
                ids: dict[str, int] = {}
                for key, agency, phone in (
                    ("a", self.a, "+22212370101"),
                    ("b", self.b, "+22212370102"),
                ):
                    rebind_tenant(s, agency)
                    user = User(agency_id=agency, phone=phone, role="manager")
                    day = ReportDay(agency_id=agency, day=DAY, status="open")
                    s.add_all([user, day])
                    s.commit()
                    ids[f"user_{key}"], ids[f"day_{key}"] = user.id, day.id
                self.user_a, self.user_b = ids["user_a"], ids["user_b"]
                self.day_a, self.day_b = ids["day_a"], ids["day_b"]
        finally:
            engine.dispose()

    def export(self, **kw: Any) -> DayExport:
        fields: dict[str, Any] = {
            "agency_id": self.a,
            "report_day_id": self.day_a,
            "version": 1,
            "content": CONTENT,
            "sha256": hashlib.sha256(CONTENT).hexdigest(),
            "created_by_user_id": self.user_a,
            "created_at": NOW,
        }
        fields.update(kw)
        return DayExport(**fields)

    def add(self, agency: int, *rows: Any) -> None:
        engine = make_engine(self.url)
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


# --- chain and shape ------------------------------------------------------------------------


def test_0012_follows_0011_and_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0012")
    assert revision is not None
    assert revision.down_revision == "0011"
    assert script.get_heads() == ["0012"]


def test_grants_are_append_only() -> None:
    assert GRANTS["day_exports"] == "SELECT, INSERT"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects; the Postgres tests are below")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0011")
    assert "day_exports" not in _tables(db_url)
    command.upgrade(cfg, "0012")
    assert "day_exports" in _tables(db_url)
    triggers = {n for n in _names(db_url, "trigger") if "day_exports" in n}
    assert triggers == {"trg_day_exports_no_update", "trg_day_exports_no_delete"}
    engine = create_engine(db_url)
    try:
        uniques = {
            tuple(u["column_names"]) for u in inspect(engine).get_unique_constraints("day_exports")
        }
        assert uniques == {("agency_id", "report_day_id", "version")}
    finally:
        engine.dispose()
    command.downgrade(cfg, "0011")
    assert "day_exports" not in _tables(db_url)
    assert not [n for n in _names(db_url, "trigger") if "day_exports" in n]
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0011"
    command.upgrade(cfg, "head")
    assert "day_exports" in _tables(db_url)


def _shape(url: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {
            "columns": {
                c["name"]: (c["nullable"], str(c["type"]).upper(), str(c["default"]))
                for c in insp.get_columns("day_exports")
            },
            "uniques": sorted(
                (u["name"], u["column_names"]) for u in insp.get_unique_constraints("day_exports")
            ),
            "foreign_keys": sorted(
                (
                    fk["name"],
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                    fk["options"].get("ondelete"),
                )
                for fk in insp.get_foreign_keys("day_exports")
            ),
            "checks": sorted(
                (c["name"], " ".join(str(c["sqltext"]).split()))
                for c in insp.get_check_constraints("day_exports")
            ),
        }
    finally:
        engine.dispose()


def test_the_migrated_table_matches_the_model(db_url: str, tmp_path: Any) -> None:
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url)
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["foreign_keys"]) == 3 and len(migrated["checks"]) >= 2
    for part in model:
        assert migrated[part] == model[part], part


# --- constraints ----------------------------------------------------------------------------


def test_versions_are_unique_per_day_and_start_at_one(db_url: str) -> None:
    migrate(db_url)
    w = W(db_url)
    w.add(w.a, w.export())
    with pytest.raises(IntegrityError):
        w.add(w.a, w.export())  # same day, same version
    with pytest.raises(IntegrityError):
        w.add(w.a, w.export(version=0))
    w.add(w.a, w.export(version=2))
    w.add(w.b, w.export(agency_id=w.b, report_day_id=w.day_b, created_by_user_id=w.user_b))


@pytest.mark.parametrize("sha", ["", "abc", "z" * 64, "A" * 64, "a" * 65])
def test_the_hash_is_64_lowercase_hex(db_url: str, sha: str) -> None:
    migrate(db_url)
    w = W(db_url)
    with pytest.raises((IntegrityError, DBAPIError)):
        w.add(w.a, w.export(sha256=sha))


def test_empty_content_is_refused(db_url: str) -> None:
    migrate(db_url)
    w = W(db_url)
    with pytest.raises(IntegrityError):
        w.add(w.a, w.export(content=b""))


def test_content_over_5_mb_is_refused(db_url: str) -> None:
    migrate(db_url)
    w = W(db_url)
    with pytest.raises((IntegrityError, DBAPIError)):
        w.add(w.a, w.export(content=b"x" * 5_000_001))
    w.add(w.a, w.export(content=b"x" * 5_000_000))


def test_an_export_cannot_point_into_another_agency(db_url: str) -> None:
    migrate(db_url)
    w = W(db_url)
    with pytest.raises(IntegrityError):  # a day of agency b
        w.add(w.a, w.export(report_day_id=w.day_b))
    with pytest.raises(IntegrityError):  # a user of agency b
        w.add(w.a, w.export(created_by_user_id=w.user_b))


def test_a_day_with_exports_cannot_be_deleted(db_url: str) -> None:
    migrate(db_url)
    w = W(db_url)
    w.add(w.a, w.export())
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            with pytest.raises((IntegrityError, DBAPIError)):
                s.execute(delete(ReportDay).where(ReportDay.id == w.day_a))
                s.commit()
    finally:
        engine.dispose()


def test_exports_are_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies below")
    migrate(db_url)
    w = W(db_url)
    w.add(w.a, w.export())
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(update(DayExport).values(content=b"changed"))
            s.rollback()
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(delete(DayExport))
            s.rollback()
    finally:
        engine.dispose()
    assert _scalar(db_url, "SELECT count(*) FROM day_exports") == 1


# --- Postgres only (written here, skipped without a server) ---------------------------------


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


def test_postgres_forces_rls_with_read_and_insert_policies_only(pg_url: str) -> None:
    migrate(pg_url)
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            flags = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'day_exports'"
                )
            ).one()
            policies = {
                (n, cmd)
                for n, cmd in conn.execute(
                    text("SELECT policyname, cmd FROM pg_policies WHERE tablename = 'day_exports'")
                )
            }
    finally:
        engine.dispose()
    assert tuple(flags) == (True, True)
    assert policies == {("tenant_read", "SELECT"), ("tenant_insert", "INSERT")}


def test_postgres_grants_select_and_insert_only_and_downgrade_revokes(pg_url: str) -> None:
    role = _app_role()
    migrate(pg_url, app_role=role)
    assert _privileges(pg_url, role, "day_exports") == {"SELECT", "INSERT"}
    with app_role_env(role):
        command.downgrade(alembic_config(pg_url), "0011")
    assert "day_exports" not in _tables(pg_url)
    assert _privileges(pg_url, role, "sales") == {"SELECT", "INSERT", "UPDATE"}  # untouched
    migrate(pg_url, app_role=role)


def test_postgres_isolation_and_append_only(pg_url: str) -> None:
    migrate(pg_url)
    w = W(pg_url)
    w.add(w.a, w.export())
    w.add(w.b, w.export(agency_id=w.b, report_day_id=w.day_b, created_by_user_id=w.user_b))
    engine = make_engine(pg_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            rows = s.query(DayExport).all()
            assert [r.agency_id for r in rows] == [w.a]  # agency b's export is invisible
            assert affected(s.execute(update(DayExport).values(content=b"x"))) == 0
            assert affected(s.execute(delete(DayExport))) == 0
            s.rollback()
            rebind_tenant(s, w.a)
            with pytest.raises((IntegrityError, DBAPIError)):  # a row for the other agency
                s.add(
                    w.export(
                        agency_id=w.b, report_day_id=w.day_b, created_by_user_id=w.user_b, version=2
                    )
                )
                s.commit()
    finally:
        engine.dispose()
