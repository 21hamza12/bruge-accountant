"""Runtime configuration, read from environment variables (never from code or the repo)."""

from __future__ import annotations

import json
import os
import secrets
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, PrivateAttr, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.ai.pricing import PRICES

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


# What a parse child gets from the environment: what the interpreter needs to start, nothing else.
_CHILD_ENV_WINDOWS = ("SYSTEMROOT", "SYSTEMDRIVE", "PATH")
_CHILD_ENV_POSIX = ("PATH", "LANG", "LC_ALL")


def child_environment() -> dict[str, str]:
    """The environment of the parse sandbox's child process (``app.parsing.sandbox``).

    An allowlist, built from scratch: ``PATH`` and ``SYSTEMROOT``/``SYSTEMDRIVE`` on Windows,
    ``PATH``/``LANG``/``LC_ALL`` elsewhere, plus ``PYTHON*`` isolation variables. Everything else
    (``DATABASE_URL``, the owner database URL, ``SENTRY_DSN``, any ``TEST_*``, API keys) is
    simply not there. It lives here because this module is the only one that reads the
    environment (a code guard checks that).
    """
    env: dict[str, str] = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    windows = os.name == "nt"
    for key in _CHILD_ENV_WINDOWS if windows else _CHILD_ENV_POSIX:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if not windows:
        env.setdefault("LANG", "C.UTF-8")
    return env


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
    # At least 5 s: interpreter start-up plus openpyxl import already take about a second.
    parse_timeout_s: int = Field(default=30, ge=5, le=300)
    parse_memory_mb: int = Field(default=768, ge=128, le=8192)
    # Parses that may run at once in this process, and how long one that finds every slot taken
    # waits before the 503. Each child needs about 31-49 MB resident (docs/phase2-plan.md), so on
    # a 512 MB instance 2 children fit beside the web process; raise it only with more memory.
    parse_concurrency: int = Field(default=2, ge=1, le=8)
    parse_queue_wait_s: float = Field(default=5.0, ge=0, le=60)
    # Number of trusted reverse proxies in front of the app (Render: see the runbook). 0 means
    # X-Forwarded-* is ignored entirely, which is the safe default; render.yaml sets 1.
    trusted_proxy_hops: int = Field(default=0, ge=0, le=10)
    sentry_dsn: SecretStr | None = None
    # AI (phase D, docs/design/11-screenshots.md). Without a key the feature is hidden. AI_ENABLED
    # unset means "on when there is a key"; true without a key is a mistake, refused in production.
    anthropic_api_key: SecretStr | None = None
    ai_enabled: bool | None = None
    ai_screenshot_model: str = "claude-haiku-5-5"
    ai_fallback_model: str = "claude-sonnet-5-5"
    # Default monthly budget of an agency in USD (its own limit: ``python -m app ai-budget``).
    ai_monthly_budget_usd: Decimal = Field(default=Decimal(5), ge=0, le=100000)
    ai_timeout_s: float = Field(default=30.0, ge=1, le=120)  # one API call
    ai_max_retries: int = Field(default=1, ge=0, le=4)  # on 429 / 5xx / network errors only
    # The chat (phase E part 2, docs/design/13-chat-ai.md): the cheap model first, the stronger one
    # when the first could not resolve the turn (empty disables the second). The chat stops at this
    # share of the month's budget; the rest is kept for screenshots.
    ai_chat_enabled: bool = True
    ai_chat_model: str = "claude-haiku-5-5"
    ai_chat_fallback_model: str = "claude-sonnet-5-5"
    ai_chat_budget_share: Decimal = Field(default=Decimal("0.8"), ge=Decimal("0.1"), le=1)
    ai_chat_timeout_s: float = Field(default=20.0, ge=1, le=120)  # one chat API call
    # No retry inside the SDK (a retry doubles the cost and the wait of a call that is already
    # slow; the turn deadline and the stronger model are the chat's own second chance).
    ai_chat_max_retries: int = Field(default=0, ge=0, le=4)
    ai_chat_turn_deadline_s: float = Field(default=45.0, ge=5, le=300)  # a whole turn
    # Calls to the stronger model (rows ``fallback`` of purpose ``chat``) an agency may cost in one
    # UTC day; 0 never escalates.
    ai_chat_fallback_daily_cap: int = Field(default=20, ge=0, le=100000)
    # Variables the Anthropic SDK reads by itself. They are fields only so that production can
    # refuse them: a base URL would send the key (and every question) to another host, an auth
    # token replaces the key, and ANTHROPIC_LOG=debug makes the SDK log request bodies.
    anthropic_base_url: str | None = None
    anthropic_auth_token: SecretStr | None = None
    anthropic_log: str | None = None
    render_git_commit: str | None = None
    # Render sets RENDER=true on its services. Seeing it outside production means a deploy
    # forgot ENV=production, so refuse to start rather than run with development defaults.
    render: str | None = None
    # Secret that signs the issue dates the app proposes (app/issue_sig.py). Unset: a random key
    # per process (older forms then lose their dates after a restart; nothing forged is accepted).
    issue_signing_key: SecretStr | None = None
    _ephemeral_key: bytes = PrivateAttr(default_factory=lambda: secrets.token_bytes(32))

    def issue_key(self) -> bytes:
        configured = self.issue_signing_key
        text = configured.get_secret_value().strip() if configured is not None else ""
        return text.encode() if text else self._ephemeral_key

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
        if self.ai_enabled and not self.has_ai_key and self.env == "production":
            raise ValueError("AI_ENABLED is true but ANTHROPIC_API_KEY is not set")
        self._check_models()
        if self.env == "production":
            for name in ("anthropic_base_url", "anthropic_auth_token", "anthropic_log"):
                if getattr(self, name) is not None:
                    raise ValueError(
                        f"{name.upper()} must not be set in production "
                        "(the SDK would read it: another host, another credential or debug logs)"
                    )
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

    def _check_models(self) -> None:
        """Every model named must be in the price table: a typo would be priced at the dearest
        entry and fail on the first call instead of at startup. An empty second model is allowed
        (it switches the second try off)."""
        for name, optional in (
            ("ai_chat_model", False),
            ("ai_chat_fallback_model", True),
            ("ai_screenshot_model", False),
            ("ai_fallback_model", True),
        ):
            model = getattr(self, name)
            if model in PRICES or (optional and model == ""):
                continue
            raise ValueError(f"{name.upper()} is not a model in the price table: {model!r}")

    @property
    def has_ai_key(self) -> bool:
        key = self.anthropic_api_key
        return key is not None and bool(key.get_secret_value().strip())

    @property
    def ai_active(self) -> bool:
        """The AI features exist at all: a key, and not switched off with AI_ENABLED=false."""
        return self.has_ai_key and self.ai_enabled is not False

    @property
    def chat_ai_active(self) -> bool:
        """The chat may ask the model: AI is active and the chat's own switch is on."""
        return self.ai_active and self.ai_chat_enabled

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def version(self) -> str:
        return self.render_git_commit or "dev"
