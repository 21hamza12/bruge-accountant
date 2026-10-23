"""FastAPI dependencies shared by routes (and replaceable in tests via dependency_overrides)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from app import audit, permissions
from app.auth import sessions
from app.auth.sessions import SessionPolicy
from app.clock import Clock
from app.models import User, UserSession
from app.settings import Settings
from app.web.cookies import session_cookie_name
from app.web.errors import ResponseException, error_response, new_reference


# The trivial dependencies are ``async def``: they do no I/O, so they run on the event loop and
# never take one of the 40 shared worker threads (a sync dependency costs a thread hop each).
async def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def get_clock(request: Request) -> Clock:
    return cast(Clock, request.app.state.clock)


def get_session(request: Request) -> Iterator[Session]:
    """One session per request: rolled back if the request fails, always closed.

    It starts with no tenant bound. Only an authenticated session lookup (``get_auth``), the
    login route once it knows the user, or an admin path binds one.
    """
    session: Session = request.app.state.sessionmaker()
    info = getattr(session, "info", None)  # what the page builders read (the screenshot button)
    if info is not None:
        info["ai_client"] = getattr(request.app.state, "ai_client", None) is not None
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def client_ip(request: Request) -> str | None:
    """The client address as ``ProxyHeadersMiddleware`` set it (trusted hops only), or None when
    the connection carries none (a unix socket, a misbehaving proxy).

    Never parse ``X-Forwarded-For`` here: the middleware already took the right entry. None is
    never turned into a shared placeholder: callers skip the per-IP limit for it.
    """
    return request.client.host if request.client else None


@dataclass(frozen=True)
class AuthContext:
    user: User
    session: UserSession

    @property
    def csrf_token(self) -> str:
        return self.session.csrf_token


def get_auth(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
) -> AuthContext | None:
    """The logged-in user and session from the cookie, or None. Binds the tenant when found."""
    token = request.cookies.get(session_cookie_name(settings))
    if not token:
        return None
    found = sessions.lookup_session(db, token, clock(), SessionPolicy.from_settings(settings))
    if found is None:
        return None
    return AuthContext(found.user, found.session)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _send_away(
    request: Request, location: str, status_code: int
) -> ResponseException | HTTPException:
    """How to turn a request away to ``location``, by kind of request.

    * htmx: ``status_code`` with ``HX-Redirect`` (a 3xx would be followed inside the swap target);
    * a plain page view (GET, HEAD): a 303 redirect;
    * anything else (a stale form POST): the error page with ``status_code``, no redirect.
    """
    if _is_htmx(request):
        return ResponseException(
            error_response(status_code, new_reference(), {"HX-Redirect": location})
        )
    if request.method in ("GET", "HEAD"):
        return ResponseException(RedirectResponse(location, status_code=303))
    return HTTPException(status_code=status_code)


def require_login(
    request: Request, auth: Annotated[AuthContext | None, Depends(get_auth)]
) -> AuthContext:
    """A logged-in user, even one who still has to change the password (``/password/change`` and
    ``/logout`` use this). Anonymous requests are sent to the login page."""
    if auth is None:
        raise _send_away(request, "/login", 401)
    return auth


def current_user(
    request: Request, auth: Annotated[AuthContext, Depends(require_login)]
) -> AuthContext:
    """A logged-in user who has no pending forced password change."""
    if auth.user.must_change_password:
        raise _send_away(request, "/password/change", 403)
    return auth


def require_permission(
    check: Callable[[User], bool],
) -> Callable[[Request, Session, Clock, AuthContext], AuthContext]:
    """A dependency that lets a user through when ``check(user)`` (a function of
    ``app.permissions``) is true; anybody else gets the 403 page and a ``permission_denied``
    audit event. A role that no permission mentions is refused by every check."""

    def dependency(
        request: Request,
        db: Annotated[Session, Depends(get_session)],
        clock: Annotated[Clock, Depends(get_clock)],
        auth: Annotated[AuthContext, Depends(current_user)],
    ) -> AuthContext:
        if not check(auth.user):
            audit.record_and_commit(
                db,
                audit.PERMISSION_DENIED,
                agency_id=auth.user.agency_id,
                actor_user_id=auth.user.id,
                ip=client_ip(request),
                details={"path": request.url.path, "method": request.method},
                now=clock(),
            )
            raise HTTPException(status_code=403)
        return auth

    dependency.__name__ = f"require_{getattr(check, '__name__', 'permission')}"
    dependency.__qualname__ = dependency.__name__
    return dependency


# The staff pages: whoever may manage staff (managers only).
require_staff_admin = require_permission(permissions.can_manage_staff)
