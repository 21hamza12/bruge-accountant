"""The test database is SQLite by default and the server named by $TEST_DATABASE_URL otherwise."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import select_test_database_url


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
