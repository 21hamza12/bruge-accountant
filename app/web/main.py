"""FastAPI application factory.

Endpoints are plain ``def`` functions: SQLAlchemy sessions are synchronous, so FastAPI runs
them in its thread pool instead of blocking the event loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import anyio
import anyio.to_thread
from anyio.lowlevel import RunVar
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sentry_sdk.transport import Transport
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware import Middleware
from starlette.responses import Response

from app import alerting, db_roles
from app.ai.chat_client import AnthropicChatClient, ChatModelClient
from app.ai.client import AnthropicTranscriber, TranscribeClient
from app.clock import Clock, utc_now
from app.db import make_engine, make_session_factory
from app.observability import configure_logging, quiet_libraries
from app.settings import Settings
from app.web.deps import get_settings
from app.web.errors import (
    ResponseException,
    http_exception_handler,
    response_exception_handler,
    validation_exception_handler,
)
from app.web.middleware import build_middleware
from app.web.routes import apres_compare as apres_compare_routes
from app.web.routes import auth as auth_routes
from app.web.routes import chat as chat_routes
from app.web.routes import days as day_routes
from app.web.routes import flag_actions as flag_action_routes
from app.web.routes import flags as flag_routes
from app.web.routes import home as home_routes
from app.web.routes import partners as partner_routes
from app.web.routes import password as password_routes
from app.web.routes import questions as questions_routes
from app.web.routes import sale_entry as sale_entry_routes
from app.web.routes import sales as sales_routes
from app.web.routes import staff as staff_routes
from app.web.routes import uploads as upload_routes
from app.web.templating import TEMPLATES_DIR, render

log = logging.getLogger("app.web")
SERVICE_WORKER = TEMPLATES_DIR.parent / "sw.js"
HEALTH_TIMEOUT_SECONDS = 2

# /healthz must not queue behind request work: it runs its one tiny query on a worker thread
# of its own (a dedicated limiter, not the 40 shared tokens) and gives up after two seconds.
# One limiter per event loop (anyio limiters belong to the loop that created them).
_health_limiter: RunVar[anyio.CapacityLimiter] = RunVar("bruge_health_limiter")


def _get_health_limiter() -> anyio.CapacityLimiter:
    try:
        return _health_limiter.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(1)
        _health_limiter.set(limiter)
        return limiter


def _ping(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(select(1))


IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


class StaticAssets(StaticFiles):
    """Static files revalidate on every load (ETag makes that cheap), so a deploy ships at once.

    The one exception is the font files (``fonts/*.woff2``): a font never changes in place, a new
    version gets a new file name (``app/web/VENDORED.md``, pinned by the hash test), so browsers
    keep them for a year without asking.
    """

    def file_response(self, full_path: Any, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(full_path, *args, **kwargs)
        path = Path(str(full_path))
        is_font = path.suffix == ".woff2" and path.parent.name == "fonts"
        response.headers["Cache-Control"] = IMMUTABLE_CACHE if is_font else "no-cache"
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


def _real_chat_client(settings: Settings) -> ChatModelClient | None:
    if not settings.chat_ai_active or settings.anthropic_api_key is None:
        return None
    client = AnthropicChatClient(
        settings.anthropic_api_key.get_secret_value().strip(),
        timeout_s=settings.ai_chat_timeout_s,
        max_retries=settings.ai_chat_max_retries,
    )
    quiet_libraries()  # the SDK was just imported: it may have lowered its loggers
    return client


def _real_ai_client(settings: Settings) -> TranscribeClient | None:
    if not settings.ai_active or settings.anthropic_api_key is None:
        return None
    client = AnthropicTranscriber(
        settings.anthropic_api_key.get_secret_value().strip(),
        timeout_s=settings.ai_timeout_s,
        max_retries=settings.ai_max_retries,
    )
    quiet_libraries()
    return client


def create_app(
    settings: Settings | None = None,
    extra_middleware: Sequence[Middleware] = (),
    clock: Clock | None = None,
    ai_client: TranscribeClient | None = None,
    chat_client: ChatModelClient | None = None,
    alerting_transport: Transport | None = None,
) -> FastAPI:
    """Build the application. Nothing happens at import time; run it with ``uvicorn --factory``."""
    settings = settings or Settings()
    configure_logging()
    alerting.init_alerting(settings, transport=alerting_transport)  # off without SENTRY_DSN
    engine = make_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )

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
    # The AI client: None (feature hidden) unless there is a key and AI is not switched off. Tests
    # pass a fake (``app.ai.fake``) and never reach the real API.
    app.state.ai_client = ai_client or _real_ai_client(settings)
    # The chat's own client (phase E part 2): None means the chat answers with plain code only.
    app.state.chat_client = chat_client or _real_chat_client(settings)

    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ResponseException, response_exception_handler)
    app.include_router(auth_routes.router)
    app.include_router(home_routes.router)
    app.include_router(password_routes.router)
    app.include_router(partner_routes.router)
    app.include_router(sale_entry_routes.screenshot_router)  # before /sales/{sale_id}
    app.include_router(sale_entry_routes.router)  # first: /sales/new before /sales/{sale_id}
    app.include_router(sales_routes.router)
    app.include_router(day_routes.router)
    app.include_router(apres_compare_routes.router)
    app.include_router(staff_routes.router)
    app.include_router(flag_routes.router)
    app.include_router(flag_action_routes.router)
    app.include_router(upload_routes.router)
    app.include_router(questions_routes.router)
    app.include_router(chat_routes.router)
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
    async def healthz(
        request: Request, settings: Annotated[Settings, Depends(get_settings)]
    ) -> JSONResponse:
        # Independent of the shared thread pool and of the per-request session dependency.
        try:
            with anyio.fail_after(HEALTH_TIMEOUT_SECONDS):
                await anyio.to_thread.run_sync(
                    _ping,
                    request.app.state.engine,
                    limiter=_get_health_limiter(),
                    abandon_on_cancel=True,
                )
        except (SQLAlchemyError, TimeoutError):
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse({"status": "ok", "version": settings.version})

    return app
