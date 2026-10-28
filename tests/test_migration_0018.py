"""Migration 0018: ``agency_details``, the agency's own details (SQLite here; the Postgres
row-level security, policies and grants are in tests/test_rls.py, which runs when a server is
given: in CI)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError

from app.db import Base, make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import AGENCY_DETAIL_LIMITS, AgencyDetails, User
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate

TABLE = "agency_details"


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _world(url: str) -> tuple[int, int, int, int]:
    """Two agencies with a manager each: ``(agency a, user a, agency b, user b)``."""
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            out: list[int] = []
            for slug, phone in (("agence-a", "+22212390201"), ("agence-b", "+22212390202")):
                agency = create_agency(s, slug, slug.upper()).id
                rebind_tenant(s, agency)
                manager = User(agency_id=agency, phone=phone, role="manager")
                s.add(manager)
                s.commit()
                out += [agency, manager.id]
            return out[0], out[1], out[2], out[3]
    finally:
        engine.dispose()


def _add(url: str, agency: int, row: AgencyDetails) -> None:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.add(row)
            s.commit()
    finally:
        engine.dispose()


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _triggers(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'trigger'"))
            return {str(n) for n in rows.scalars() if TABLE in str(n)}
    finally:
        engine.dispose()


def test_0018_follows_0017() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0018")
    assert revision is not None and revision.down_revision == "0017"
    assert len(script.get_heads()) == 1  # 0019 is the head now: see test_migration_0019


def test_grants_follow_the_design() -> None:
    assert GRANTS[TABLE] == "SELECT, INSERT, UPDATE"  # saved again in place; never DELETE


def test_the_migration_keeps_its_own_copy_of_the_limits() -> None:
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "migrations/versions/0018_agency_details.py"
    spec = importlib.util.spec_from_file_location("migration_0018_source", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LIMITS == AGENCY_DETAIL_LIMITS


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0017")
    assert TABLE not in _tables(db_url)
    command.upgrade(cfg, "0018")
    assert TABLE in _tables(db_url)
    assert _triggers(db_url) == {"trg_agency_details_no_delete"}
    command.downgrade(cfg, "0017")
    assert TABLE not in _tables(db_url) and not _triggers(db_url)
    command.upgrade(cfg, "0018")
    assert TABLE in _tables(db_url)


def _shape(url: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {
            "columns": {
                c["name"]: (c["nullable"], str(c["type"]).upper(), str(c["default"]))
                for c in insp.get_columns(TABLE)
            },
            "pk": insp.get_pk_constraint(TABLE)["constrained_columns"],
            "indexes": {
                i["name"]: (i["column_names"], i["unique"]) for i in insp.get_indexes(TABLE)
            },
            "foreign_keys": sorted(
                (
                    fk["name"],
                    fk["constrained_columns"],
                    fk["referred_table"],
                    fk["referred_columns"],
                    fk["options"].get("ondelete"),
                )
                for fk in insp.get_foreign_keys(TABLE)
            ),
            "checks": sorted(
                (c["name"], " ".join(str(c["sqltext"]).split()))
                for c in insp.get_check_constraints(TABLE)
            ),
        }
    finally:
        engine.dispose()


def test_the_migrated_table_matches_the_model(db_url: str, tmp_path: Path) -> None:
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url, revision="0018")
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["checks"]) == len(AGENCY_DETAIL_LIMITS) + 1
    assert len(migrated["foreign_keys"]) == 2 and migrated["pk"] == ["agency_id"]
    for part in model:
        assert migrated[part] == model[part], part


def test_one_row_per_agency(db_url: str) -> None:
    migrate(db_url, revision="0018")
    a, user_a, _, _ = _world(db_url)
    _add(db_url, a, AgencyDetails(agency_id=a, name_fr="Agence Test", updated_by_user_id=user_a))
    with pytest.raises(IntegrityError):
        _add(db_url, a, AgencyDetails(agency_id=a, nif="00000000", updated_by_user_id=user_a))


@pytest.mark.parametrize("column", sorted(AGENCY_DETAIL_LIMITS))
def test_each_detail_has_its_length_check(db_url: str, column: str) -> None:
    migrate(db_url, revision="0018")
    a, user_a, _, _ = _world(db_url)
    limit = AGENCY_DETAIL_LIMITS[column]
    with pytest.raises(IntegrityError):
        _add(db_url, a, AgencyDetails(agency_id=a, updated_by_user_id=user_a, **{column: ""}))
    # SQLite enforces the length CHECK; Postgres refuses the value at the VARCHAR length first.
    with pytest.raises((IntegrityError, DataError)):
        _add(
            db_url,
            a,
            AgencyDetails(agency_id=a, updated_by_user_id=user_a, **{column: "x" * (limit + 1)}),
        )
    _add(db_url, a, AgencyDetails(agency_id=a, updated_by_user_id=user_a, **{column: "x" * limit}))


def test_the_editor_must_be_a_user_of_the_same_agency(db_url: str) -> None:
    migrate(db_url, revision="0018")
    a, _, _, user_b = _world(db_url)
    with pytest.raises((IntegrityError, DBAPIError)):
        _add(
            db_url, a, AgencyDetails(agency_id=a, name_fr="Agence Test", updated_by_user_id=user_b)
        )


def test_the_row_is_updated_in_place_but_never_deleted_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite trigger; Postgres has no DELETE policy or grant (tests/test_rls.py)")
    migrate(db_url, revision="0018")
    a, user_a, _, _ = _world(db_url)
    _add(db_url, a, AgencyDetails(agency_id=a, name_fr="Agence Test", updated_by_user_id=user_a))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, a)
            s.execute(update(AgencyDetails).values(nif="00000000"))
            s.commit()
            with pytest.raises(DBAPIError, match="never deleted"):
                s.execute(delete(AgencyDetails))
            s.rollback()
            rebind_tenant(s, a)
            assert s.execute(select(AgencyDetails.name_fr, AgencyDetails.nif)).all() == [
                ("Agence Test", "00000000")
            ]
    finally:
        engine.dispose()
