"""Fail-closed settings, exercised through the real environment (``Settings()``), not only
``model_validate``, so a wrong env var name or a parsing bug cannot hide."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.cli import main
from app.settings import DEV_ALLOWED_HOSTS, Settings

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PG_URL = "postgresql://app:pw@db.internal/bruge"

ENV_NAMES = (
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
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def prod_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    base = {"ENV": "production", "ALLOWED_HOSTS": "app.example.com", "DATABASE_URL": PG_URL}
    for key, value in {**base, **env}.items():
        monkeypatch.setenv(key, value)


def test_production_accepts_a_sane_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_env(monkeypatch, CORS_ALLOWED_ORIGINS="https://app.example.com")
    s = Settings()
    assert s.is_production
    assert s.cookie_secure is True


def test_production_requires_database_url_to_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_env(monkeypatch)
    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


def test_production_rejects_the_implicit_sqlite_default_via_validate() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings.model_validate({"env": "production", "allowed_hosts": "app.example.com"})


@pytest.mark.parametrize("blank", ["", "   "])
def test_production_rejects_a_blank_database_url(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    prod_env(monkeypatch, DATABASE_URL=blank)
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings()


@pytest.mark.parametrize("url", ["sqlite:///bruge.db", "SQLite:///:memory:", "sqlite://"])
def test_production_rejects_sqlite(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    prod_env(monkeypatch, DATABASE_URL=url)
    with pytest.raises(ValidationError, match="SQLite"):
        Settings()


def test_production_rejects_insecure_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_env(monkeypatch, COOKIE_SECURE="false")
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings()


@pytest.mark.parametrize("origins", ["*", "https://a.example.com,*"])
@pytest.mark.parametrize("env", ["development", "production"])
def test_wildcard_cors_is_rejected_in_every_environment(
    monkeypatch: pytest.MonkeyPatch, env: str, origins: str
) -> None:
    # allow_credentials is on, so a wildcard origin is never acceptable.
    prod_env(monkeypatch, ENV=env, CORS_ALLOWED_ORIGINS=origins)
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        Settings()


@pytest.mark.parametrize(
    "origins",
    [
        "http://app.example.com",
        "null",
        "NULL",
        "HTTP://x.example.com",
        "https://ok.example.com,null",
    ],
)
def test_production_rejects_http_and_null_origins(
    monkeypatch: pytest.MonkeyPatch, origins: str
) -> None:
    prod_env(monkeypatch, CORS_ALLOWED_ORIGINS=origins)
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        Settings()


@pytest.mark.parametrize("hosts", ["*", "*.example.com", "ok.example.com,*.example.com", "a*.x"])
def test_production_rejects_wildcard_hosts(monkeypatch: pytest.MonkeyPatch, hosts: str) -> None:
    prod_env(monkeypatch, ALLOWED_HOSTS=hosts)
    with pytest.raises(ValidationError, match="ALLOWED_HOSTS"):
        Settings()


def test_development_still_accepts_http_origins_wildcard_hosts_and_insecure_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("ALLOWED_HOSTS", "*.localhost")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    s = Settings()
    assert s.cors_allowed_origins == ["http://localhost:3000"]
    assert s.allowed_hosts == ["*.localhost"]
    assert s.cookie_secure is False


def test_render_without_production_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(ValidationError, match="RENDER"):
        Settings()
    monkeypatch.setenv("ENV", "development")
    with pytest.raises(ValidationError, match="RENDER"):
        Settings()


def test_render_with_production_is_fine_and_an_empty_render_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prod_env(monkeypatch, RENDER="true")
    assert Settings().is_production
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("RENDER", "")
    assert Settings().env == "development"


def test_validation_errors_never_echo_the_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_env(monkeypatch, DATABASE_URL="sqlite:///hunter2-secret.db", COOKIE_SECURE="false")
    with pytest.raises(ValidationError) as info:
        Settings()
    assert "hunter2-secret" not in str(info.value)
    assert "input_value" not in str(info.value)


# --- list variables: CSV or a JSON array, never a mangled single entry --------------------------


def test_lists_accept_a_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", '["a.example.com", "b.example.com"]')
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://a.example.com"]')
    s = Settings()
    assert s.allowed_hosts == ["a.example.com", "b.example.com"]
    assert s.cors_allowed_origins == ["https://a.example.com"]


def test_json_array_wildcard_is_still_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["*"]')
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("bad", ["[not json", '["a",', "[1, 2]", '[["a"]]', '{"a": "b"}'])
def test_malformed_list_values_are_rejected(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", bad)
    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        Settings()


def test_empty_list_values_mean_no_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "  ")
    monkeypatch.setenv("ALLOWED_HOSTS", "[]")
    s = Settings()
    assert s.cors_allowed_origins == []
    assert s.allowed_hosts == list(DEV_ALLOWED_HOSTS)


# --- only Settings reads the environment ------------------------------------------------------


def test_only_settings_reads_the_environment() -> None:
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in APP_DIR.rglob("*.py")
        if path.name != "settings.py"
        and re.search(r"os\.environ|os\.getenv|\bgetenv\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_web_process_never_references_the_migration_url() -> None:
    for path in (APP_DIR / "web").rglob("*.py"):
        assert "migration_database_url" not in path.read_text(encoding="utf-8").lower(), path


def test_create_app_uses_database_url_even_when_the_owner_url_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.web.main import create_app

    app_db, owner_db = tmp_path / "app.db", tmp_path / "owner.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{app_db}")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", f"sqlite:///{owner_db}")
    built = create_app()
    try:
        assert built.state.engine.url.database == str(app_db)
    finally:
        built.state.engine.dispose()


def test_cli_reports_invalid_configuration_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("ALLOWED_HOSTS", "app.example.com")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///leaky-secret.db")
    assert main(["add-agency", "x", "X"]) != 0
    err = capsys.readouterr().err
    assert "configuration" in err.lower()
    assert "leaky-secret" not in err
