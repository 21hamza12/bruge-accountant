"""Runtime configuration, read from environment variables (never from code or the repo)."""

from __future__ import annotations

import json
import os
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]


def _parse_list(value: Any, name: str) -> Any:
    """``a,b`` or a JSON array of strings (``["a","b"]``); anything else is an error.

    Without this a JSON array in the dashboard would become one bogus entry.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith(("[", "{")):
        try:
            parsed = json.loads(text)
        except ValueError:
            raise ValueError(f"{name} is not valid JSON; use a,b or a JSON array") from None
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{name} must be a JSON array of strings")
        return [item.strip() for item in parsed if item.strip()]
    return [part.strip() for part in text.split(",") if part.strip()]


CsvList = Annotated[list[str], NoDecode]


def environment_is_production() -> bool:
    """True when ``ENV=production``, read without validating the rest of the configuration.

    For commands that must refuse to run in production before anything else (a production
    configuration that is incomplete would otherwise fail ``Settings()`` first).
    """
    return os.environ.get("ENV", "").strip().lower() == "production"


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a URL with a password in it.
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", case_sensitive=False, hide_input_in_errors=True
    )

    env: Literal["development", "production"] = "development"
    database_url: SecretStr = SecretStr("sqlite:///bruge.db")
    # Owner role used by `init-db` (preDeployCommand); the app itself uses database_url.
    migration_database_url: SecretStr | None = None
    allowed_hosts: CsvList = Field(default_factory=list)
    cors_allowed_origins: CsvList = Field(default_factory=list)
    cookie_secure: bool = True
    # Server-side sessions (docs/phase2-plan.md decision 2): 12 h idle, 7 d absolute.
    session_idle_hours: int = Field(default=12, ge=1, le=24 * 30)
    session_absolute_days: int = Field(default=7, ge=1, le=90)
    # SQLAlchemy connection pool (Postgres). The default 5 + 5 overflow keeps one 512 MB web
    # instance well under Postgres's connection limit; raise it only with a bigger plan.
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    # Largest request body accepted, headers aside: 10 MB upload plus 64 KB of form overhead.
    max_request_bytes: int = Field(default=10 * 1024 * 1024 + 64 * 1024, ge=1024)
    # The parse sandbox (app/parsing/sandbox.py): seconds before the child is killed, and its
    # address-space cap in MB (Linux only). RLIMIT_AS counts virtual memory, so it sits well above
    # the resident size measured in docs/phase2-plan.md.
    parse_timeout_s: int = Field(default=30, ge=1, le=300)
    parse_memory_mb: int = Field(default=768, ge=128, le=8192)
    # Number of trusted reverse proxies in front of the app (Render: see the runbook). 0 means
    # X-Forwarded-* is ignored entirely, which is the safe default; render.yaml sets 1.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=10)
    sentry_dsn: SecretStr | None = None
    render_git_commit: str | None = None
    # Render sets RENDER=true on its services. Seeing it outside production means a deploy
    # forgot ENV=production, so refuse to start rather than run with development defaults.
    render: str | None = None

    @field_validator("allowed_hosts", "cors_allowed_origins", mode="before")
    @classmethod
    def _lists(cls, value: Any, info: ValidationInfo) -> Any:
        return _parse_list(value, (info.field_name or "list").upper())

    @model_validator(mode="after")
    def _check_environment(self) -> Settings:
        if "*" in self.cors_allowed_origins:
            # Cookies are sent cross-origin (allow_credentials), so a wildcard is never safe.
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*'")
        if self.render and self.env != "production":
            raise ValueError("RENDER is set but ENV is not production; set ENV=production")
        if self.env != "production":
            if not self.allowed_hosts:
                self.allowed_hosts = list(DEV_ALLOWED_HOSTS)
            return self
        url = self.database_url.get_secret_value().strip()
        if "database_url" not in self.model_fields_set or not url:
            raise ValueError("DATABASE_URL must be set explicitly in production")
        if url.lower().startswith("sqlite"):
            raise ValueError("DATABASE_URL must not be SQLite in production")
        if not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")
        if not self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must list the public host names in production")
        if any("*" in host for host in self.allowed_hosts):
            raise ValueError("ALLOWED_HOSTS must not contain wildcards in production")
        if any(not origin.lower().startswith("https://") for origin in self.cors_allowed_origins):
            raise ValueError("CORS_ALLOWED_ORIGINS entries must be https:// origins in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def version(self) -> str:
        return self.render_git_commit or "dev"
