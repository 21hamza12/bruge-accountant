from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from app.cli import main
from app.db import make_engine, normalize_database_url
from app.settings import Settings


def make(**env: str) -> Settings:
    return Settings.model_validate(env)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ENV",
        "DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        "ALLOWED_HOSTS",
        "CORS_ALLOWED_ORIGINS",
        "COOKIE_SECURE",
        "TRUSTED_PROXY_HOPS",
        "SENTRY_DSN",
        "RENDER_GIT_COMMIT",
        "RENDER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_development_defaults() -> None:
    s = make()
    assert s.env == "development"
    assert s.database_url.get_secret_value() == "sqlite:///bruge.db"
    assert s.cors_allowed_origins == []
    assert "localhost" in s.allowed_hosts
    assert s.trusted_proxy_hops == 0  # fail safe: trust no proxy unless told
    assert s.is_production is False
    assert s.version == "dev"


def test_reads_environment_and_splits_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", "a.example.com, b.example.com ,")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://a.example.com")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    s = Settings()
    assert s.allowed_hosts == ["a.example.com", "b.example.com"]
    assert s.cors_allowed_origins == ["https://a.example.com"]
    assert s.version == "abc123"


def production(**overrides: str) -> Settings:
    values = {
        "env": "production",
        "allowed_hosts": "app.example.com",
        "database_url": "postgresql://app:pw@db.internal/bruge",
        **overrides,
    }
    return make(**values)


def test_production_accepts_a_sane_configuration() -> None:
    s = production(cors_allowed_origins="https://app.example.com")
    assert s.is_production
    assert s.allowed_hosts == ["app.example.com"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"cors_allowed_origins": "*"},
        {"cors_allowed_origins": "https://ok.example.com,*"},
        {"cookie_secure": "false"},
        {"allowed_hosts": ""},
        {"allowed_hosts": "*"},
        {"allowed_hosts": "*.example.com,*"},
    ],
)
def test_production_rejects_unsafe_values(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        production(**overrides)


def test_production_requires_hosts_even_when_unset() -> None:
    with pytest.raises(ValidationError):
        make(env="production")


def test_development_allows_insecure_cookies_but_never_wildcard_cors() -> None:
    assert make(cookie_secure="false").cookie_secure is False
    with pytest.raises(ValidationError):
        make(cors_allowed_origins="*")


def test_unknown_env_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make(env="staging")


@pytest.mark.parametrize("hops", ["-1", "11", "x"])
def test_trusted_proxy_hops_bounds(hops: str) -> None:
    with pytest.raises(ValidationError):
        make(trusted_proxy_hops=hops)


def test_secrets_do_not_leak_via_repr() -> None:
    s = make(
        database_url="postgresql://user:hunter2@db/x",
        migration_database_url="postgresql://owner:swordfish@db/x",
        sentry_dsn="https://key123@o1.ingest.sentry.io/1",
    )
    shown = repr(s) + str(s) + str(s.model_dump())
    for secret in ("hunter2", "swordfish", "key123"):
        assert secret not in shown


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_normalize_database_url(given: str, expected: str) -> None:
    assert normalize_database_url(given) == expected


def test_make_engine_uses_psycopg_and_pre_ping_for_postgres() -> None:
    engine = make_engine("postgres://u:p@localhost/db")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.dialect.driver == "psycopg"
        assert engine.pool._pre_ping is True
    finally:
        engine.dispose()


def test_make_engine_sqlite_unchanged(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'x.db'}")
    try:
        assert engine.dialect.name == "sqlite"
    finally:
        engine.dispose()


def _tables(path: Path) -> set[str]:
    engine = make_engine(f"sqlite:///{path}")
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_init_db_prefers_migration_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db, owner_db = tmp_path / "app.db", tmp_path / "owner.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{app_db}")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{owner_db}")
    assert main(["init-db"]) == 0
    assert "agencies" in _tables(owner_db)
    assert not app_db.exists() or "agencies" not in _tables(app_db)


def test_init_db_explicit_db_flag_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    flag_db, owner_db = tmp_path / "flag.db", tmp_path / "owner.db"
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{owner_db}")
    assert main(["init-db", "--db", f"sqlite:///{flag_db}"]) == 0
    assert "agencies" in _tables(flag_db)
    assert not owner_db.exists()


def test_other_commands_ignore_migration_database_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_db, owner_db = tmp_path / "app.db", tmp_path / "owner.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{app_db}")
    assert main(["init-db"]) == 0
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{owner_db}")
    assert main(["add-agency", "x-agency", "X"]) == 0
    assert not owner_db.exists()
