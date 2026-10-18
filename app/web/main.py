"""FastAPI application factory.

Endpoints are plain ``def`` functions: SQLAlchemy sessions are synchronous, so FastAPI runs
them in its thread pool instead of blocking the event loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.db import make_engine, make_session_factory
from app.settings import Settings
from app.web.errors import (
    ErrorBoundaryMiddleware,
    http_exception_handler,
    validation_exception_handler,
)
from app.web.security import add_security_middleware
from app.web.templating import TEMPLATES_DIR, render

log = logging.getLogger("app.web")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url.get_secret_value())

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="Bruge Accountant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_session_factory(engine)

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # Catches unhandled exceptions (the "Exception" handler); innermost, see the class docstring.
    app.add_middleware(ErrorBoundaryMiddleware)
    add_security_middleware(app, settings)
    app.mount("/static", StaticFiles(directory=TEMPLATES_DIR.parent / "static"), name="static")

    @app.get("/offline", include_in_schema=False)
    def offline() -> HTMLResponse:
        return HTMLResponse(render("offline.html"))

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request) -> JSONResponse:
        try:
            with request.app.state.sessionmaker() as session:
                session.execute(select(1))
        except SQLAlchemyError:
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "version": settings.version})

    return app


app = create_app()
