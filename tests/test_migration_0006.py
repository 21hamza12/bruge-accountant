"""Migration 0006: the append-only ``amadeus_lines`` table."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.db import Base
from app.db_roles import GRANTS
from tests.conftest import TEST_APP_DATABASE_URL_ENV, alembic_config, app_role_env, migrate


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def test_grants_are_select_and_insert_only() -> None:
    assert GRANTS["amadeus_lines"] == "SELECT, INSERT"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects; the Postgres test is below")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0005")
    engine = create_engine(db_url)
    try:
        assert "amadeus_lines" not in inspect(engine).get_table_names()
        command.upgrade(cfg, "0006")
        with engine.connect() as conn:
            triggers = set(
                conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                ).scalars()
            )
        assert {"trg_amadeus_lines_no_update", "trg_amadeus_lines_no_delete"} <= triggers
        insp = inspect(engine)
        assert "amadeus_lines" in insp.get_table_names()
        indexes = {i["name"]: i["column_names"] for i in insp.get_indexes("amadeus_lines")}
        assert indexes["ix_amadeus_lines_agency_upload_order"] == [
            "agency_id",
            "upload_id",
            "seq_order",
        ]
        assert set(indexes) == {"ix_amadeus_lines_agency_upload_order"}
        uniques = [u["column_names"] for u in insp.get_unique_constraints("amadeus_lines")]
        assert ["agency_id", "upload_id", "doc_number"] in uniques
        composite = [
            fk
            for fk in insp.get_foreign_keys("amadeus_lines")
            if len(fk["constrained_columns"]) == 2
        ]
        assert len(composite) == 1
        assert composite[0]["referred_table"] == "uploads"
        assert composite[0]["referred_columns"] == ["agency_id", "id"]
        assert composite[0]["options"].get("ondelete") == "RESTRICT"

        command.downgrade(cfg, "0005")
        with engine.connect() as conn:
            left = conn.execute(
                text("SELECT name FROM sqlite_master WHERE name LIKE '%amadeus_lines%'")
            ).all()
        assert left == []
        command.upgrade(cfg, "head")  # and up again
        assert "amadeus_lines" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def _shape(url: str) -> dict[str, Any]:
    """What the table looks like to the inspector, comparable between two databases."""
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        name = "amadeus_lines"
        return {
            "columns": {
                c["name"]: (c["nullable"], str(c["type"]).upper()) for c in insp.get_columns(name)
            },
            "indexes": {
                i["name"]: (i["column_names"], i["unique"]) for i in insp.get_indexes(name)
            },
            "uniques": sorted(
                (u["name"], u["column_names"]) for u in insp.get_unique_constraints(name)
            ),
            "foreign_keys": sorted(
                (
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
    finally:
        engine.dispose()


def test_the_migrated_table_matches_the_model(db_url: str, tmp_path: Path) -> None:
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url)
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["checks"]) == 4, "the comparison must see every CHECK constraint"
    for part in model:
        assert migrated[part] == model[part], part


def test_postgres_up_and_down(db_url: str) -> None:
    if not _is_postgres(db_url):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    app_url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    assert app_url
    role = make_url(app_url).username
    cfg = alembic_config(db_url)
    migrate(db_url, app_role=role, revision="0005")
    engine = create_engine(db_url)
    try:
        migrate(db_url, app_role=role)
        with engine.connect() as conn:
            flags = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'amadeus_lines'"
                )
            ).one()
            policies = set(
                conn.execute(
                    text(
                        "SELECT policyname, cmd FROM pg_policies WHERE tablename = 'amadeus_lines'"
                    )
                ).all()
            )
            privileges = set(
                conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_name = 'amadeus_lines' AND grantee = :g"
                    ),
                    {"g": role},
                ).scalars()
            )
        assert tuple(flags) == (True, True)
        assert policies == {("tenant_read", "SELECT"), ("tenant_insert", "INSERT")}
        assert privileges == {"SELECT", "INSERT"}

        with app_role_env(role):
            command.downgrade(cfg, "0005")
        with engine.connect() as conn:
            assert "amadeus_lines" not in inspect(conn).get_table_names()
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_policies WHERE tablename = 'amadeus_lines'")
                ).scalar()
                == 0
            )
        migrate(db_url, app_role=role)  # up again
        assert "amadeus_lines" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_postgres_single_role_mode_grants_nothing_but_still_has_rls(db_url: str) -> None:
    if not _is_postgres(db_url):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    migrate(db_url, app_role=None)
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(
                text(
                    "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'amadeus_lines'"
                )
            ).scalar()
    finally:
        engine.dispose()
