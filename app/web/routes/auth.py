"""Login and logout.

``POST /login`` does its checks in a fixed order, and the order is the security design:

1. CSRF (a route dependency: cookie against hidden field, plus Origin), before anything is read;
2. the rate limits, per client IP and then per normalised phone, counted and *committed* before
   any account is looked at (so a flood never reaches the user table, and the count survives a
   failed request, even one that raises);
3. only then the exact-phone lookup (``authenticate``), which also hides whether the phone exists;
4. the verification (with no database connection held: see ``app.auth.login``).

The limits count FAILURES: the attempt is counted up front, and a successful login takes it back
(the phone bucket is deleted, the IP counter refunded). So a whole office behind one NAT address
can log in all day, while five wrong passwords for one phone, or 100 wrong ones from one address,
within 15 minutes are refused. A client with no address (``request.client`` is None) skips the IP
limit with a logged warning; the phone limit still applies.

A rate-limit refusal writes one ``rate_limited`` audit row per bucket window (the first one over
the limit); the rest only reach the JSON log. A CSRF failure of an anonymous client is never a row.

Every failure shows the same French message; the internal reason is only in the audit event.
Phones are masked (``+222******12``) everywhere they are logged or audited.
"""

from __future__ import annotations

import logging
from datetime import datetime
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
    session_cookie_name,
    set_login_csrf_cookie,
    set_session_cookie,
)
from app.web.csrf import (
    csrf_protect_if_logged_in,
    login_csrf_protect,
    new_login_token,
    usable_login_token,
)
from app.web.deps import (
    AuthContext,
    client_ip,
    get_auth,
    get_clock,
    get_session,
    get_settings,
)
from app.web.pages import page

router = APIRouter()
log = logging.getLogger("app.web.auth")

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


def rate_limited(
    db: Session,
    request: Request,
    now: datetime,
    scope: str,
    decision: ratelimit.RateDecision,
    phone: str | None,
) -> HTTPException:
    """The 429. Only the first refusal of a bucket window is a database row."""
    details = {"scope": scope, "phone": mask_phone(phone) if phone else None}
    ip = client_ip(request)
    if decision.first_rejection:
        audit.record_and_commit(db, audit.RATE_LIMITED, ip=ip, details=details, now=now)
    else:
        audit.log_only(audit.RATE_LIMITED, ip=ip, details=details)
    return HTTPException(status_code=429, headers={"Retry-After": str(decision.retry_after)})


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

    if ip is None:
        log.warning("login from a client without an address: the per-IP limit is skipped")
    by_ip = ratelimit.hit(db, ratelimit.LOGIN_IP, ip, now) if ip is not None else None
    ip_allowed = by_ip is None or by_ip.allowed
    by_phone = ratelimit.hit(db, ratelimit.LOGIN_PHONE, e164, now) if e164 and ip_allowed else None
    db.commit()  # the counts must survive whatever happens next
    if by_ip is not None and not by_ip.allowed:
        raise rate_limited(db, request, now, "ip", by_ip, e164)
    if by_phone is not None and not by_phone.allowed:
        raise rate_limited(db, request, now, "phone", by_phone, e164)

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
            now=now,
        )
        return _login_page(settings, request, status_code=401, error=GENERIC_FAILURE)

    # Success: the attempt does not count (see the module doc), and the browser's old session ends.
    if e164:
        ratelimit.reset(db, ratelimit.LOGIN_PHONE, e164)
    if ip is not None:
        ratelimit.refund(db, ratelimit.LOGIN_IP, ip, now)
    policy = SessionPolicy.from_settings(settings)
    sessions.revoke_by_token(
        db, request.cookies.get(session_cookie_name(settings), ""), result.user.agency_id, now
    )
    created = sessions.create_session(db, result.user, now, policy)  # a new token every login
    audit.record(
        db,
        audit.LOGIN_SUCCESS,
        agency_id=result.user.agency_id,
        actor_user_id=result.user.id,
        ip=ip,
        details={"phone": masked},
        now=now,
    )
    db.commit()
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, created.token, settings, policy)
    clear_login_csrf_cookie(response, settings)
    return response


@router.post("/logout", dependencies=[Depends(csrf_protect_if_logged_in)])
def logout(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext | None, Depends(get_auth)],
) -> Response:
    """Idempotent: with no live session it just clears the cookies and goes to the login page
    (a stale tab's logout button must not show an error)."""
    if auth is not None:
        now = clock()
        sessions.revoke_session(db, auth.session, now)
        audit.record(
            db,
            audit.LOGOUT,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=client_ip(request),
            now=now,
        )
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, settings)
    clear_login_csrf_cookie(response, settings)
    return response
