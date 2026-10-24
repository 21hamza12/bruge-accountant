from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.db import Base, make_engine, make_session_factory
from app.db_roles import ROLE_ENV_VAR
from app.parsing.amadeus import ParsedReport, parse_report
from app.settings import Settings
from app.web.main import create_app
from tests.web_support import WebDb

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
SHA = "a" * 64
ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"
TEST_APP_DATABASE_URL_ENV = "TEST_APP_DATABASE_URL"
# Every test wipes and rebuilds the schema, so two test runs on one server would corrupt each
# other. Each test takes this (session-level) advisory lock first; the second run waits its turn.
ADVISORY_LOCK_KEY = 7_283_200_017
LOCK_WAIT_SECONDS = 900.0
LOCK_POLL_SECONDS = 0.5


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


def select_app_database_url(owner_url: str, app_url: str | None) -> str:
    """The URL the application connects with: the owner's on SQLite, else $TEST_APP_DATABASE_URL.

    On Postgres the app must be a different (non-owner) role on the same database as the owner,
    otherwise row-level security tests would run as the table owner and prove nothing.
    """
    owner = make_url(owner_url)
    if owner.get_backend_name() != "postgresql":
        return owner_url
    if not app_url:
        raise pytest.UsageError(
            f"{TEST_APP_DATABASE_URL_ENV} must be set when {TEST_DATABASE_URL_ENV} is Postgres: "
            "the connection of the non-owner application role (see README)"
        )
    app = make_url(app_url)
    if app.username == owner.username:
        raise pytest.UsageError(
            f"{TEST_APP_DATABASE_URL_ENV} must use a different role than {TEST_DATABASE_URL_ENV}"
        )
    if (app.host, app.port, app.database) != (owner.host, owner.port, owner.database):
        raise pytest.UsageError(
            f"{TEST_APP_DATABASE_URL_ENV} must point at the same database as "
            f"{TEST_DATABASE_URL_ENV}"
        )
    return app_url


@contextmanager
def app_role_env(role: str | None) -> Iterator[None]:
    """Set (or clear) APP_DB_ROLE for the migration, then put the old value back."""
    previous = os.environ.get(ROLE_ENV_VAR)
    if role is None:
        os.environ.pop(ROLE_ENV_VAR, None)
    else:
        os.environ[ROLE_ENV_VAR] = role
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ROLE_ENV_VAR, None)
        else:
            os.environ[ROLE_ENV_VAR] = previous


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def migrate(owner_url: str, app_role: str | None = None, revision: str = "head") -> None:
    """``alembic upgrade`` as the owner; with ``app_role`` the migration also grants to it."""
    with app_role_env(app_role):
        command.upgrade(alembic_config(owner_url), revision)


@contextmanager
def database_lock(url: str) -> Iterator[None]:
    """Hold the test-run advisory lock on the server for the duration of the block."""
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while not conn.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            ).scalar():
                if time.monotonic() >= deadline:
                    raise pytest.UsageError(
                        "another test run has used this database for "
                        f"{LOCK_WAIT_SECONDS:.0f} s; stop it, or wait for it to finish"
                    )
                time.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY})
    finally:
        engine.dispose()


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
def db_url(tmp_path: Path) -> Iterator[str]:
    """An empty database for this test only. On a server, the test also holds the run lock."""
    url = select_test_database_url(tmp_path)
    if url.startswith("sqlite"):
        yield url
        return
    with database_lock(url):
        _reset_server_database(url)
        yield url


@pytest.fixture
def app_db_url(db_url: str) -> str:
    """The URL the application connects with, on a database migrated to head by the owner.

    SQLite: ``db_url`` itself, untouched (tests build the schema themselves there).
    Postgres: ``alembic upgrade head`` as the owner with APP_DB_ROLE set, so migration 0003
    grants the app role its privileges; then the app role's URL.
    """
    url = select_app_database_url(db_url, os.environ.get(TEST_APP_DATABASE_URL_ENV))
    if url == db_url:
        return url
    migrate(db_url, app_role=make_url(url).username)
    return url


@dataclass(frozen=True)
class RlsDatabase:
    """A Postgres database migrated to head, in one of the two deployment modes.

    ``app-role``: the application connects as a separate non-owner role (grants applied).
    ``single-role``: no second role exists; the application connects as the owner and only
    FORCE ROW LEVEL SECURITY protects the tenants from each other.
    """

    mode: str
    owner_url: str
    app_url: str


def assert_subject_to_rls(url: str) -> None:
    """A superuser or BYPASSRLS role skips every policy, which would make an RLS test vacuous."""
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
    finally:
        engine.dispose()
    if row.rolsuper or row.rolbypassrls:
        raise pytest.UsageError(
            f"role {row.rolname!r} is a superuser or has BYPASSRLS, so row-level security would "
            "not apply to it; connect the tests as a plain role (see README)"
        )


@pytest.fixture(params=["app-role", "single-role"])
def rls_db(request: pytest.FixtureRequest, db_url: str) -> RlsDatabase:
    """Every RLS test runs twice: with a separate app role, and in single-role mode."""
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    mode: str = request.param
    assert_subject_to_rls(db_url)
    if mode == "single-role":
        migrate(db_url, app_role=None)
        return RlsDatabase(mode, owner_url=db_url, app_url=db_url)
    app_url = select_app_database_url(db_url, os.environ.get(TEST_APP_DATABASE_URL_ENV))
    assert_subject_to_rls(app_url)
    migrate(db_url, app_role=make_url(app_url).username)
    return RlsDatabase(mode, owner_url=db_url, app_url=app_url)


@pytest.fixture(params=["default", "single-role"])
def web_db(request: pytest.FixtureRequest, db_url: str) -> WebDb:
    """A migrated database for the web tests, so every test runs under row-level security.

    SQLite: one run, schema built from the models. Postgres: ``default`` is the separate
    non-owner application role, ``single-role`` the owner alone (FORCE ROW LEVEL SECURITY only).
    """
    mode: str = request.param
    if make_url(db_url).get_backend_name() != "postgresql":
        if mode != "default":
            pytest.skip("single-role mode needs Postgres")
        engine = make_engine(db_url)
        Base.metadata.create_all(engine)
        engine.dispose()
        return WebDb("sqlite", db_url, db_url)
    assert_subject_to_rls(db_url)
    if mode == "single-role":
        migrate(db_url, app_role=None)
        return WebDb(mode, db_url, db_url)
    app_url = select_app_database_url(db_url, os.environ.get(TEST_APP_DATABASE_URL_ENV))
    assert_subject_to_rls(app_url)
    migrate(db_url, app_role=make_url(app_url).username)
    return WebDb("app-role", db_url, app_url)


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


@pytest.fixture
def web_settings(db_url: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Development settings pointing at this test's database, with a clean environment."""
    for name in ("ENV", "ALLOWED_HOSTS", "CORS_ALLOWED_ORIGINS", "COOKIE_SECURE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    return Settings(database_url=SecretStr(db_url))


@pytest.fixture
def client(web_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(web_settings), base_url="https://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def no_sdk_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic SDK reads these by itself, and a production ``Settings`` refuses them: a
    developer's shell (or a coding agent's) may have them set, and no test depends on them."""
    for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_LOG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """create_app() configures logging; undo it so tests stay independent."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    uvicorn_loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access")]
    saved = [(lg, list(lg.handlers), lg.propagate, lg.disabled) for lg in uvicorn_loggers]
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for lg, lg_handlers, propagate, disabled in saved:
        lg.handlers[:] = lg_handlers
        lg.propagate = propagate
        lg.disabled = disabled


@pytest.fixture
def make_app() -> Iterator[Callable[..., FastAPI]]:
    """create_app() whose engines are disposed at teardown, even without a lifespan run."""
    built: list[FastAPI] = []

    def factory(settings: Settings | None = None, **kwargs: object) -> FastAPI:
        app = create_app(settings, **kwargs)  # type: ignore[arg-type]
        built.append(app)
        return app

    yield factory
    for app in built:
        app.state.engine.dispose()


@pytest.fixture
def gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt in: the close gate (B9) says "nothing blocks". For tests about closing, reopening or
    exporting a day that has no Amadeus report; the gate itself is tested for real in
    tests/test_close_gate.py and tests/test_approval_pages.py."""
    from app.approvals import gate

    monkeypatch.setattr(gate, "can_close", create_autospec(gate.can_close, return_value=[]))
