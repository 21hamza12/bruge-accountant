"""Manager pages for the agency's staff: list, create, link signs, role, deactivate, re-issue.

Every route needs a logged-in manager (``require_manager``: staff get a 403 page and a
``permission_denied`` audit event) and a CSRF token on POST. An id that belongs to another agency
is a 404, exactly like an id that does not exist.

A temporary password is shown only in the response of the POST that issued it, never in a redirect,
URL or later page, with ``Cache-Control: no-store``.

A manager may issue a temporary password for STAFF only, never for another manager or for
themselves (the 403 page and a ``permission_denied`` audit event). A locked-out manager is reset
by the platform owner with the ``reset-password`` CLI command (docs/runbook.md).

A reload of the POST response ("resend the form") must not issue a second password silently. The
re-issue form carries ``expected_temp_id``, the id of the user's newest temporary password when
the page was drawn; the POST refuses unless that is still the newest (checked with the user row
locked). A replay carries the old id, so it is refused with a message. This is simpler than a
single-use CSRF token (no extra state, and the session's token keeps working for every other
form) and needs no confirmation page.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import accounts, audit
from app.accounts import AccountError
from app.auth import temp_passwords
from app.clock import Clock
from app.models import ROLE_STAFF, User
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, get_clock, get_session, require_manager
from app.web.pages import page
from app.web.params import SignId, UserId

router = APIRouter(prefix="/staff", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
DEACTIVATED = "Ce compte est désactivé."
ALREADY_ISSUED = (
    "Un mot de passe temporaire a déjà été émis pour ce compte depuis l'affichage de cette page. "
    "Rouvrez la fiche pour en émettre un nouveau si nécessaire."
)


def _list_page(
    db: Session,
    auth: AuthContext,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return page(
        "staff/list.html",
        status_code=status_code,
        csrf_token=auth.csrf_token,
        rows=accounts.list_staff(db, auth.user.agency_id),
        me=auth.user,
        error=error,
        form=form or {"role": ROLE_STAFF},
    )


def _detail_page(
    db: Session,
    auth: AuthContext,
    user: User,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    return page(
        "staff/detail.html",
        status_code=status_code,
        csrf_token=auth.csrf_token,
        target=user,
        can_reissue=user.role == ROLE_STAFF and user.is_active,
        expected_temp_id=temp_passwords.latest_id(db, user),
        signs=accounts.get_signs(db, auth.user.agency_id, user.id),
        me=auth.user,
        error=error,
    )


def _target(db: Session, auth: AuthContext, user_id: int, *, lock: bool = False) -> User:
    user = accounts.get_user(db, auth.user.agency_id, user_id, lock=lock)
    if user is None:
        raise HTTPException(status_code=404)
    return user


def _created_page(user: User, plaintext: str, auth: AuthContext) -> Response:
    return page(
        "staff/created.html",
        headers=NO_STORE,
        csrf_token=auth.csrf_token,
        target=user,
        temp_password=plaintext,
    )


@router.get("")
def staff_list(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    return _list_page(db, auth)


@router.post("")
def staff_create(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
    display_name: Annotated[str, Form(max_length=512)] = "",
    phone: Annotated[str, Form(max_length=256)] = "",
    role: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    try:
        user, plaintext = accounts.create_user(
            db,
            auth.user,
            display_name=display_name,
            phone=phone,
            role=role,
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _list_page(
            db,
            auth,
            error=str(exc),
            form={"display_name": display_name, "phone": phone, "role": role},
            status_code=400,
        )
    return _created_page(user, plaintext, auth)


@router.get("/{user_id}")
def staff_detail(
    user_id: UserId,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    return _detail_page(db, auth, _target(db, auth, user_id))


def _back(user_id: int) -> Response:
    return RedirectResponse(f"/staff/{user_id}", status_code=303)


@router.post("/{user_id}/signs")
def staff_link_sign(
    request: Request,
    user_id: UserId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
    sign: Annotated[str, Form(max_length=256)] = "",
) -> Response:
    user = _target(db, auth, user_id)
    try:
        accounts.link_sign(db, auth.user, user, sign, clock(), client_ip(request))
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _detail_page(db, auth, user, error=str(exc), status_code=400)
    return _back(user_id)


@router.post("/{user_id}/signs/{sign_id}/unlink")
def staff_unlink_sign(
    request: Request,
    user_id: UserId,
    sign_id: SignId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    user = _target(db, auth, user_id)
    if not accounts.unlink_sign(db, auth.user, user, sign_id, clock(), client_ip(request)):
        raise HTTPException(status_code=404)
    db.commit()
    return _back(user_id)


@router.post("/{user_id}/role")
def staff_change_role(
    request: Request,
    user_id: UserId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
    role: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    user = _target(db, auth, user_id)
    try:
        accounts.change_role(db, auth.user, user, role, clock(), client_ip(request))
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _detail_page(db, auth, user, error=str(exc), status_code=400)
    return _back(user_id)


@router.post("/{user_id}/deactivate")
def staff_deactivate(
    request: Request,
    user_id: UserId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    user = _target(db, auth, user_id)
    try:
        accounts.deactivate_user(db, auth.user, user, clock(), client_ip(request))
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _detail_page(db, auth, user, error=str(exc), status_code=400)
    return RedirectResponse("/staff", status_code=303)


@router.post("/{user_id}/temp-password")
def staff_reissue_temp_password(
    request: Request,
    user_id: UserId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
    expected_temp_id: Annotated[str, Form(max_length=20)] = "",
) -> Response:
    now = clock()
    user = _target(db, auth, user_id, lock=True)  # FOR UPDATE: one reissue at a time per user
    if user.role != ROLE_STAFF:
        # Managers (including the caller) are reset by the platform owner, not by a manager.
        audit.record_and_commit(
            db,
            audit.PERMISSION_DENIED,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=client_ip(request),
            details={"path": request.url.path, "method": request.method, "reason": "manager_reset"},
            now=now,
        )
        raise HTTPException(status_code=403)
    if not user.is_active:
        return _detail_page(db, auth, user, error=DEACTIVATED, status_code=400)
    if expected_temp_id != str(temp_passwords.latest_id(db, user)):
        return _detail_page(db, auth, user, error=ALREADY_ISSUED, status_code=400)
    plaintext = temp_passwords.issue_temporary_password(
        db, user, now, actor_user_id=auth.user.id, ip=client_ip(request)
    )
    db.commit()
    return _created_page(user, plaintext, auth)
