"""Migration 0010: partner_aliases (SQLite always; the Postgres parts only with a server)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.db import make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import ALIAS_PENDING, Partner, PartnerAlias, User
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_0010_follows_0009_and_is_the_single_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    revision = script.get_revision("0010")
    assert revision is not None and revision.down_revision == "0009"
    assert len(script.get_heads()) == 1


def test_grants_have_no_delete() -> None:
    assert GRANTS["partner_aliases"] == "SELECT, INSERT, UPDATE"


def test_up_and_down_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite objects; Postgres is run separately")
    migrate(db_url, revision="0009")
    assert "partner_aliases" not in _tables(db_url)
    command.upgrade(alembic_config(db_url), "0010")
    assert "partner_aliases" in _tables(db_url)
    engine = create_engine(db_url)
    try:
        insp = inspect(engine)
        assert {c["name"] for c in insp.get_columns("partner_aliases")} == {
            "id", "agency_id", "partner_id", "alias", "alias_key", "status",
            "proposed_by_user_id", "decided_by_user_id", "decided_at", "created_at",
        }  # fmt: skip
        assert {
            tuple(u["column_names"]) for u in insp.get_unique_constraints("partner_aliases")
        } == {("agency_id", "alias_key")}
        assert len(insp.get_foreign_keys("partner_aliases")) == 4
    finally:
        engine.dispose()
    command.downgrade(alembic_config(db_url), "0009")
    assert "partner_aliases" not in _tables(db_url)
    command.upgrade(alembic_config(db_url), "0010")
    assert "partner_aliases" in _tables(db_url)


def _world(url: str) -> tuple[int, int, int, int]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            a = create_agency(s, "agence-a", "Agence A").id
            b = create_agency(s, "agence-b", "Agence B").id
            rebind_tenant(s, a)
            user = User(agency_id=a, phone="+22212360101", role="manager")
            partner = Partner(agency_id=a, name="P", name_key="p")
            s.add_all([user, partner])
            s.commit()
            rebind_tenant(s, b)
            other = Partner(agency_id=b, name="Q", name_key="q")
            s.add(other)
            s.commit()
            return a, user.id, partner.id, other.id
    finally:
        engine.dispose()


def _insert(url: str, agency: int, **fields: object) -> None:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            base: dict[str, object] = {"agency_id": agency, "alias": "WG", "alias_key": "wg"}
            s.add(PartnerAlias(**{**base, **fields}))
            s.commit()
    finally:
        engine.dispose()


def test_the_table_refuses_what_it_should(db_url: str) -> None:
    migrate(db_url)
    a, user, partner, foreign_partner = _world(db_url)
    ok = {"partner_id": partner, "proposed_by_user_id": user}
    _insert(db_url, a, **ok)
    for bad in (
        {**ok},  # the same key twice in one agency
        {**ok, "alias_key": "x", "status": "bogus"},
        {**ok, "alias_key": ""},
        {**ok, "alias_key": "y", "status": "approved"},  # decided, but nobody decided it
        {**ok, "alias_key": "z", "decided_at": NOW},  # pending yet decided
        {"partner_id": foreign_partner, "proposed_by_user_id": user, "alias_key": "w"},
    ):
        with pytest.raises(IntegrityError):
            _insert(db_url, a, **bad)
    _insert(
        db_url,
        a,
        **ok,
        alias_key="done",
        status="approved",
        decided_at=NOW,
        decided_by_user_id=user,
    )
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, a)  # row-level security: the rows of this agency only
            rows = list(s.scalars(select(PartnerAlias)))
            assert sorted(r.alias_key for r in rows) == ["done", "wg"]
            assert {r.alias_key: r.status for r in rows}["wg"] == ALIAS_PENDING
    finally:
        engine.dispose()


def test_rows_are_never_deleted_on_sqlite(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("the Postgres guard is the missing DELETE grant and policy")
    migrate(db_url)
    a, user, partner, _ = _world(db_url)
    _insert(db_url, a, partner_id=partner, proposed_by_user_id=user)
    engine = create_engine(db_url)
    try:
        with engine.begin() as conn, pytest.raises(Exception, match="never deleted"):
            conn.execute(text("DELETE FROM partner_aliases"))
    finally:
        engine.dispose()
