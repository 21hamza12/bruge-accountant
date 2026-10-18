"""FastAPI application factory.

Endpoints are plain ``def`` functions: SQLAlchemy sessions are synchronous, so FastAPI runs
them in its thread pool instead of blocking the event loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.responses import Response

from app import db_roles
from app.clock import Clock, utc_now
from app.db import make_engine, make_session_factory
from app.observability import configure_logging
from app.settings import Settings
from app.web.deps import get_session, get_settings
from app.web.errors import (
    ResponseException,
    http_exception_handler,
    response_exception_handler,
    validation_exception_handler,
)
from app.web.middleware import build_middleware
from app.web.routes import auth as auth_routes
from app.web.routes import home as home_routes
from app.web.routes import password as password_routes
from app.web.routes import staff as staff_routes
from app.web.templating import TEMPLATES_DIR, render

log = logging.getLogger("app.web")
SERVICE_WORKER = TEMPLATES_DIR.parent / "sw.js"


class StaticAssets(StaticFiles):
    """Static files revalidate on every load (ETag makes that cheap), so a deploy ships at once."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _check_database_role(engine: Engine, settings: Settings) -> None:
    """A superuser or BYPASSRLS role skips every row-level security policy: refuse to start in
    production as one (Postgres only), warn elsewhere."""
    problem = db_roles.check_connecting_role(engine)
    if problem is None:
        return
    if settings.is_production:
        raise db_roles.PrivilegedRoleError(db_roles.privileged_startup_message(problem))
    log.warning("%s. Tolerated outside production only.", problem)


def create_app(
    settings: Settings | None = None,
    extra_middleware: Sequence[Middleware] = (),
    clock: Clock | None = None,
) -> FastAPI:
    """Build the application. Nothing happens at import time; run it with ``uvicorn --factory``."""
    settings = settings or Settings()
    configure_logging()
    engine = make_engine(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            _check_database_role(engine, settings)
            yield
        finally:
            engine.dispose()

    app = FastAPI(
        title="Bruge Accountant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
        middleware=build_middleware(settings, extra_middleware),
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_session_factory(engine)
    app.state.clock = clock or utc_now  # injected in tests; sessions, limits and expiry use it

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ResponseException, response_exception_handler)
    app.include_router(auth_routes.router)
    app.include_router(home_routes.router)
    app.include_router(password_routes.router)
    app.include_router(staff_routes.router)
    app.mount("/static", StaticAssets(directory=TEMPLATES_DIR.parent / "static"), name="static")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        # no-cache: browsers must revalidate the worker script on every load so updates ship.
        return FileResponse(
            SERVICE_WORKER,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/offline", include_in_schema=False)
    def offline() -> HTMLResponse:
        return HTMLResponse(render("offline.html"))

    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    def healthz(
        settings: Annotated[Settings, Depends(get_settings)],
        session: Annotated[Session, Depends(get_session)],
    ) -> JSONResponse:
        try:
            session.execute(select(1))
        except SQLAlchemyError:
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "version": settings.version})

    return app
