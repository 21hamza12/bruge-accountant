"""Pages for the agency's partners and their fee history (manager and accountant).

Every route needs a user who may manage partners (``require_permission(can_manage_partners)``:
staff get a 403 page and a ``permission_denied`` audit event) and a CSRF token on POST. The agency
is always the session's; a partner id of another agency is a 404, like one that does not exist.
Pages carry names and money, so they are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import partners, permissions
from app.clock import Clock
from app.models import Partner
from app.partners import PartnerError
from app.web.csrf import csrf_protect
from app.web.deps import (
    AuthContext,
    client_ip,
    get_clock,
    get_session,
    require_permission,
)
from app.web.pages import page
from app.web.params import PartnerId
from app.web.shell import ACTIVE_PARTNERS, shell_context

router = APIRouter(prefix="/partners", dependencies=[Depends(csrf_protect)])
require_partner_admin = require_permission(permissions.can_manage_partners)

NO_STORE = {"Cache-Control": "no-store"}


def _list_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return page(
        "partners/list.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNERS),
        rows=partners.list_partners(db, auth.user.agency_id, today=clock().date()),
        error=error,
        form=form or {},
    )


def _detail_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    partner: Partner,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    agency_id = auth.user.agency_id
    today = clock().date()
    return page(
        "partners/detail.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNERS),
        partner=partner,
        history=partners.fee_history(db, agency_id, partner.id),
        current_fee=partners.fee_on(db, agency_id, partner.id, today),
        today=today,
        error=error,
        form=form or {},
    )


def _partner(db: Session, auth: AuthContext, partner_id: int) -> Partner:
    partner = partners.get_partner(db, auth.user.agency_id, partner_id)
    if partner is None:
        raise HTTPException(status_code=404)
    return partner


def _back(partner_id: int) -> Response:
    return RedirectResponse(f"/partners/{partner_id}", status_code=303)


@router.get("")
def partner_list(
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
) -> Response:
    return _list_page(db, auth, clock)


@router.post("")
def partner_create(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
    name: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
) -> Response:
    try:
        created = partners.create_partner(
            db, auth.user, name, phone, now=clock(), ip=client_ip(request)
        )
        db.commit()
    except PartnerError as exc:
        db.rollback()
        return _list_page(
            db, auth, clock, error=str(exc), form={"name": name, "phone": phone}, status_code=400
        )
    return _back(created.id)


@router.get("/{partner_id}")
def partner_detail(
    partner_id: PartnerId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
) -> Response:
    return _detail_page(db, auth, clock, _partner(db, auth, partner_id))


@router.post("/{partner_id}")
def partner_update(
    request: Request,
    partner_id: PartnerId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
    name: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
) -> Response:
    partner = _partner(db, auth, partner_id)
    try:
        partners.update_partner(
            db, auth.user, partner, name=name, phone=phone, now=clock(), ip=client_ip(request)
        )
        db.commit()
    except PartnerError as exc:
        db.rollback()
        return _detail_page(
            db,
            auth,
            clock,
            partner,
            error=str(exc),
            form={"name": name, "phone": phone},
            status_code=400,
        )
    return _back(partner_id)


@router.post("/{partner_id}/fees")
def partner_set_fee(
    request: Request,
    partner_id: PartnerId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
    fee: Annotated[str, Form()] = "",
    valid_from: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    partner = _partner(db, auth, partner_id)
    try:
        partners.set_fee(
            db,
            partner,
            fee,
            partners.parse_valid_from(valid_from),
            auth.user,
            note,
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except PartnerError as exc:
        db.rollback()
        return _detail_page(
            db,
            auth,
            clock,
            partner,
            error=str(exc),
            form={"fee": fee, "valid_from": valid_from, "note": note},
            status_code=400,
        )
    return _back(partner_id)


@router.get("/{partner_id}/deactivate")
def partner_confirm_deactivate(
    partner_id: PartnerId,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
) -> Response:
    """The confirmation step: nothing changes until the form on this page is posted."""
    partner = _partner(db, auth, partner_id)
    return page(
        "partners/confirm_deactivate.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNERS),
        partner=partner,
    )


@router.post("/{partner_id}/reactivate")
def partner_reactivate(
    request: Request,
    partner_id: PartnerId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
) -> Response:
    partner = _partner(db, auth, partner_id)
    try:
        partners.reactivate_partner(db, auth.user, partner, now=clock(), ip=client_ip(request))
        db.commit()
    except PartnerError as exc:
        db.rollback()
        return _detail_page(db, auth, clock, partner, error=str(exc), status_code=400)
    return _back(partner_id)


@router.post("/{partner_id}/deactivate")
def partner_deactivate(
    request: Request,
    partner_id: PartnerId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
) -> Response:
    partner = _partner(db, auth, partner_id)
    try:
        partners.deactivate_partner(db, auth.user, partner, now=clock(), ip=client_ip(request))
        db.commit()
    except PartnerError as exc:
        db.rollback()
        return _detail_page(db, auth, clock, partner, error=str(exc), status_code=400)
    return RedirectResponse("/partners", status_code=303)
