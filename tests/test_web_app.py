from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.settings import Settings
from app.web.main import app, create_app


def test_module_level_app_exists() -> None:
    assert app.title


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "dev"}


def test_healthz_reports_git_commit(web_settings: Settings) -> None:
    settings = web_settings.model_copy(update={"render_git_commit": "deadbeef"})
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        assert c.get("/healthz").json() == {"status": "ok", "version": "deadbeef"}


def test_healthz_503_without_details_when_database_is_down(
    web_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> None:
    from pydantic import SecretStr

    missing = tmp_path_factory.mktemp("x") / "no" / "such" / "dir" / "x.db"
    settings = web_settings.model_copy(update={"database_url": SecretStr(f"sqlite:///{missing}")})
    with TestClient(create_app(settings), base_url="https://testserver") as c:
        resp = c.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "error"}
    assert "sqlite" not in resp.text.lower()


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_disabled(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_all_endpoints_are_sync(client: TestClient) -> None:
    routes = [r for r in client.app.routes if isinstance(r, APIRoute)]  # type: ignore[attr-defined]
    assert routes
    assert not [r.path for r in routes if inspect.iscoroutinefunction(r.endpoint)]


def test_engine_and_sessionmaker_live_on_app_state(web_settings: Settings) -> None:
    built = create_app(web_settings)
    assert built.state.engine is not None
    assert built.state.sessionmaker is not None
