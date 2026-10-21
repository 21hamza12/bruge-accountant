"""Guards the migration-0003 trap: 0003 must not grant on tables that a later phase adds to GRANTS.

Migration 0003 used to call ``apply_grants`` with no table list, which grants on every table in
``db_roles.GRANTS``. The day a later phase adds a table to GRANTS, a fresh ``alembic upgrade
head`` would fail at 0003 (the table does not exist yet). 0003 now passes
``MIGRATION_0003_TABLES``, the tables it grants today.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import db_roles
from app.db_roles import (
    GRANTS,
    MIGRATION_0003_TABLES,
    apply_grants,
    grant_statements,
    revoke_statements,
)
from tests.conftest import TEST_APP_DATABASE_URL_ENV, migrate

# What 0003 granted before the change: pinned here on purpose, so editing the constant fails.
PINNED = {
    "agencies",
    "alembic_version",
    "uploads",
    "tickets",
    "users",
    "temporary_passwords",
    "staff_sign_links",
    "user_sessions",
    "rate_limit_buckets",
    "audit_events",
}


def test_the_frozen_table_list_is_what_0003_granted_and_is_immutable() -> None:
    assert isinstance(MIGRATION_0003_TABLES, tuple)
    assert set(MIGRATION_0003_TABLES) == PINNED
    assert len(MIGRATION_0003_TABLES) == len(PINNED)
    assert set(GRANTS) >= PINNED


def test_grant_statements_default_to_every_table_and_accept_an_explicit_list() -> None:
    everything = grant_statements("bruge_app", "public")
    assert sum('"public"."agencies"' in s for s in everything) == 1
    only = grant_statements("bruge_app", "public", tables=("agencies",))
    assert any('"public"."agencies"' in s for s in only)
    assert not any('"public"."users"' in s for s in only)
    assert any("ALL SEQUENCES" in s for s in only)  # sequences are still granted


def test_an_unknown_table_in_the_list_is_refused() -> None:
    with pytest.raises(ValueError, match="no_such_table"):
        grant_statements("bruge_app", "public", tables=("no_such_table",))


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> None:
        self.statements.append(str(statement.statement))


def test_apply_grants_with_a_table_list_skips_the_other_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_roles, "assert_grantable", lambda connection, role: None)
    monkeypatch.setattr(db_roles, "_current_schema", lambda connection: "public")
    monkeypatch.setitem(GRANTS, "phase_b_table", "SELECT")
    everything, limited = _Recorder(), _Recorder()
    apply_grants(everything, "bruge_app")  # type: ignore[arg-type]
    apply_grants(limited, "bruge_app", tables=MIGRATION_0003_TABLES)  # type: ignore[arg-type]
    assert any("phase_b_table" in s for s in everything.statements)
    assert not any("phase_b_table" in s for s in limited.statements)
    assert any('"users"' in s for s in limited.statements)


def test_migration_0003_passes_its_frozen_list() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "0003_row_level_security.py"
    ).read_text(encoding="utf-8")
    assert "apply_grants(bind, role, tables=MIGRATION_0003_TABLES)" in source


def test_migrations_fresh_upgrade_head_with_app_role(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table added to GRANTS later must not break a fresh upgrade (Postgres, app role set)."""
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    app_url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    if not app_url:
        pytest.skip("needs TEST_APP_DATABASE_URL")
    role = make_url(app_url).username or ""
    monkeypatch.setitem(GRANTS, "phase_b_future_table", "SELECT, INSERT")  # does not exist yet
    migrate(db_url, app_role=role)
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            can_read = conn.execute(
                text("SELECT has_table_privilege(:r, 'users', 'SELECT')"), {"r": role}
            ).scalar()
    finally:
        engine.dispose()
    assert can_read is True


def test_revoke_statements_default_to_every_table_and_accept_an_explicit_list() -> None:
    everything = "\n".join(revoke_statements("bruge_app", "public"))
    assert "ALL TABLES IN SCHEMA" in everything
    only = revoke_statements("bruge_app", "public", tables=("agencies", "users"))
    joined = "\n".join(only)
    assert "ALL TABLES IN SCHEMA" not in joined
    assert '"public"."agencies"' in joined and '"public"."users"' in joined
    assert '"public"."tickets"' not in joined
    assert any("SEQUENCES" in s for s in only) and any("USAGE ON SCHEMA" in s for s in only)


def test_migration_0003_downgrade_revokes_only_its_own_tables() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent
        / "migrations"
        / "versions"
        / "0003_row_level_security.py"
    ).read_text(encoding="utf-8")
    assert "revoke_grants(bind, role, tables=MIGRATION_0003_TABLES)" in source


def test_migration_0003_upgrade_then_downgrade_with_a_future_grant_table(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Postgres: a table added to GRANTS later does not break 0003 in either direction."""
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    app_url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    if not app_url:
        pytest.skip("needs TEST_APP_DATABASE_URL")
    from alembic import command

    from tests.conftest import alembic_config, app_role_env

    role = make_url(app_url).username or ""
    monkeypatch.setitem(GRANTS, "phase_b_future_table", "SELECT, INSERT")
    migrate(db_url, app_role=role, revision="0003")
    with app_role_env(role):
        command.downgrade(alembic_config(db_url), "0002")
