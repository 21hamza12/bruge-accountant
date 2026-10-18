"""Two-role Postgres setup: the owner migrates, ``bruge_app`` (non-owner, no BYPASSRLS) serves.

The Postgres tests are skipped without TEST_DATABASE_URL and TEST_APP_DATABASE_URL (they run in
CI); the URL selection logic is checked everywhere.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import select_app_database_url

OWNER = "postgresql+psycopg://bruge:owner-pw@localhost:5432/bruge_test"


def test_sqlite_app_url_is_the_owner_url() -> None:
    assert select_app_database_url("sqlite:///x.db", None) == "sqlite:///x.db"
    assert select_app_database_url("sqlite:///x.db", "postgresql+psycopg://a@h/d") == (
        "sqlite:///x.db"
    )


def test_returns_the_configured_app_url() -> None:
    app = "postgresql+psycopg://bruge_app:pw@localhost:5432/bruge_test"
    assert select_app_database_url(OWNER, app) == app


@pytest.mark.parametrize("value", [None, ""])
def test_postgres_without_an_app_url_is_a_usage_error(value: str | None) -> None:
    with pytest.raises(pytest.UsageError, match="TEST_APP_DATABASE_URL"):
        select_app_database_url(OWNER, value)


def test_app_url_must_not_be_the_owner_role() -> None:
    with pytest.raises(pytest.UsageError, match="different role"):
        select_app_database_url(OWNER, OWNER)


def test_app_url_must_name_the_same_database() -> None:
    other = "postgresql+psycopg://bruge_app:pw@localhost:5432/other_test"
    with pytest.raises(pytest.UsageError, match="same database"):
        select_app_database_url(OWNER, other)


def test_app_db_url_on_sqlite_equals_db_url(db_url: str, app_db_url: str) -> None:
    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite only")
    assert app_db_url == db_url


def test_app_role_is_not_owner_and_cannot_bypass_rls(db_url: str, app_db_url: str) -> None:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    engine = create_engine(app_db_url)
    with engine.connect() as conn:
        role = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
        owners = set(
            conn.execute(text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public'"))
            .scalars()
            .all()
        )
        # It can use the tables it was granted (and the migrations did run).
        assert conn.execute(text("SELECT count(*) FROM agencies")).scalar_one() == 0
    engine.dispose()
    assert role.rolsuper is False
    assert role.rolbypassrls is False
    assert owners
    assert role.rolname not in owners


def test_app_role_cannot_create_tables(db_url: str, app_db_url: str) -> None:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    engine = create_engine(app_db_url)
    with engine.connect() as conn, pytest.raises(Exception, match="permission denied"):
        conn.execute(text("CREATE TABLE intruder (id int)"))
    engine.dispose()
