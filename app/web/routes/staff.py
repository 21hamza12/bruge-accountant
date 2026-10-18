"""Manager pages for the agency's staff: list, create, link signs, role, deactivate, re-issue.

Every route needs a logged-in manager (``require_manager``: staff get a 403 page and a
``permission_denied`` audit event) and a CSRF token on POST. An id that belongs to another agency
is a 404, exactly like an id that does not exist.

A temporary password is shown only in the response of the POST that issued it, never in a redirect,
URL or later page, with ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import accounts
from app.accounts import AccountError
from app.auth import temp_passwords
from app.clock import Clock
from app.models import ROLE_STAFF, User
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, get_clock, get_session, require_manager
from app.web.pages import page

router = APIRouter(prefix="/staff", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
DEACTIVATED = "Ce compte est désactivé."
OWN_ACCOUNT = "Pour votre propre compte, utilisez « Changer le mot de passe »."


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
        signs=accounts.get_signs(db, auth.user.agency_id, user.id),
        me=auth.user,
        error=error,
    )


def _target(db: Session, auth: AuthContext, user_id: int) -> User:
    user = accounts.get_user(db, auth.user.agency_id, user_id)
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
    user_id: int,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    return _detail_page(db, auth, _target(db, auth, user_id))


def _back(user_id: int) -> Response:
    return RedirectResponse(f"/staff/{user_id}", status_code=303)


@router.post("/{user_id}/signs")
def staff_link_sign(
    request: Request,
    user_id: int,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_manager)],
    sign: Annotated[str, Form(max_length=256)] = "",
) -> Response:
    user = _target(db, auth, user_id)
    try:
        accounts.link_sign(db, auth.user, user, sign, client_ip(request))
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _detail_page(db, auth, user, error=str(exc), status_code=400)
    return _back(user_id)


@router.post("/{user_id}/signs/{sign_id}/unlink")
def staff_unlink_sign(
    request: Request,
    user_id: int,
    sign_id: int,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    user = _target(db, auth, user_id)
    if not accounts.unlink_sign(db, auth.user, user, sign_id, client_ip(request)):
        raise HTTPException(status_code=404)
    db.commit()
    return _back(user_id)


@router.post("/{user_id}/role")
def staff_change_role(
    request: Request,
    user_id: int,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_manager)],
    role: Annotated[str, Form(max_length=32)] = "",
) -> Response:
    user = _target(db, auth, user_id)
    try:
        accounts.change_role(db, auth.user, user, role, client_ip(request))
        db.commit()
    except AccountError as exc:
        db.rollback()
        return _detail_page(db, auth, user, error=str(exc), status_code=400)
    return _back(user_id)


@router.post("/{user_id}/deactivate")
def staff_deactivate(
    request: Request,
    user_id: int,
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
    user_id: int,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_manager)],
) -> Response:
    user = _target(db, auth, user_id)
    if not user.is_active:
        return _detail_page(db, auth, user, error=DEACTIVATED, status_code=400)
    if user.id == auth.user.id:
        return _detail_page(db, auth, user, error=OWN_ACCOUNT, status_code=400)
    plaintext = temp_passwords.issue_temporary_password(
        db, user, clock(), actor_user_id=auth.user.id, ip=client_ip(request)
    )
    db.commit()
    return _created_page(user, plaintext, auth)
