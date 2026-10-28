"""Migration 0019: no partner line or payment dated before 1 January 2020 (SQLite here: a
trigger per table; on a server, in CI, the same tests run against Postgres's CHECKs)."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import Base
from app.models import PARTNER_DATE_FLOOR
from tests.conftest import alembic_config, migrate
from tests.test_migration_0017 import _add, _is_postgres, _world, entry, payment

FLOOR = date.fromisoformat(PARTNER_DATE_FLOOR)
BEFORE = date(2019, 12, 31)
TRIGGERS = {"trg_partner_entries_date_floor", "trg_partner_payments_date_floor"}


def _triggers(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            return {str(n) for n in rows.scalars() if "date_floor" in str(n)}
    finally:
        engine.dispose()


def _checks(url: str, table: str) -> dict[str, str]:
    engine = create_engine(url)
    try:
        return {
            str(c["name"]): " ".join(str(c["sqltext"]).split())
            for c in inspect(engine).get_check_constraints(table)
            if "floor" in str(c["name"])
        }
    finally:
        engine.dispose()


def test_0019_follows_0018_and_is_the_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0019")
    assert revision is not None and revision.down_revision == "0018"
    assert script.get_heads() == ["0019"]


def test_the_migration_keeps_its_own_copy_of_the_floor() -> None:
    path = Path(__file__).resolve().parent.parent / "migrations/versions/0019_partner_date_floor.py"
    spec = importlib.util.spec_from_file_location("migration_0019_source", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.FLOOR == PARTNER_DATE_FLOOR


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0018")
    assert _triggers(db_url) == set()
    command.upgrade(cfg, "0019")
    assert _triggers(db_url) == TRIGGERS
    command.downgrade(cfg, "0018")
    assert _triggers(db_url) == set()
    command.upgrade(cfg, "0019")
    assert _triggers(db_url) == TRIGGERS


def test_the_models_carry_the_same_floor(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'model.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    assert _checks(url, "partner_entries") == {
        "ck_partner_entries_entry_date_floor": "entry_date >= '2020-01-01'"
    }
    assert _checks(url, "partner_payments") == {
        "ck_partner_payments_paid_on_floor": "paid_on >= '2020-01-01'"
    }


def test_postgres_has_the_checks(db_url: str) -> None:
    if not _is_postgres(db_url):
        pytest.skip("Postgres CHECKs (CI)")
    migrate(db_url)
    assert set(_checks(db_url, "partner_entries")) == {"ck_partner_entries_entry_date_floor"}
    assert set(_checks(db_url, "partner_payments")) == {"ck_partner_payments_paid_on_floor"}


@pytest.mark.parametrize(
    ("make", "field"), [(entry, "entry_date"), (payment, "paid_on")], ids=["line", "payment"]
)
def test_a_date_before_2020_is_refused_and_the_floor_itself_accepted(
    db_url: str, make: Any, field: str
) -> None:
    migrate(db_url)
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, make(w, **{field: BEFORE}))
    with pytest.raises(IntegrityError):
        _add(w, w.a, make(w, **{field: date(2, 1, 1)}))
    assert _add(w, w.a, make(w, **{field: FLOOR}))


@pytest.mark.parametrize(
    ("make", "field"), [(entry, "entry_date"), (payment, "paid_on")], ids=["line", "payment"]
)
def test_the_model_schema_refuses_it_too(tmp_path: Path, make: Any, field: str) -> None:
    url = f"sqlite:///{tmp_path / 'model.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    w = _world(url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, make(w, **{field: BEFORE}))
