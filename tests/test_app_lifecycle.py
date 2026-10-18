"""App factory, lifespan, dependencies, middleware order and the error boundary."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send

import app.db as db
from app.db import make_engine
from app.observability import JsonFormatter, RequestLogMiddleware, request_id_var
from app.settings import Settings
from app.web.deps import get_session, get_settings
from app.web.errors import ErrorBoundaryMiddleware
from app.web.proxy import ProxyHeadersMiddleware
from app.web.security import (
    CSP,
    AllowedHostMiddleware,
    DefaultCacheControlMiddleware,
    SecurityHeadersMiddleware,
)

ROOT = Path(__file__).resolve().parent.parent
INTERNAL = "internal-detail-plugh"
MakeApp = Callable[..., FastAPI]


# --- factory: no import-time side effects -------------------------------------------------------


def test_importing_the_module_has_no_side_effects() -> None:
    code = (
        "import logging, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import app.web.main as m\n"
        "assert not hasattr(m, 'app'), 'module-level app'\n"
        "assert logging.getLogger().handlers == [], 'logging configured at import'\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows"},
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_create_app_configures_json_logging(make_app: MakeApp, web_settings: Settings) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    make_app(web_settings)
    added = [h for h in root.handlers if h not in before]
    assert len(added) == 1
    assert isinstance(added[0].formatter, JsonFormatter)


# --- lifespan -----------------------------------------------------------------------------------


def test_lifespan_disposes_the_engine_on_shutdown(
    make_app: MakeApp, web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(web_settings)
    calls: list[int] = []
    real = app.state.engine.dispose

    def spy(*_a: object, **_k: object) -> None:
        calls.append(1)
        real()

    monkeypatch.setattr(app.state.engine, "dispose", spy)
    with TestClient(app, base_url="https://testserver"):
        assert calls == []
    assert calls == [1]


def test_lifespan_disposes_even_when_the_app_body_fails(
    make_app: MakeApp, web_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_app(web_settings)
    calls: list[int] = []
    monkeypatch.setattr(app.state.engine, "dispose", lambda *a, **k: calls.append(1))

    async def run() -> None:
        async with app.router.lifespan_context(app):
            raise RuntimeError("crashed while serving")

    with pytest.raises(RuntimeError, match="crashed"):
        asyncio.run(run())
    assert calls == [1]


# --- dependencies -------------------------------------------------------------------------------


class SpySession:
    def __init__(self) -> None:
        self.events: list[str] = []

    def execute(self, *_a: object, **_k: object) -> None:
        self.events.append("execute")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


def with_spy_session(app: FastAPI) -> SpySession:
    spy = SpySession()
    app.state.sessionmaker = lambda: spy
    return spy


def test_healthz_uses_the_dependencies_so_they_can_be_overridden(
    make_app: MakeApp, web_settings: Settings
) -> None:
    app = make_app(web_settings)
    spy = SpySession()
    app.dependency_overrides[get_session] = lambda: spy
    app.dependency_overrides[get_settings] = lambda: web_settings.model_copy(
        update={"render_git_commit": "from-override"}
    )
    with TestClient(app, base_url="https://testserver") as c:
        resp = c.get("/healthz")
    assert resp.json() == {"status": "ok", "version": "from-override"}
    assert spy.events == ["execute"]


def test_healthz_is_503_when_the_overridden_session_fails(
    make_app: MakeApp, web_settings: Settings
) -> None:
    class Broken(SpySession):
        def execute(self, *_a: object, **_k: object) -> None:
            raise OperationalError("select 1", {}, Exception("down: password=P1"))

    app = make_app(web_settings)
    app.dependency_overrides[get_session] = lambda: Broken()
    with TestClient(app, base_url="https://testserver") as c:
        resp = c.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "error"}


def test_healthz_stays_cheap_one_statement_and_nothing_else(
    make_app: MakeApp, web_settings: Settings
) -> None:
    app = make_app(web_settings)
    spy = with_spy_session(app)
    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/healthz").status_code == 200
    assert spy.events == ["execute", "close"]


def test_session_dependency_closes_after_a_successful_request(
    make_app: MakeApp, web_settings: Settings
) -> None:
    app = make_app(web_settings)
    spy = with_spy_session(app)

    @app.get("/t/ok")
    def ok(session: Session = Depends(get_session)) -> dict[str, bool]:  # noqa: B008
        assert cast(object, session) is spy
        return {"ok": True}

    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/t/ok").json() == {"ok": True}
    assert spy.events == ["close"]


def test_session_dependency_rolls_back_and_closes_on_error(
    make_app: MakeApp, web_settings: Settings
) -> None:
    app = make_app(web_settings)
    spy = with_spy_session(app)

    @app.get("/t/fail")
    def fail(session: Session = Depends(get_session)) -> None:  # noqa: B008
        raise RuntimeError(INTERNAL)

    @app.get("/t/http-fail")
    def http_fail(session: Session = Depends(get_session)) -> None:  # noqa: B008
        raise HTTPException(status_code=403)

    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        assert c.get("/t/fail").status_code == 500
        assert spy.events == ["rollback", "close"]
        spy.events.clear()
        assert c.get("/t/http-fail").status_code == 403
        assert spy.events == ["rollback", "close"]


def test_get_settings_returns_the_app_settings(make_app: MakeApp, web_settings: Settings) -> None:
    app = make_app(web_settings)

    @app.get("/t/settings")
    def show(settings: Settings = Depends(get_settings)) -> dict[str, str]:  # noqa: B008
        return {"env": settings.env}

    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/t/settings").json() == {"env": "development"}


# --- engine ---------------------------------------------------------------------------------


def test_postgres_engine_has_connect_and_pool_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    real = sa.create_engine

    def spy(url: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr(db, "create_engine", spy)
    engine = make_engine("postgres://u:p@localhost/db")
    try:
        assert captured["connect_args"] == {"connect_timeout": 3}
        assert captured["pool_timeout"] == 5
        assert engine.pool.timeout() == 5  # type: ignore[attr-defined]
    finally:
        engine.dispose()


def test_sqlite_engine_gets_no_postgres_only_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}
    real = sa.create_engine
    monkeypatch.setattr(
        db, "create_engine", lambda url, **kw: (captured.update(kw), real(url, **kw))[1]
    )
    make_engine(f"sqlite:///{tmp_path / 'x.db'}").dispose()
    assert "connect_args" not in captured
    assert "pool_timeout" not in captured


# --- one middleware stack, in a documented order --------------------------------------------


def stack(app: FastAPI) -> list[Any]:
    return [m.cls for m in app.user_middleware]


def test_middleware_order_is_pinned(make_app: MakeApp, web_settings: Settings) -> None:
    # Outermost first. Changing this order changes what error pages carry; see
    # app/web/middleware.py before touching it.
    assert stack(make_app(web_settings)) == [
        ProxyHeadersMiddleware,
        RequestLogMiddleware,
        SecurityHeadersMiddleware,
        DefaultCacheControlMiddleware,
        AllowedHostMiddleware,
        ErrorBoundaryMiddleware,
    ]


def test_cors_sits_between_cache_control_and_host_check(
    make_app: MakeApp, web_settings: Settings
) -> None:
    settings = web_settings.model_copy(update={"cors_allowed_origins": ["https://a.example.com"]})
    assert stack(make_app(settings)) == [
        ProxyHeadersMiddleware,
        RequestLogMiddleware,
        SecurityHeadersMiddleware,
        DefaultCacheControlMiddleware,
        CORSMiddleware,
        AllowedHostMiddleware,
        ErrorBoundaryMiddleware,
    ]


class Misbehaving:
    """A later-added middleware that fails; stands in for whatever a future PR adds."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/t/mw-http":
            raise HTTPException(status_code=403, detail=INTERNAL)
        if scope["type"] == "http" and scope["path"] == "/t/mw-boom":
            raise RuntimeError(INTERNAL)
        await self.app(scope, receive, send)


@pytest.fixture
def mw_client(make_app: MakeApp, web_settings: Settings) -> Iterator[TestClient]:
    app = make_app(web_settings, extra_middleware=[Middleware(Misbehaving)])
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def test_extra_middleware_runs_innermost(make_app: MakeApp, web_settings: Settings) -> None:
    app = make_app(web_settings, extra_middleware=[Middleware(Misbehaving)])
    assert stack(app)[-2:] == [ErrorBoundaryMiddleware, Misbehaving]


@pytest.mark.parametrize(
    ("path", "status", "french"),
    [("/t/mw-http", 403, "Accès refusé"), ("/t/mw-boom", 500, "Une erreur est survenue")],
)
def test_a_failing_later_middleware_still_gets_the_custom_page_and_headers(
    mw_client: TestClient, path: str, status: int, french: str
) -> None:
    resp = mw_client.get(path)
    assert resp.status_code == status
    assert french in resp.text
    assert INTERNAL not in resp.text
    assert resp.headers["content-security-policy"] == CSP
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])
    assert resp.headers["cache-control"] == "no-store"
    assert "server" not in resp.headers


def test_a_failing_later_middleware_is_in_the_request_log(
    mw_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="app.request"):
        resp = mw_client.get("/t/mw-boom")
    (rec,) = [r for r in caplog.records if r.name == "app.request"]
    assert rec.status == 500  # type: ignore[attr-defined]
    assert rec.request_id == resp.headers["x-request-id"]  # type: ignore[attr-defined]


def test_middleware_module_documents_the_design_rule() -> None:
    text = (ROOT / "app" / "web" / "middleware.py").read_text(encoding="utf-8")
    assert "dependencies, not middleware" in text


# --- request id context var -----------------------------------------------------------------


def http_scope() -> dict[str, Any]:
    return {"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b""}


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def drive(inner: Any) -> tuple[list[dict[str, Any]], str | None]:
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def run() -> str | None:
        assert request_id_var.get() is None
        try:
            await RequestLogMiddleware(inner)(http_scope(), _noop_receive, send)  # type: ignore[arg-type]
        finally:
            after = request_id_var.get()
        return after

    return sent, asyncio.run(run())


def test_request_id_var_is_set_inside_the_request_and_cleared_after() -> None:
    seen: list[str | None] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(request_id_var.get())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent, after = drive(inner)
    assert after is None
    assert seen[0] is not None
    assert re.fullmatch(r"[0-9a-f]{32}", seen[0])
    header = dict(sent[0]["headers"])
    assert header[b"x-request-id"].decode() == seen[0]


def test_request_id_var_is_cleared_when_the_app_raises() -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        drive(inner)
    assert request_id_var.get() is None


# --- cache-control and the rest of the response surface ------------------------------------------


@pytest.mark.parametrize("path", ["/healthz", "/offline", "/nope", "/docs"])
def test_dynamic_responses_default_to_no_store(client: TestClient, path: str) -> None:
    assert client.get(path).headers["cache-control"] == "no-store"


def test_static_files_and_service_worker_override_the_default(client: TestClient) -> None:
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"
    assert client.get("/static/does-not-exist.js").headers["cache-control"] == "no-store"


def test_a_route_can_set_its_own_cache_control(make_app: MakeApp, web_settings: Settings) -> None:
    app = make_app(web_settings)

    @app.get("/t/cached")
    def cached(request: Request) -> Any:
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("x", headers={"Cache-Control": "public, max-age=60"})

    with TestClient(app, base_url="https://testserver") as c:
        assert c.get("/t/cached").headers["cache-control"] == "public, max-age=60"
