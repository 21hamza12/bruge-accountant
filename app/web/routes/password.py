"""Changing the password, including the forced change after a temporary password.

A session whose user must change the password can reach this page and ``/logout`` and nothing
else (``require_login`` does not apply the gate; ``current_user`` does). A successful change:

* checks the current password. The rate limit (5 per 15 minutes per user) counts WRONG guesses
  only: the attempt is counted before the check, so parallel guesses cannot slip under the limit,
  and given back when the current password turns out to be right (a policy failure or a success
  costs nothing). A stolen session must not be a free password oracle,
* applies the password policy together with the user's phone,
* rotates the session token (a new session, a new CSRF secret),
* revokes every other session of the user, and clears the must-change flag.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import audit
from app.auth import ratelimit, sessions, temp_passwords
from app.auth.passwords import hash_password, password_policy_error, verify_password
from app.auth.sessions import SessionPolicy
from app.clock import Clock
from app.settings import Settings
from app.web.cookies import set_session_cookie
from app.web.csrf import csrf_protect
from app.web.deps import (
    AuthContext,
    client_ip,
    get_clock,
    get_session,
    get_settings,
    require_login,
)
from app.web.pages import page
from app.web.shell import ACTIVE_PROFILE, shell_context

router = APIRouter(dependencies=[Depends(csrf_protect)])

WRONG_CURRENT = "Le mot de passe actuel est incorrect."
MISMATCH = "Les deux nouveaux mots de passe ne sont pas identiques."
UNCHANGED = "Le nouveau mot de passe doit être différent de l'ancien."


def _form(
    db: Session, auth: AuthContext, error: str | None = None, status_code: int = 200
) -> Response:
    """The form. A forced change gets a bare page (nothing else is reachable yet); otherwise the
    page lives in the application shell as the user's profile."""
    forced = auth.user.must_change_password
    context = {"csrf_token": auth.csrf_token} if forced else shell_context(db, auth, ACTIVE_PROFILE)
    return page(
        "auth/password_change.html",
        status_code=status_code,
        forced=forced,
        error=error,
        user=auth.user,
        **context,
    )


@router.get("/password/change")
def change_form(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_login)],
) -> Response:
    return _form(db, auth)


def current_password_is_right(db: Session, auth: AuthContext, supplied: str, now: datetime) -> bool:
    """The real password, or - for an account that only has a temporary one - the temporary
    password it logged in with less than 30 minutes ago."""
    if auth.user.password_hash is None:
        return temp_passwords.spent_temporary_matches(db, auth.user, supplied, now)
    return verify_password(supplied, auth.user.password_hash)


@router.post("/password/change")
def change_submit(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_login)],
    current_password: Annotated[str, Form(max_length=4096)] = "",
    new_password: Annotated[str, Form(max_length=4096)] = "",
    confirm_password: Annotated[str, Form(max_length=4096)] = "",
) -> Response:
    now = clock()
    user = auth.user
    subject = f"{user.agency_id}:{user.id}"
    decision = ratelimit.hit(db, ratelimit.PASSWORD_CHANGE_USER, subject, now)
    db.commit()  # counted before the check; this also releases the connection before Argon2
    if not decision.allowed:
        ip = client_ip(request)
        details = {"scope": "password_change"}
        if decision.first_rejection:  # one audit row per window, however long the guessing goes on
            audit.record_and_commit(
                db,
                audit.RATE_LIMITED,
                agency_id=user.agency_id,
                actor_user_id=user.id,
                ip=ip,
                details=details,
                now=now,
            )
        else:
            audit.log_only(
                audit.RATE_LIMITED,
                agency_id=user.agency_id,
                actor_user_id=user.id,
                ip=ip,
                details=details,
            )
        raise HTTPException(status_code=429, headers={"Retry-After": str(decision.retry_after)})

    if not current_password_is_right(db, auth, current_password, now):
        return _form(db, auth, WRONG_CURRENT, 400)
    ratelimit.refund(db, ratelimit.PASSWORD_CHANGE_USER, subject, now)  # only wrong guesses count
    db.commit()
    if new_password != confirm_password:
        return _form(db, auth, MISMATCH, 400)
    if new_password == current_password:
        return _form(db, auth, UNCHANGED, 400)
    problem = password_policy_error(new_password, user.phone)
    if problem is not None:
        return _form(db, auth, problem, 400)

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.updated_at = now
    temp_passwords.retire_pending(db, user, now)
    policy = SessionPolicy.from_settings(settings)
    created = sessions.create_session(db, user, now, policy)  # rotate: a brand new token
    sessions.revoke_all_for_user(db, user.agency_id, user.id, now, except_session_id=created.row.id)
    audit.record(
        db,
        audit.PASSWORD_CHANGED,
        agency_id=user.agency_id,
        actor_user_id=user.id,
        ip=client_ip(request),
        now=now,
    )
    db.commit()
    response = RedirectResponse("/", status_code=303)
    set_session_cookie(response, created.token, settings, policy)
    return response
