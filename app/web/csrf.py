"""CSRF protection as FastAPI dependencies (not middleware: they need the session and the
database, and they run inside the router where the custom 403 page is rendered).

Two schemes, both also checking where the request came from:

* Logged-in forms: a synchronizer token stored on the ``user_sessions`` row, sent back as the
  hidden field ``csrf_token`` or the ``X-CSRF-Token`` header (htmx gets it through ``hx-headers``
  on ``<body>``, read from a data attribute: no inline script).
* The anonymous login form: a double-submit cookie ``__Host-login_csrf`` (HttpOnly,
  SameSite=Strict) that must equal the hidden field.
* ``Origin`` (falling back to ``Referer``) must be this site's own origin or one of
  ``CORS_ALLOWED_ORIGINS``. A request with neither header is refused: every browser sends one of
  them on a POST. This needs ``TRUSTED_PROXY_HOPS`` right behind a TLS-terminating proxy, so the
  scheme the app sees is the one the browser used.

Tokens are compared in constant time. A failure answers with the custom 403 page. For a logged-in
user it also writes a ``csrf_failed`` audit event (committed first: the error rolls the request
back); for an anonymous client it only goes to the JSON log, so a flood of bare POSTs cannot
become a flood of database rows. Safe methods (GET, HEAD, OPTIONS) are never checked.

The token is read from the ``X-CSRF-Token`` header first and the body is parsed only when there
is no header. The PR 4 upload route does not use ``csrf_protect``: it calls ``verify_origin`` and
``verify_token`` itself so it can run them in its own order (login, Origin, header token, rate
limit) and read the form only afterwards (see ``app.web.routes.uploads``).
"""

from __future__ import annotations

import hmac
import re
import secrets
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import audit
from app.clock import Clock
from app.settings import Settings
from app.web.cookies import login_csrf_cookie_name
from app.web.deps import (
    AuthContext,
    client_ip,
    get_auth,
    get_clock,
    get_session,
    get_settings,
    require_login,
)

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")
_TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{32,64}")


def new_login_token() -> str:
    return secrets.token_urlsafe(32)


def tokens_match(expected: str | None, supplied: str | None) -> bool:
    """Constant-time comparison; False when either side is missing or empty."""
    if not expected or not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8", "replace"))


def usable_login_token(value: str | None) -> str | None:
    """The cookie value if it looks like one of ours, else None (so a fresh one is issued)."""
    return value if value is not None and _TOKEN_SHAPE.fullmatch(value) else None


def _normalise(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


def _referer_origin(referer: str) -> str | None:
    parts = urlsplit(referer)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


def origin_allowed(request: Request, settings: Settings) -> bool:
    """The request came from this site (or an allowlisted origin)."""
    supplied = request.headers.get("origin")
    if supplied is None:
        referer = request.headers.get("referer")
        supplied = _referer_origin(referer) if referer else None
    if not supplied or supplied.strip().lower() == "null":
        return False
    own = f"{request.url.scheme}://{request.headers.get('host', '')}"
    allowed = {_normalise(own), *(_normalise(o) for o in settings.cors_allowed_origins)}
    return _normalise(supplied) in allowed


async def submitted_token(request: Request) -> str | None:
    """The token from the ``X-CSRF-Token`` header, else from the form field."""
    header = request.headers.get(CSRF_HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "").lower()
    if content_type.startswith(_FORM_TYPES):
        value = (await request.form()).get(CSRF_FIELD)
        return value if isinstance(value, str) else None
    return None


async def _refuse(
    request: Request,
    db: Session,
    clock: Clock,
    reason: str,
    auth: AuthContext | None = None,
) -> HTTPException:
    """Record the failure and return the 403 to raise.

    Logged-in: an audit row, committed now. Anonymous: the JSON log only (see the module doc).
    """
    details = {"reason": reason, "method": request.method}
    if auth is None:
        audit.log_only(audit.CSRF_FAILED, ip=client_ip(request), details=details)
        return HTTPException(status_code=403)

    def write() -> None:
        audit.record_and_commit(
            db,
            audit.CSRF_FAILED,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=client_ip(request),
            details=details,
            now=clock(),
        )

    await run_in_threadpool(write)
    return HTTPException(status_code=403)


async def verify_origin(
    request: Request, db: Session, settings: Settings, clock: Clock, auth: AuthContext
) -> None:
    """The Origin step on its own (403 plus an audit event). The upload route runs it before it
    reads any of the body."""
    if not origin_allowed(request, settings):
        raise await _refuse(request, db, clock, "origin", auth)


async def verify_token(
    request: Request, db: Session, clock: Clock, auth: AuthContext, supplied: str | None
) -> None:
    """The token step on its own: ``supplied`` came from the header or the form field."""
    if not tokens_match(auth.csrf_token, supplied):
        raise await _refuse(request, db, clock, "token", auth)


async def _check_logged_in(
    request: Request, db: Session, settings: Settings, clock: Clock, auth: AuthContext
) -> None:
    if request.method in SAFE_METHODS:
        return
    await verify_origin(request, db, settings, clock, auth)
    await verify_token(request, db, clock, auth, await submitted_token(request))


async def csrf_protect(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_login)],
) -> None:
    """Logged-in forms: synchronizer token plus origin check, on unsafe methods only.

    An anonymous request never gets here: ``require_login`` answers it first (login redirect,
    htmx 401 with ``HX-Redirect``, or the 401 page for a stale POST).
    """
    await _check_logged_in(request, db, settings, clock, auth)


async def csrf_protect_if_logged_in(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext | None, Depends(get_auth)],
) -> None:
    """For ``/logout``: a logged-in user must present the token; an anonymous one passes
    (logging out of nothing is harmless and must be idempotent)."""
    if auth is not None:
        await _check_logged_in(request, db, settings, clock, auth)


async def login_csrf_protect(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> None:
    """The anonymous login form: double-submit cookie plus origin check."""
    if request.method in SAFE_METHODS:
        return
    if not origin_allowed(request, settings):
        raise await _refuse(request, db, clock, "origin")
    cookie = request.cookies.get(login_csrf_cookie_name(settings))
    if not tokens_match(cookie, await submitted_token(request)):
        raise await _refuse(request, db, clock, "token")
