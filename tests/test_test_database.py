"""The test database is SQLite by default and the server named by $TEST_DATABASE_URL otherwise."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from tests import conftest
from tests.conftest import ADVISORY_LOCK_KEY, select_test_database_url


def test_defaults_to_a_temporary_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    assert select_test_database_url(tmp_path) == f"sqlite:///{tmp_path / 'test.db'}"


def test_empty_variable_means_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_DATABASE_URL", "")
    assert select_test_database_url(tmp_path).startswith("sqlite:///")


def test_uses_the_configured_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://ci@localhost:5432/bruge_test"
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    assert select_test_database_url(tmp_path) == url


@pytest.mark.parametrize(
    "url",
    ["postgresql+psycopg://ci@localhost/bruge", "postgresql+psycopg://ci@localhost/"],
)
def test_refuses_a_database_not_named_for_tests(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every test wipes the schema, so never point the suite at a real database by mistake.
    monkeypatch.setenv("TEST_DATABASE_URL", url)
    with pytest.raises(pytest.UsageError, match="test"):
        select_test_database_url(tmp_path)


# --- two runs on the same server take turns ---------------------------------------------


def test_the_db_url_fixture_holds_an_advisory_lock_for_the_whole_test(db_url: str) -> None:
    if db_url.startswith("sqlite"):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as other_run:
            got = other_run.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            ).scalar()
            assert got is False, "a second test run could have started while this test runs"
    finally:
        engine.dispose()


def test_the_lock_is_released_when_the_test_is_over(db_url: str) -> None:
    if db_url.startswith("sqlite"):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    # The previous test's lock is gone, so this test (which holds it now) is the only holder.
    engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            holders = conn.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND ((classid::bigint << 32) | objid::bigint) = :k"
                ),
                {"k": ADVISORY_LOCK_KEY},
            ).scalar()
    finally:
        engine.dispose()
    assert holders == 1


def test_waiting_for_the_lock_gives_up_with_a_clear_message(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if db_url.startswith("sqlite"):
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    monkeypatch.setattr(conftest, "LOCK_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(conftest, "LOCK_POLL_SECONDS", 0.05)
    with pytest.raises(pytest.UsageError, match="another test run"), conftest.database_lock(db_url):
        pass  # this test already holds the lock through db_url
