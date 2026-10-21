"""Migration 0005: the users role CHECK allows 'accountant'."""

from __future__ import annotations

import re

import pytest
from alembic import command
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.db import make_engine, make_session_factory
from app.models import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_STAFF, StaffSignLink, User
from app.repository import create_agency
from app.tenancy import rebind_tenant
from tests.conftest import alembic_config, migrate

ACCOUNTANT_PHONE = "+22212345799"


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _add_agency_with_users(url: str, *roles: str) -> int:
    """An agency whose users have phones +22212345700, ...701 and the given roles."""
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            agency_id = create_agency(s, "agence-m", "Agence M").id
            rebind_tenant(s, agency_id)
            for i, role in enumerate(roles):
                s.add(User(agency_id=agency_id, phone=f"+2221234570{i}", role=role))
            s.commit()
        return agency_id
    finally:
        engine.dispose()


def _add_user(url: str, agency_id: int, phone: str, role: str) -> None:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency_id)
            s.add(User(agency_id=agency_id, phone=phone, role=role))
            s.commit()
    finally:
        engine.dispose()


def _roles(url: str, agency_id: int) -> list[tuple[str, str]]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency_id)
            return [(u.phone, u.role) for u in s.scalars(select(User).order_by(User.id))]
    finally:
        engine.dispose()


def test_0004_refuses_an_accountant(db_url: str) -> None:
    migrate(db_url, revision="0004")
    agency = _add_agency_with_users(db_url, ROLE_MANAGER)
    with pytest.raises(IntegrityError):
        _add_user(db_url, agency, ACCOUNTANT_PHONE, ROLE_ACCOUNTANT)


def test_upgrade_allows_an_accountant_and_keeps_rows(db_url: str) -> None:
    migrate(db_url, revision="0004")
    agency = _add_agency_with_users(db_url, ROLE_MANAGER, ROLE_STAFF)
    before = _roles(db_url, agency)
    command.upgrade(alembic_config(db_url), "0005")
    assert _roles(db_url, agency) == before
    _add_user(db_url, agency, ACCOUNTANT_PHONE, ROLE_ACCOUNTANT)
    assert (ACCOUNTANT_PHONE, ROLE_ACCOUNTANT) in _roles(db_url, agency)
    with pytest.raises(IntegrityError):
        _add_user(db_url, agency, "+22212345798", "root")
    with pytest.raises(IntegrityError):
        _add_user(db_url, agency, "+22212345700", ROLE_STAFF)  # the phone stays unique


def test_sqlite_rebuild_keeps_dependent_rows_and_foreign_keys(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite table rebuild")
    migrate(db_url, revision="0004")
    agency = _add_agency_with_users(db_url, ROLE_MANAGER, ROLE_STAFF)
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            staff = s.scalars(select(User).where(User.role == ROLE_STAFF)).one()
            s.add(StaffSignLink(agency_id=agency, user_id=staff.id, staff_sign="0001AA"))
            s.commit()
    finally:
        engine.dispose()
    command.upgrade(alembic_config(db_url), "0005")
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM staff_sign_links")).scalar() == 1
            assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 2
    finally:
        engine.dispose()


def test_downgrade_removes_the_option_again(db_url: str) -> None:
    cfg = alembic_config(db_url)
    migrate(db_url)
    agency = _add_agency_with_users(db_url, ROLE_MANAGER, ROLE_STAFF)
    before = _roles(db_url, agency)
    command.downgrade(cfg, "0004")
    assert _roles(db_url, agency) == before
    with pytest.raises(IntegrityError):
        _add_user(db_url, agency, ACCOUNTANT_PHONE, ROLE_ACCOUNTANT)
    command.upgrade(cfg, "head")
    _add_user(db_url, agency, ACCOUNTANT_PHONE, ROLE_ACCOUNTANT)


def test_downgrade_is_refused_while_an_accountant_exists(db_url: str) -> None:
    cfg = alembic_config(db_url)
    migrate(db_url)
    agency = _add_agency_with_users(db_url, ROLE_MANAGER, ROLE_ACCOUNTANT)
    with pytest.raises(RuntimeError, match="accountant"):
        command.downgrade(cfg, "0004")
    assert ("+22212345701", ROLE_ACCOUNTANT) in _roles(db_url, agency)  # nothing changed


def test_postgres_allows_an_accountant_after_upgrade(db_url: str) -> None:
    if not _is_postgres(db_url):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    migrate(db_url)
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            definition = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_users_role' AND conrelid = 'users'::regclass"
                )
            ).scalar()
    finally:
        engine.dispose()
    assert definition is not None and "accountant" in definition
    agency = _add_agency_with_users(db_url, ROLE_ACCOUNTANT)
    assert _roles(db_url, agency) == [("+22212345700", ROLE_ACCOUNTANT)]


def test_a_single_upgrade_from_0003_runs_through_the_sqlite_rebuild(db_url: str) -> None:
    """0004 leaves a transaction open on SQLite; 0005 must still switch foreign keys off."""
    migrate(db_url, revision="0003")
    agency = _add_agency_with_users(db_url, ROLE_MANAGER, ROLE_STAFF)
    before = _roles(db_url, agency)
    migrate(db_url)
    assert _roles(db_url, agency) == before


def _users_ddl(url: str) -> str:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            ddl = conn.execute(text("SELECT sql FROM sqlite_master WHERE name = 'users'")).scalar()
    finally:
        engine.dispose()
    return str(ddl)


def _normalised(ddl: str) -> set[str]:
    """The pieces of a CREATE TABLE, split on commas outside parentheses, without whitespace."""
    parts, depth, current = [], 0, ""
    body = ddl[ddl.index("(") + 1 : ddl.rindex(")")]
    for char in body:
        depth += char == "("
        depth -= char == ")"
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    pieces = {re.sub(r"\s+", " ", p).strip().replace('"', "") for p in parts}
    # the rebuild writes the same default as DEFAULT (CURRENT_TIMESTAMP): not a difference
    return {p.replace("(CURRENT_TIMESTAMP)", "CURRENT_TIMESTAMP") for p in pieces}


def test_sqlite_rebuild_changes_only_the_role_check(db_url: str) -> None:
    if _is_postgres(db_url):
        pytest.skip("SQLite table rebuild")
    migrate(db_url, revision="0004")
    before = _normalised(_users_ddl(db_url))
    command.upgrade(alembic_config(db_url), "0005")
    after = _normalised(_users_ddl(db_url))
    changed_before = {p for p in before - after}
    changed_after = {p for p in after - before}
    assert len(changed_before) == 1 and "'manager', 'staff'" in next(iter(changed_before))
    assert len(changed_after) == 1 and "'accountant'" in next(iter(changed_after))
    joined = " | ".join(after)
    for kept in ("uq_users_phone", "uq_users_agency_id_id", "ck_users_phone_e164"):
        assert kept in joined
    assert "REFERENCES agencies" in joined


def _force_rls_on_users(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'users'::regclass")
                ).scalar()
            )
    finally:
        engine.dispose()


def test_postgres_force_row_security_survives_a_refused_and_a_successful_downgrade(
    db_url: str,
) -> None:
    if not _is_postgres(db_url):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    cfg = alembic_config(db_url)
    migrate(db_url)
    agency = _add_agency_with_users(db_url, ROLE_MANAGER, ROLE_ACCOUNTANT)
    assert _force_rls_on_users(db_url)
    with pytest.raises(RuntimeError, match="accountant"):
        command.downgrade(cfg, "0004")
    assert _force_rls_on_users(db_url)  # the refusal must not leave FORCE lifted
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency)
            s.query(User).filter(User.role == ROLE_ACCOUNTANT).update({"role": ROLE_STAFF})
            s.commit()
    finally:
        engine.dispose()
    command.downgrade(cfg, "0004")
    assert _force_rls_on_users(db_url)
