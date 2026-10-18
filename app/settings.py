"""Runtime configuration, read from environment variables (never from code or the repo)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEV_ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    env: Literal["development", "production"] = "development"
    database_url: SecretStr = SecretStr("sqlite:///bruge.db")
    # Owner role used by `init-db` (preDeployCommand); the app itself uses database_url.
    migration_database_url: SecretStr | None = None
    allowed_hosts: CsvList = Field(default_factory=list)
    cors_allowed_origins: CsvList = Field(default_factory=list)
    cookie_secure: bool = True
    # Number of trusted reverse proxies in front of the app (Render: see the runbook). 0 means
    # X-Forwarded-* is ignored entirely, which is the safe default; render.yaml sets 1.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=10)
    sentry_dsn: SecretStr | None = None
    render_git_commit: str | None = None

    @field_validator("allowed_hosts", "cors_allowed_origins", mode="before")
    @classmethod
    def _csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @model_validator(mode="after")
    def _check_environment(self) -> Settings:
        if self.env != "production":
            if not self.allowed_hosts:
                self.allowed_hosts = list(DEV_ALLOWED_HOSTS)
            return self
        if not self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must list the public host names in production")
        if "*" in self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must not contain '*' in production")
        if "*" in self.cors_allowed_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*' in production")
        if not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def version(self) -> str:
        return self.render_git_commit or "dev"
