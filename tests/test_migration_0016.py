"""Migration 0016: ``ai_usage`` also accepts the purpose ``chat`` (SQLite; the Postgres policies are
unchanged and covered by tests/test_rls.py when a server is given)."""

from __future__ import annotations

from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, inspect, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import Base, make_engine, make_session_factory
from app.models import AI_PURPOSES, AiUsage
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate
from tests.rls_support import Tenant, seed_tenant


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _scalar(url: str, sql: str) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql)).scalar()
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


def _tenant(url: str) -> Tenant:
    return seed_tenant(url, "agence-a", "+22212390101", "a1" * 32, 3940000000, ai=True)


def usage(t: Tenant, **kw: Any) -> AiUsage:
    fields: dict[str, Any] = {
        "agency_id": t.agency_id,
        "user_id": t.user_id,
        "purpose": "chat",
        "model": "claude-haiku-5-5",
        "input_tokens": 1500,
        "output_tokens": 120,
        "cost_micro_usd": 210,
        "outcome": "ok",
    }
    fields.update(kw)
    return AiUsage(**fields)


def _add(url: str, agency: int, *rows: Any) -> None:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.add_all(rows)
            s.commit()
    finally:
        engine.dispose()


def _tenant_scalar(url: str, agency: int, sql: str) -> object:
    """Read through a tenant-bound session: on Postgres the owner sees no row of a FORCE RLS
    table without an agency."""
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            return s.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_0016_follows_0015_and_is_the_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0016")
    assert revision is not None and revision.down_revision == "0015"
    assert len(script.get_heads()) == 1  # a newer migration is the head: see its own test
    assert AI_PURPOSES == ("screenshot", "chat")


def test_a_chat_row_is_refused_before_and_accepted_after(db_url: str) -> None:
    migrate(db_url, revision="0015")
    t = _tenant(db_url)
    with pytest.raises(IntegrityError):
        _add(db_url, t.agency_id, usage(t))
    migrate(db_url, revision="0016")
    _add(db_url, t.agency_id, usage(t), usage(t, purpose="screenshot"))
    sql = "SELECT count(*) FROM ai_usage WHERE purpose = 'chat'"
    assert _tenant_scalar(db_url, t.agency_id, sql) == 1


def test_a_purpose_that_is_neither_is_still_refused(db_url: str) -> None:
    migrate(db_url, revision="0016")
    t = _tenant(db_url)
    with pytest.raises(IntegrityError):
        _add(db_url, t.agency_id, usage(t, purpose="weekly"))


def test_the_rows_and_the_other_checks_survive_the_rebuild(db_url: str) -> None:
    migrate(db_url, revision="0015")
    t = _tenant(db_url)
    total = "SELECT sum(cost_micro_usd) FROM ai_usage"
    before = _tenant_scalar(db_url, t.agency_id, total)
    assert before  # the seeded tenant has usage rows
    migrate(db_url, revision="0016")
    assert _tenant_scalar(db_url, t.agency_id, total) == before
    for bad in ({"outcome": "weird"}, {"input_tokens": -1}, {"cost_micro_usd": -1}):
        with pytest.raises(IntegrityError):
            _add(db_url, t.agency_id, usage(t, **bad))
    with pytest.raises(IntegrityError):  # the composite foreign key is still there
        _add(db_url, t.agency_id, usage(t, user_id=t.user_id + 999))


def _shape(url: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        insp = inspect(engine)
        return {
            "columns": {
                c["name"]: (c["nullable"], str(c["type"]).upper())
                for c in insp.get_columns("ai_usage")
            },
            "indexes": {
                i["name"]: (i["column_names"], i["unique"]) for i in insp.get_indexes("ai_usage")
            },
            "foreign_keys": sorted(
                (fk["name"], fk["constrained_columns"], fk["referred_table"])
                for fk in insp.get_foreign_keys("ai_usage")
            ),
            "checks": sorted(
                (c["name"], " ".join(str(c["sqltext"]).split()))
                for c in insp.get_check_constraints("ai_usage")
            ),
        }
    finally:
        engine.dispose()


def test_the_table_matches_the_model_after_the_rebuild(db_url: str, tmp_path: Any) -> None:
    if _is_postgres(db_url):
        pytest.skip("compared on SQLite")
    migrate(db_url, revision="0016")
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    engine = create_engine(model_url)
    Base.metadata.create_all(engine)
    engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["checks"]) == 3 and len(migrated["foreign_keys"]) == 2
    assert migrated == model


def test_the_append_only_triggers_survive_the_rebuild(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies and grants")
    migrate(db_url, revision="0016")
    assert {"trg_ai_usage_no_update", "trg_ai_usage_no_delete"} <= _names(db_url, "trigger")
    t = _tenant(db_url)
    _add(db_url, t.agency_id, usage(t))
    engine = make_engine(db_url)
    try:
        for statement in (update(AiUsage).values(cost_micro_usd=0), delete(AiUsage)):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, t.agency_id)
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(statement)
                s.rollback()
    finally:
        engine.dispose()


def test_the_downgrade_refuses_while_a_chat_row_exists(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0016")
    t = _tenant(db_url)
    _add(db_url, t.agency_id, usage(t))
    with pytest.raises(RuntimeError, match="Nothing was changed"):
        command.downgrade(cfg, "0015")
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0016"
    sql = "SELECT count(*) FROM ai_usage WHERE purpose = 'chat'"
    assert _tenant_scalar(db_url, t.agency_id, sql) == 1
    assert {"trg_ai_usage_no_update", "trg_ai_usage_no_delete"} <= _names(db_url, "trigger")


def test_the_downgrade_works_without_chat_rows_and_goes_up_again(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0016")
    t = _tenant(db_url)
    _add(db_url, t.agency_id, usage(t, purpose="screenshot"))
    command.downgrade(cfg, "0015")
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0015"
    assert {"trg_ai_usage_no_update", "trg_ai_usage_no_delete"} <= _names(db_url, "trigger")
    assert _scalar(db_url, "SELECT count(*) FROM ai_usage WHERE purpose = 'screenshot'") == 2
    with pytest.raises(IntegrityError):
        _add(db_url, t.agency_id, usage(t))
    command.upgrade(cfg, "0016")
    _add(db_url, t.agency_id, usage(t))
