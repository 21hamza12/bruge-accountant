"""Pages for the agency's partners and their fee history (manager and accountant).

Every route needs a user who may manage partners (``require_permission(can_manage_partners)``:
staff get a 403 page and a ``permission_denied`` audit event) and a CSRF token on POST. The agency
is always the session's; a partner id of another agency is a 404, like one that does not exist.
Pages carry names and money, so they are ``Cache-Control: no-store``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import partner_aliases, partners, permissions
from app.clock import Clock
from app.models import Partner, User
from app.partner_aliases import AliasError, AliasNotFound
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
from app.web.params import DbId, PartnerId, parse_db_id
from app.web.sale_pages import throttle
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
    agency_id = auth.user.agency_id
    rows = partners.list_partners(db, agency_id, today=clock().date())
    names = {row.partner.id: row.partner.name for row in rows}
    pending = partner_aliases.pending(db, agency_id)
    proposers = {
        u.id: (u.display_name or "").strip() or u.role
        for u in db.scalars(
            select(User).where(
                User.agency_id == agency_id, User.id.in_({a.proposed_by_user_id for a in pending})
            )
        )
    }
    return page(
        "partners/list.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNERS),
        rows=rows,
        aliases=[
            {
                "alias": a,
                "partner_name": names.get(a.partner_id, "?"),
                "by": proposers.get(a.proposed_by_user_id, ""),
            }
            for a in pending
        ],
        rejected=[
            {"alias": a, "partner_name": names.get(a.partner_id, "?")}
            for a in partner_aliases.rejected(db, agency_id)
        ],
        alias_cap=partner_aliases.at_cap(db, agency_id),
        active_partners=[row.partner for row in rows if row.partner.is_active],
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
        aliases=partner_aliases.approved_for(db, agency_id, partner.id),
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


# --- aliases (names an agency is known by in the Amadeus text) --------------------------------
# Declared before the ``/{partner_id}`` routes. Same rules as the rest of the page: a manager or
# accountant, CSRF, an alias of another agency is a 404.


def _alias_action(
    request: Request,
    db: Session,
    auth: AuthContext,
    clock: Clock,
    run: Callable[[], None],
    *,
    back_to: str = "/partners",
    error_page: Callable[[str], Response] | None = None,
) -> Response:
    throttle(db, request, auth, clock, write=True)  # the sale write budget, per user
    try:
        run()
        db.commit()
    except AliasNotFound:
        db.rollback()
        raise HTTPException(status_code=404) from None
    except (AliasError, PartnerError) as exc:
        db.rollback()
        if error_page is not None:
            return error_page(str(exc))
        return _list_page(db, auth, clock, error=str(exc), status_code=400)
    return RedirectResponse(back_to, status_code=303)


def _simple(action: str) -> Callable[..., Response]:
    """The route of an alias decision that needs nothing but the alias."""
    do = getattr(partner_aliases, action)

    def route(
        request: Request,
        alias_id: DbId,
        db: Annotated[Session, Depends(get_session)],
        clock: Annotated[Clock, Depends(get_clock)],
        auth: Annotated[AuthContext, Depends(require_partner_admin)],
    ) -> Response:
        def run() -> None:
            do(db, auth.user, alias_id, now=clock(), ip=client_ip(request))

        return _alias_action(request, db, auth, clock, run)

    route.__name__ = f"alias_{action}"
    return route


for _action in ("approve", "reject", "reactivate"):
    router.add_api_route(f"/aliases/{{alias_id}}/{_action}", _simple(_action), methods=["POST"])


@router.post("/aliases/{alias_id}/reassign")
def alias_reassign(
    request: Request,
    alias_id: DbId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
    partner_id: Annotated[str, Form()] = "",
) -> Response:
    """Approve the alias for another partner than the one the agent chose."""

    def run() -> None:
        target = parse_db_id(partner_id)
        if target is None:
            raise AliasError(partner_aliases.PARTNER_NOT_FOUND)
        partner_aliases.approve(
            db, auth.user, alias_id, partner_id=target, now=clock(), ip=client_ip(request)
        )

    return _alias_action(request, db, auth, clock, run)


@router.post("/aliases/{alias_id}/remove")
def alias_remove(
    request: Request,
    alias_id: DbId,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(require_partner_admin)],
    back: Annotated[str, Form()] = "",
) -> Response:
    """Take an approved alias out of use (it becomes rejected). Back to the partner's page, which
    also shows a refusal."""

    def run() -> None:
        partner_aliases.remove(db, auth.user, alias_id, now=clock(), ip=client_ip(request))

    origin = parse_db_id(back)
    partner = partners.get_partner(db, auth.user.agency_id, origin) if origin else None

    def error_page(message: str) -> Response:
        if partner is None:
            return _list_page(db, auth, clock, error=message, status_code=400)
        return _detail_page(db, auth, clock, partner, error=message, status_code=400)

    return _alias_action(
        request,
        db,
        auth,
        clock,
        run,
        back_to=f"/partners/{partner.id}" if partner else "/partners",
        error_page=error_page,
    )


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
