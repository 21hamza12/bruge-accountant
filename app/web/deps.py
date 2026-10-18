"""FastAPI dependencies shared by routes (and replaceable in tests via dependency_overrides)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import sessions
from app.auth.sessions import SessionPolicy
from app.clock import Clock
from app.models import User, UserSession
from app.settings import Settings
from app.web.cookies import session_cookie_name

UNKNOWN_IP = "unknown"


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_clock(request: Request) -> Clock:
    return cast(Clock, request.app.state.clock)


def get_session(request: Request) -> Iterator[Session]:
    """One session per request: rolled back if the request fails, always closed.

    It starts with no tenant bound. Only an authenticated session lookup (``get_auth``), the
    login route once it knows the user, or an admin path binds one.
    """
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def client_ip(request: Request) -> str:
    """The client address as ``ProxyHeadersMiddleware`` set it (trusted hops only).

    Never parse ``X-Forwarded-For`` here: the middleware already took the right entry.
    """
    return request.client.host if request.client else UNKNOWN_IP


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
