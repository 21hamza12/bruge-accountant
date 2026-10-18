"""Migration 0003: row-level security, optional grants for the app role, users.display_name.

Cross-engine tests run on SQLite and Postgres (the Postgres-only parts of the migration are
skipped on SQLite). The rest need TEST_DATABASE_URL; the grant tests also TEST_APP_DATABASE_URL.
"""

from __future__ import annotations

import logging
import os

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app import db_roles
from app.db_roles import apply_grants, revoke_statements, role_privilege_problem
from tests.conftest import (
    TEST_APP_DATABASE_URL_ENV,
    RlsDatabase,
    alembic_config,
    app_role_env,
    migrate,
)
from tests.rls_support import tables_missing_rls

# privileges the application role must hold on each table, and nothing else
READ_WRITE = {"SELECT", "INSERT", "UPDATE"}
EXPECTED_TABLE_PRIVILEGES = {
    "agencies": {"SELECT"},
    "alembic_version": {"SELECT"},
    "uploads": READ_WRITE,
    "tickets": READ_WRITE,
    "users": READ_WRITE,
    "temporary_passwords": READ_WRITE | {"DELETE"},  # purge of used and expired passwords
    "staff_sign_links": READ_WRITE | {"DELETE"},
    "user_sessions": READ_WRITE | {"DELETE"},
    "rate_limit_buckets": READ_WRITE | {"DELETE"},
    # DELETE exists for the retention policy only; no policy lets a session delete a recent row
    "audit_events": {"SELECT", "INSERT", "DELETE"},
}
ALL_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture
def pg_url(db_url: str) -> str:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    return db_url


@pytest.fixture
def app_role() -> str:
    url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    if not url:
        pytest.skip("needs TEST_APP_DATABASE_URL")
    return make_url(url).username or ""


def scalar(url: str, sql: str, **params: object) -> object:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def privileges(url: str, role: str, table: str) -> set[str]:
    return {
        p
        for p in ALL_TABLE_PRIVILEGES
        if scalar(url, "SELECT has_table_privilege(:r, :t, :p)", r=role, t=table, p=p)
    }


def user_columns(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("users")}
    finally:
        engine.dispose()


# --- both engines --------------------------------------------------------------------


def test_upgrade_downgrade_upgrade_adds_and_removes_display_name(db_url: str) -> None:
    migrate(db_url, revision="0002")
    assert "display_name" not in user_columns(db_url)
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    assert "display_name" in user_columns(db_url)
    command.downgrade(cfg, "0002")
    assert "display_name" not in user_columns(db_url)
    command.upgrade(cfg, "head")
    assert "display_name" in user_columns(db_url)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


def test_display_name_is_nullable_text(db_url: str) -> None:
    migrate(db_url)
    engine = create_engine(db_url)
    try:
        column = next(
            c for c in inspect(engine).get_columns("users") if c["name"] == "display_name"
        )
    finally:
        engine.dispose()
    assert column["nullable"] is True
    assert column["type"].__class__.__name__ in {"TEXT", "Text"}


def test_sqlite_ignores_app_db_role(db_url: str) -> None:
    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite only")
    migrate(db_url, app_role="no_such_role")
    migrate(db_url, app_role='not a "valid" name')  # not even validated: there is nothing to grant
    assert "display_name" in user_columns(db_url)


# --- Postgres: modes and failures ----------------------------------------------------


def test_without_app_db_role_it_runs_single_role_mode_and_says_so(
    pg_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        migrate(pg_url, app_role=None)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("single-role" in w and "APP_DB_ROLE" in w for w in warnings), warnings
    assert tables_missing_rls_in(pg_url) == []


def test_a_blank_app_db_role_counts_as_unset(pg_url: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        migrate(pg_url, app_role="   ")
    assert any("single-role" in r.getMessage() for r in caplog.records)


def test_single_role_mode_grants_nothing_to_anybody_else(pg_url: str, app_role: str) -> None:
    migrate(pg_url, app_role=None)
    for table in EXPECTED_TABLE_PRIVILEGES:
        assert privileges(pg_url, app_role, table) == set(), table


def test_a_missing_role_fails_loudly_and_changes_nothing(pg_url: str) -> None:
    migrate(pg_url, revision="0002")
    with pytest.raises(RuntimeError, match="no_such_role"):
        migrate(pg_url, app_role="no_such_role")
    assert scalar(pg_url, "SELECT version_num FROM alembic_version") == "0002"
    assert "display_name" not in user_columns(pg_url)
    assert scalar(pg_url, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'") == 0
    assert (
        scalar(
            pg_url,
            "SELECT count(*) FROM pg_proc WHERE proname = 'app_current_agency_id' "
            "AND pronamespace = 'public'::regnamespace",
        )
        == 0
    )


@pytest.mark.parametrize(
    "name", ["Bad", "x;y", 'a"b', "1abc", "a" * 64, "bruge app", "'; DROP TABLE users; --"]
)
def test_an_invalid_role_name_is_refused_before_any_ddl(pg_url: str, name: str) -> None:
    migrate(pg_url, revision="0002")
    with pytest.raises(ValueError, match="APP_DB_ROLE"):
        migrate(pg_url, app_role=name)
    assert scalar(pg_url, "SELECT version_num FROM alembic_version") == "0002"
    assert scalar(pg_url, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'") == 0


def tables_missing_rls_in(url: str) -> list[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return tables_missing_rls(conn)
    finally:
        engine.dispose()


# --- Postgres: the privileged-role guard ---------------------------------------------------


def test_the_migration_refuses_a_superuser_or_bypassrls_app_role(
    pg_url: str, app_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role that skips row-level security would make every policy decorative. The real check
    runs in every other test of this file (the test roles are plain); here the lookup is forced
    to report a privileged role, because the local test server has no superuser to borrow."""
    migrate(pg_url, revision="0002")
    monkeypatch.setattr(
        db_roles,
        "role_privilege_problem",
        lambda _conn, role=None: (
            f"role {role!r} has BYPASSRLS, so row-level security would not apply"
        ),
    )
    with pytest.raises(RuntimeError, match="BYPASSRLS"):
        migrate(pg_url, app_role=app_role)
    assert scalar(pg_url, "SELECT version_num FROM alembic_version") == "0002"
    assert scalar(pg_url, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'") == 0


def test_the_connecting_roles_of_the_tests_are_plain(pg_url: str, app_role: str) -> None:
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            assert role_privilege_problem(conn) is None  # the owner (current_user)
            assert role_privilege_problem(conn, app_role) is None
            assert role_privilege_problem(conn, "no_such_role_at_all") is not None
    finally:
        engine.dispose()


def test_apply_grants_is_idempotent_and_repairs_a_role_created_late(
    pg_url: str, app_role: str
) -> None:
    migrate(pg_url, app_role=None)  # single-role: the app role exists but got nothing
    for table in EXPECTED_TABLE_PRIVILEGES:
        assert privileges(pg_url, app_role, table) == set(), table
    engine = create_engine(pg_url)
    try:
        for _ in range(2):  # twice: the second run changes nothing and does not fail
            with engine.begin() as conn:
                apply_grants(conn, app_role)
            for table, expected in EXPECTED_TABLE_PRIVILEGES.items():
                assert privileges(pg_url, app_role, table) == expected, table
        with engine.begin() as conn:
            for statement in revoke_statements(app_role, "public"):
                conn.execute(text(statement))
        for table in EXPECTED_TABLE_PRIVILEGES:
            assert privileges(pg_url, app_role, table) == set(), table
    finally:
        engine.dispose()


def test_apply_grants_refuses_a_role_that_does_not_exist(pg_url: str) -> None:
    migrate(pg_url, app_role=None)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as conn, pytest.raises(RuntimeError, match="does not exist"):
            apply_grants(conn, "no_such_role")
    finally:
        engine.dispose()


# --- Postgres: the grants ------------------------------------------------------------


def test_the_app_role_gets_exactly_the_documented_privileges(
    rls_db: RlsDatabase, app_role: str
) -> None:
    if rls_db.mode != "app-role":
        pytest.skip("grants only exist when a separate app role is configured")
    url = rls_db.owner_url
    for table, expected in EXPECTED_TABLE_PRIVILEGES.items():
        assert privileges(url, app_role, table) == expected, table
    # No TRUNCATE, REFERENCES or TRIGGER anywhere, and no DELETE on tickets or uploads.
    assert "DELETE" not in privileges(url, app_role, "tickets")
    assert "DELETE" not in privileges(url, app_role, "uploads")
    assert scalar(url, "SELECT has_schema_privilege(:r, 'public', 'USAGE')", r=app_role) is True
    assert scalar(url, "SELECT has_schema_privilege(:r, 'public', 'CREATE')", r=app_role) is False
    for seq in ("agencies_id_seq", "tickets_id_seq", "audit_events_id_seq"):
        for priv, wanted in (("USAGE", True), ("SELECT", True), ("UPDATE", False)):
            got = scalar(
                url, "SELECT has_sequence_privilege(:r, :s, :p)", r=app_role, s=seq, p=priv
            )
            assert got is wanted, (seq, priv)


# --- Postgres: downgrade reverses everything -----------------------------------------


def test_downgrade_removes_policies_function_and_grants(rls_db: RlsDatabase, app_role: str) -> None:
    url = rls_db.owner_url
    role = app_role if rls_db.mode == "app-role" else None
    cfg = alembic_config(url)
    with app_role_env(role):
        command.downgrade(cfg, "0002")
    assert scalar(url, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'") == 0
    assert (
        scalar(
            url,
            "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace "
            "AND (relrowsecurity OR relforcerowsecurity)",
        )
        == 0
    )
    assert (
        scalar(
            url,
            "SELECT count(*) FROM pg_proc WHERE proname = 'app_current_agency_id' "
            "AND pronamespace = 'public'::regnamespace",
        )
        == 0
    )
    assert "display_name" not in user_columns(url)
    for table in EXPECTED_TABLE_PRIVILEGES:
        assert privileges(url, app_role, table) == set(), table
    assert scalar(url, "SELECT has_schema_privilege(:r, 'public', 'USAGE')", r=app_role) is False
    # ...and it comes back.
    with app_role_env(role):
        command.upgrade(cfg, "head")
    assert tables_missing_rls_in(url) == []
    if role:
        assert privileges(url, app_role, "tickets") == READ_WRITE


def test_a_full_cycle_works_in_both_modes(rls_db: RlsDatabase, app_role: str) -> None:
    role = app_role if rls_db.mode == "app-role" else None
    cfg = alembic_config(rls_db.owner_url)
    with app_role_env(role):
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    assert tables_missing_rls_in(rls_db.owner_url) == []


def test_downgrade_without_the_role_still_works_and_warns(
    rls_db: RlsDatabase, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = alembic_config(rls_db.owner_url)
    with app_role_env(None), caplog.at_level(logging.WARNING):
        command.downgrade(cfg, "0002")
    assert (
        scalar(rls_db.owner_url, "SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        == 0
    )
    assert any("APP_DB_ROLE" in r.getMessage() for r in caplog.records)
