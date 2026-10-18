from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db import Base, make_engine, make_session_factory
from app.parsing.amadeus import ParsedReport, parse_report

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
SHA = "a" * 64
ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"


def select_test_database_url(tmp_path: Path) -> str:
    """$TEST_DATABASE_URL when set (e.g. Postgres in CI), else a fresh SQLite file."""
    url = os.environ.get(TEST_DATABASE_URL_ENV)
    if not url:
        return f"sqlite:///{tmp_path / 'test.db'}"
    if "test" not in (make_url(url).database or ""):
        raise pytest.UsageError(
            f"{TEST_DATABASE_URL_ENV} must name a database containing 'test': "
            "every test drops and recreates its schema"
        )
    return url


def _reset_server_database(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


def allow_future_price_statuses(session: Session) -> None:
    """Phase 3 extends the price_status CHECK; simulate a later status in Phase 1 tests."""
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("PRAGMA ignore_check_constraints=ON"))
    else:
        session.execute(text("ALTER TABLE tickets DROP CONSTRAINT ck_tickets_price_status"))
        session.commit()


@pytest.fixture(scope="session")
def fixture_path() -> Path:
    return FIXTURE


@pytest.fixture(scope="session")
def parsed_fixture() -> ParsedReport:
    return parse_report(FIXTURE)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """An empty database for this test only."""
    url = select_test_database_url(tmp_path)
    if not url.startswith("sqlite"):
        _reset_server_database(url)
    return url


@pytest.fixture
def session(db_url: str) -> Iterator[Session]:
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


@pytest.fixture
def alembic_cfg(db_url: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, str]:
    monkeypatch.setenv("DATABASE_URL", db_url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg, db_url
