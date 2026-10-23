"""Migration 0015: the append-only ``chat_messages`` table (SQLite; the Postgres policies are
covered by tests/test_rls.py when a server is given, which this suite never needs)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, delete, func, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import Base, make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import CHAT_MAX_TEXT, ChatMessage, User
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate

TABLE = "chat_messages"


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


@dataclass(frozen=True)
class W:
    url: str
    a: int
    b: int
    user_a: int
    user_b: int


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
                user = User(agency_id=agency, phone=phone, role="staff")
                s.add(user)
                s.commit()
                ids.update({key: agency, f"user_{key}": user.id})
            return W(url, ids["a"], ids["b"], ids["user_a"], ids["user_b"])
    finally:
        engine.dispose()


def message(w: W, **kw: Any) -> ChatMessage:
    fields: dict[str, Any] = {
        "agency_id": w.a,
        "user_id": w.user_a,
        "role": "user",
        "text": "mes billets",
    }
    fields.update(kw)
    return ChatMessage(**fields)


def _add(w: W, agency: int, *rows: Any) -> None:
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.add_all(rows)
            s.commit()
    finally:
        engine.dispose()


def _count_for(w: W, agency: int) -> int:
    """Read through a tenant-bound session: on Postgres the owner sees no row of a FORCE RLS
    table without an agency."""
    engine = make_engine(w.url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            return s.scalar(select(func.count()).select_from(ChatMessage)) or 0
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


def test_0015_follows_0014_and_is_the_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0015")
    assert revision is not None and revision.down_revision == "0014"
    assert script.get_heads() == ["0015"]


def test_grants_follow_the_design() -> None:
    assert GRANTS[TABLE] == "SELECT, INSERT"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects")
    cfg = alembic_config(db_url)
    migrate(db_url, revision="0014")
    assert TABLE not in _tables(db_url)
    command.upgrade(cfg, "0015")
    assert TABLE in _tables(db_url)
    assert {n for n in _names(db_url, "trigger") if TABLE in n} == {
        "trg_chat_messages_no_update",
        "trg_chat_messages_no_delete",
    }
    engine = create_engine(db_url)
    try:
        indexes = {i["name"]: i["column_names"] for i in inspect(engine).get_indexes(TABLE)}
    finally:
        engine.dispose()
    assert indexes["ix_chat_messages_agency_user_created"] == ["agency_id", "user_id", "created_at"]
    command.downgrade(cfg, "0014")
    assert TABLE not in _tables(db_url)
    assert not [n for n in _names(db_url, "trigger") if TABLE in n]
    assert _scalar(db_url, "SELECT version_num FROM alembic_version") == "0014"
    command.upgrade(cfg, "0015")
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
    migrate(db_url, revision="0015")
    model_url = f"sqlite:///{tmp_path / 'model.db'}"
    model_engine = create_engine(model_url)
    Base.metadata.create_all(model_engine)
    model_engine.dispose()
    migrated, model = _shape(db_url), _shape(model_url)
    assert len(migrated["checks"]) == 2 and len(migrated["foreign_keys"]) == 2
    for part in model:
        assert migrated[part] == model[part], part


# --- constraints -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "weird"},
        {"role": "manager"},
        {"role": ""},
        {"text": ""},
        {"text": "x" * (CHAT_MAX_TEXT + 1)},
    ],
    ids=lambda o: ",".join(f"{k}={str(v)[:12]}" for k, v in o.items()),
)
def test_message_checks(db_url: str, overrides: dict[str, Any]) -> None:
    migrate(db_url, revision="0015")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, message(w, **overrides))


def test_the_valid_messages_are_accepted(db_url: str) -> None:
    migrate(db_url, revision="0015")
    w = _world(db_url)
    _add(
        w,
        w.a,
        message(w),
        message(w, role="app", text="Voici ce que je sais faire :"),
        message(w, text="x"),
        message(w, text="é" * CHAT_MAX_TEXT),
    )
    assert _count_for(w, w.a) == 4


def test_a_message_cannot_point_at_a_user_of_another_agency(db_url: str) -> None:
    migrate(db_url, revision="0015")
    w = _world(db_url)
    with pytest.raises(IntegrityError):
        _add(w, w.a, message(w, user_id=w.user_b))
    with pytest.raises(IntegrityError):
        _add(w, w.b, message(w, agency_id=w.b))  # b's rows, a's user


def test_the_table_is_append_only_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite triggers; Postgres uses policies and grants")
    migrate(db_url, revision="0015")
    w = _world(db_url)
    _add(w, w.a, message(w))
    engine = make_engine(db_url)
    try:
        for statement in (
            update(ChatMessage).values(text="changed afterwards"),
            delete(ChatMessage),
        ):
            with make_session_factory(engine)() as s:
                rebind_tenant(s, w.a)
                with pytest.raises(DBAPIError, match="append-only"):
                    s.execute(statement)
                s.rollback()
    finally:
        engine.dispose()
    assert _scalar(db_url, "SELECT text FROM chat_messages") == "mes billets"


def test_the_user_of_a_conversation_cannot_be_deleted(db_url: str) -> None:
    migrate(db_url, revision="0015")
    w = _world(db_url)
    _add(w, w.a, message(w))
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, w.a)
            with pytest.raises((IntegrityError, DBAPIError)):
                s.execute(delete(User).where(User.id == w.user_a))
                s.commit()
    finally:
        engine.dispose()
