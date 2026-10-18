"""Login and logout.

``POST /login`` does its checks in a fixed order, and the order is the security design:

1. CSRF (a route dependency: cookie against hidden field, plus Origin), before anything is read;
2. the rate limits, per client IP and then per normalised phone, counted and *committed* before
   any account is looked at (so a flood never reaches the user table, and the count survives a
   failed request);
3. only then the exact-phone lookup (``authenticate``), which also hides whether the phone exists;
4. the verification.

Every failure shows the same French message; the internal reason is only in the audit event.
Phones are masked (``+222******12``) everywhere they are logged or audited.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import audit
from app.auth import ratelimit, sessions
from app.auth.login import authenticate
from app.auth.phone import InvalidPhoneError, mask_phone, normalize_phone
from app.auth.sessions import SessionPolicy
from app.clock import Clock
from app.settings import Settings
from app.web.cookies import (
    clear_login_csrf_cookie,
    clear_session_cookie,
    login_csrf_cookie_name,
    set_login_csrf_cookie,
    set_session_cookie,
)
from app.web.csrf import csrf_protect, login_csrf_protect, new_login_token, usable_login_token
from app.web.deps import (
    AuthContext,
    client_ip,
    current_user,
    get_auth,
    get_clock,
    get_session,
    get_settings,
)
from app.web.pages import page

router = APIRouter()

GENERIC_FAILURE = "Numéro de téléphone ou mot de passe incorrect."


def _login_page(
    settings: Settings, request: Request, status_code: int = 200, error: str | None = None
) -> Response:
    token = usable_login_token(request.cookies.get(login_csrf_cookie_name(settings)))
    token = token or new_login_token()
    response = page("auth/login.html", status_code=status_code, form_token=token, error=error)
    set_login_csrf_cookie(response, token, settings)
    return response


@router.get("/login")
def login_form(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    auth: Annotated[AuthContext | None, Depends(get_auth)],
) -> Response:
    if auth is not None:
        return RedirectResponse("/", status_code=303)
    return _login_page(settings, request)


def _rate_limited(
    db: Session, request: Request, scope: str, retry_after: int, phone: str | None
) -> HTTPException:
    audit.record_and_commit(
        db,
        audit.RATE_LIMITED,
        ip=client_ip(request),
        details={"scope": scope, "phone": mask_phone(phone) if phone else None},
    )
    return HTTPException(status_code=429, headers={"Retry-After": str(retry_after)})


@router.post("/login", dependencies=[Depends(login_csrf_protect)])
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    phone: Annotated[str, Form(max_length=256)] = "",
    password: Annotated[str, Form(max_length=4096)] = "",
) -> Response:
    now = clock()
    ip = client_ip(request)
    try:
        e164: str | None = normalize_phone(phone)
    except InvalidPhoneError:
        e164 = None

    by_ip = ratelimit.hit(db, ratelimit.LOGIN_IP, ip, now)
    by_phone = (
        ratelimit.hit(db, ratelimit.LOGIN_PHONE, e164, now) if e164 and by_ip.allowed else None
    )
    db.commit()  # the counts must survive whatever happens next
    if not by_ip.allowed:
        raise _rate_limited(db, request, "ip", by_ip.retry_after, e164)
    if by_phone is not None and not by_phone.allowed:
        raise _rate_limited(db, request, "phone", by_phone.retry_after, e164)

    result = authenticate(db, e164, password, now)
    masked = mask_phone(e164) if e164 else None
    if not result.ok or result.user is None:
        user = result.user
        audit.record_and_commit(
            db,
            audit.LOGIN_FAILURE,
            agency_id=user.agency_id if user else None,
            actor_user_id=user.id if user else None,
            ip=ip,
            details={"reason": result.reason, "phone": masked},
        )
        return _login_page(settings, request, status_code=401, error=GENERIC_FAILURE)

    policy = SessionPolicy.from_settings(settings)
    created = sessions.create_session(db, result.user, now, policy)  # a new token every login
    audit.record(
        db,
        audit.LOGIN_SUCCESS,
        agency_id=result.user.agency_id,
        actor_user_id=result.user.id,
        ip=ip,
        details={"phone": masked},
    )
    db.commit()
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, created.token, settings, policy)
    clear_login_csrf_cookie(response, settings)
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    sessions.revoke_session(db, auth.session, clock())
    audit.record(
        db,
        audit.LOGOUT,
        agency_id=auth.user.agency_id,
        actor_user_id=auth.user.id,
        ip=client_ip(request),
    )
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    return response
