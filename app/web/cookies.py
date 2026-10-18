"""Cookie names and attributes in one place.

``__Host-`` cookies must be ``Secure``, have ``Path=/`` and no ``Domain``: the browser then
refuses to let a sibling subdomain overwrite them. Over plain http (local development only,
``COOKIE_SECURE=false``) a browser would discard a ``__Host-`` cookie that lacks ``Secure``, so
there the prefix is dropped; production refuses to start with insecure cookies.
"""

from __future__ import annotations

from starlette.responses import Response

from app.auth.sessions import SessionPolicy
from app.settings import Settings

SESSION_COOKIE = "session"
LOGIN_CSRF_COOKIE = "login_csrf"


def _name(base: str, settings: Settings) -> str:
    return f"__Host-{base}" if settings.cookie_secure else base


def session_cookie_name(settings: Settings) -> str:
    return _name(SESSION_COOKIE, settings)


def login_csrf_cookie_name(settings: Settings) -> str:
    return _name(LOGIN_CSRF_COOKIE, settings)


def set_session_cookie(
    response: Response, token: str, settings: Settings, policy: SessionPolicy
) -> None:
    response.set_cookie(
        session_cookie_name(settings),
        token,
        max_age=int(policy.absolute.total_seconds()),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        session_cookie_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def set_login_csrf_cookie(response: Response, value: str, settings: Settings) -> None:
    response.set_cookie(
        login_csrf_cookie_name(settings),
        value,
        max_age=3600,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )


def clear_login_csrf_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        login_csrf_cookie_name(settings),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
