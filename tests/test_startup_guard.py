"""The web app refuses to start in production as a superuser or BYPASSRLS role.

Such a role skips every row-level security policy, so the isolation would only look present. The
check reads ``current_user`` once at startup. The real detection runs on the test server's
plain roles; the privileged case is simulated, because the local server offers no superuser to
connect as.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from app import db_roles
from app.db_roles import PrivilegedRoleError
from app.settings import Settings

MakeApp = Callable[..., FastAPI]
PROBLEM = "role 'web' is a superuser, so row-level security would not apply to it"


def production(url: str) -> Settings:
    return Settings(
        env="production",
        database_url=SecretStr(url),
        allowed_hosts=["testserver"],
        cookie_secure=True,
    )


@pytest.fixture
def privileged(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(_connection: object, role: str | None = None) -> str:
        seen.append("current_user" if role is None else role)
        return PROBLEM

    monkeypatch.setattr(db_roles, "role_privilege_problem", fake)
    return seen


@pytest.fixture
def pg_url(db_url: str) -> str:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    return db_url


def test_production_refuses_to_start_as_a_privileged_role(
    pg_url: str, make_app: MakeApp, privileged: list[str]
) -> None:
    app = make_app(production(pg_url))
    with pytest.raises(PrivilegedRoleError) as info, TestClient(app):
        pass
    message = str(info.value)
    assert "superuser" in message and "BYPASSRLS" in message and "DATABASE_URL" in message
    assert "web" in message
    assert privileged == ["current_user"]  # it asked about the connecting role


def test_the_message_never_contains_the_connection_url(
    pg_url: str, make_app: MakeApp, privileged: list[str]
) -> None:
    password = make_url(pg_url).password
    app = make_app(production(pg_url))
    with pytest.raises(PrivilegedRoleError) as info, TestClient(app):
        pass
    assert pg_url not in str(info.value)
    assert not password or password not in str(info.value)


def test_production_starts_as_a_plain_role(pg_url: str, make_app: MakeApp) -> None:
    app = make_app(production(pg_url))
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/healthz").status_code == 200


def test_development_only_warns(
    pg_url: str,
    make_app: MakeApp,
    privileged: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(database_url=SecretStr(pg_url))
    with TestClient(make_app(settings), base_url="https://testserver") as client:
        assert client.get("/healthz").status_code == 200
    assert any("superuser" in r.getMessage() for r in caplog.records)


def test_sqlite_is_never_checked(
    db_url: str, make_app: MakeApp, privileged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite only")
    settings = Settings(database_url=SecretStr(db_url))
    with TestClient(make_app(settings), base_url="https://testserver") as client:
        assert client.get("/healthz").status_code == 200
    assert privileged == []
