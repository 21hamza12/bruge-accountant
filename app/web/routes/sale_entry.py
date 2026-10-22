"""The sale entry screens: the form, the read-back, the confirm, and the same three for an edit.

* **Who**: staff for their own signs, manager and accountant for the agency
  (``permissions.can_use_sales_screens``; the rules for each sale are the service's, in
  ``app/sales.py``). The agency is always the session's. A sale of another agency, or (staff) of
  another sign, is a 404 page, like one that does not exist.
* **Read-back, then confirm**: ``POST /sales/preview`` (and ``/sales/{id}/preview-edit``) call the
  service's ``preview_sale`` and write nothing. The page carries every posted value in hidden
  fields; ``POST /sales`` (and ``/sales/{id}``) take them as input to be validated and priced
  again by the service. Nothing computed travels, and nothing posted is believed: the agency, the
  sale id and any price of a partner sale are ignored or recomputed.
* **Mistakes**: a ``UserError`` of the service becomes a French message on the form (field errors
  next to the fields), never a 500: 400 for a mistake in the data, 403 for a refusal, 409 when the
  world moved (a duplicate ticket, a closed day, a sale changed meanwhile). The session is rolled
  back first.
* **Edits are built on a version**: the edit form carries ``version_no``; a missing, garbled or
  old one is refused ("rechargez la page"; the service checks again under its lock).
* **Double submit**: confirming twice (a double tap, a reload of the POST) finds its own sale again
  and redirects to it instead of reporting a duplicate (``_just_created``).
* Sale writes share one rate limit per user (``ratelimit.SALE_WRITE_USER``), read-backs a looser
  one (``SALE_PREVIEW_USER``). A client's name or phone is never logged or audited.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import partner_aliases, partners, permissions, sales
from app.errors import UserError
from app.models import User
from app.parsing.pnr_sales import ProposedSale
from app.sales import DuplicateTicket, SaleView
from app.web import sale_forms, sale_paste
from app.web.csrf import csrf_protect
from app.web.deps import client_ip
from app.web.params import DbId
from app.web.sale_forms import SaleForm
from app.web.sale_pages import (
    Auth,
    Db,
    Failure,
    Now,
    Posted,
    choose_page,
    detail_page,
    docs_of,
    failure_for,
    form_page,
    get_sale,
    go_to,
    numbers_of,
    readback_page,
    reason_problem,
    sign_choice,
    stale_or_garbled,
    throttle,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/sales", dependencies=[Depends(csrf_protect)])

DOUBLE_SUBMIT_WINDOW = timedelta(seconds=120)


def _just_created(
    db: Session, user: User, duplicate: DuplicateTicket, form: SaleForm, now: datetime
) -> SaleView | None:
    """The sale this very user created a moment ago with exactly these tickets, when ``duplicate``
    points at it: the second submit of the same confirmation. Only ever a redirect target; nothing
    is written."""
    if duplicate.sale_id is None:
        return None
    view = sales.get_sale_for(db, user, duplicate.sale_id)
    if view is None or view.created_by_user_id != user.id or view.version_no != 1:
        return None
    created = view.created_at if view.created_at.tzinfo else view.created_at.replace(tzinfo=UTC)
    if abs(now - created) > DOUBLE_SUBMIT_WINDOW:
        return None
    return view if numbers_of(view.tickets) == docs_of(form) else None


# --- new sale ---------------------------------------------------------------------------------


@router.get("/new")
def sale_new(db: Db, clock: Now, auth: Auth) -> Response:
    return form_page(db, auth, clock, SaleForm())


@router.post("/new")
def sale_new_again(db: Db, clock: Now, auth: Auth, posted: Posted) -> Response:
    """Back from the read-back: the form with the values typed, nothing validated."""
    return form_page(db, auth, clock, sale_forms.read(posted))


@router.post("/paste")
def sale_paste_text(request: Request, db: Db, clock: Now, auth: Auth, posted: Posted) -> Response:
    """Read pasted Amadeus text and show what it proposes: the prefilled form (one passenger) or
    the list of proposed sales (several). Nothing is written, and the text is neither stored nor
    logged nor audited: it is read here and dropped."""
    throttle(db, request, auth, clock, write=False)
    raw = posted.get(sale_paste.TEXT_FIELD)
    error = sale_paste.check_text(raw)
    if error:
        return form_page(db, auth, clock, SaleForm(), paste_error=error, status_code=400)
    result = sale_paste.read_paste(db, auth.user.agency_id, raw if isinstance(raw, str) else "")
    if not result.proposals:
        message = " ".join([result.error, *result.reader_warnings])
        return form_page(db, auth, clock, SaleForm(), paste_error=message, status_code=400)
    mode, signs = sign_choice(db, auth.user)
    for proposal in result.proposals:
        _prefill_sign(proposal, mode, signs)
    if len(result.proposals) == 1:
        return form_page(db, auth, clock, result.proposals[0].form)
    names = {p.id: p.name for p in partners.list_active(db, auth.user.agency_id)}
    items = [
        {"sale": p.sale, "pairs": p.form.hidden_pairs, "agency": _agency_label(p.sale, names)}
        for p in result.proposals
    ]
    return choose_page(db, auth, items, result.reader_warnings)


def _agency_label(sale: ProposedSale, names: dict[int, str]) -> str:
    """The agency a proposal suggests, in words (the agent confirms it on the form)."""
    if sale.ag_kind == "direct":
        return "BRUGE (vente directe)"
    if sale.ag_kind == "partner" and sale.partner_id in names:
        return names[sale.partner_id]
    if sale.suggested_partner_id in names:
        return f"suggestion : {names[sale.suggested_partner_id]}"
    return "à choisir"


def _prefill_sign(proposal: sale_paste.Proposal, mode: str, signs: list[str]) -> None:
    """The RP sign is a hint. Staff only ever enter their own sign (the form fixes or offers
    theirs); a manager or accountant gets the known sign it stands for."""
    form, rp = proposal.form, proposal.rp_sign
    mine = sale_paste.matching_sign(rp, signs)
    # for "fixed" the form page fills in their own sign; otherwise only a sign they may use
    form.staff_sign = mine if mine and mode in ("select", "free") else ""
    if rp and mine is None:
        form.notices.append(f"Signe du PNR : {rp[:8]}. Vérifiez le signe de la vente.")


@router.post("/preview")
def sale_preview(request: Request, db: Db, clock: Now, auth: Auth, posted: Posted) -> Response:
    throttle(db, request, auth, clock, write=False)
    form = sale_forms.read(posted)
    if form.add_row:
        return form_page(db, auth, clock, sale_forms.with_extra_row(form))
    user = auth.user
    built = sale_forms.to_input(form, choose_day=permissions.can_edit_any_entry(user))
    if built.problems:
        return form_page(
            db, auth, clock, form, problems=built.problems, rows=built.rows, status_code=400
        )
    try:
        result = sales.preview_sale(db, user, built.data, day=built.day, now=clock())
    except UserError as exc:
        db.rollback()
        return form_page(db, auth, clock, form, failure=failure_for(exc, built.rows))
    if isinstance(result, list):
        failure = Failure(400, errors=sale_forms.error_map(result, built.rows))
        return form_page(db, auth, clock, form, failure=failure)
    return readback_page(db, auth, clock, form, result)


@router.post("")
def sale_create(request: Request, db: Db, clock: Now, auth: Auth, posted: Posted) -> Response:
    throttle(db, request, auth, clock, write=True)
    form = sale_forms.read(posted)
    user = auth.user
    built = sale_forms.to_input(form, choose_day=permissions.can_edit_any_entry(user))
    if built.problems:
        return form_page(
            db, auth, clock, form, problems=built.problems, rows=built.rows, status_code=400
        )
    now, ip = clock(), client_ip(request)
    try:
        created = sales.create_sale(
            db, user, built.data, day=built.day, now=now, ip=ip, source=form.source
        )
        _propose_alias(db, user, form, built.data.partner_id, now, ip)
        db.commit()
    except DuplicateTicket as exc:
        db.rollback()
        again = _just_created(db, user, exc, form, now)
        if again is not None:
            return go_to(again.id)
        return form_page(db, auth, clock, form, failure=failure_for(exc, built.rows))
    except UserError as exc:
        db.rollback()
        return form_page(db, auth, clock, form, failure=failure_for(exc, built.rows))
    return go_to(created.sale.id)


def _propose_alias(
    db: Session, user: User, form: SaleForm, partner_id: int | None, now: datetime, ip: str | None
) -> None:
    """A sale from pasted text whose agency did not match exactly, confirmed with a partner: the
    agency text becomes a **pending** alias of that partner (never matching until a manager
    approves it). Never blocks the sale."""
    # an exact match is already known; a payment hint (FP) can be a client's name: no alias
    if not form.from_paste or form.ag_match in ("exact", "fp") or partner_id is None:
        return
    if not form.ag_text.strip():
        return
    try:
        with db.begin_nested():  # its own savepoint: a failure here never touches the sale
            partner_aliases.propose(db, user, partner_id, form.ag_text, now=now, ip=ip)
    except Exception:  # noqa: BLE001 - an alias is a convenience, never a reason to fail
        log.warning("alias proposal failed", extra={"partner_id": partner_id})  # never the text


# --- edit -------------------------------------------------------------------------------------


@router.get("/{sale_id}/edit")
def sale_edit(sale_id: DbId, db: Db, clock: Now, auth: Auth) -> Response:
    view = get_sale(db, auth, sale_id)
    form = sale_forms.from_view(view)
    built = sale_forms.to_input(form, choose_day=False)
    try:  # the service's own access rules (voided, closed day, today only), without writing
        sales.preview_sale(db, auth.user, built.data, sale_id=view.id, now=clock())
    except UserError as exc:
        db.rollback()
        failure = failure_for(exc)
        return detail_page(db, auth, clock, view, error=failure.message, status_code=failure.status)
    return form_page(db, auth, clock, form, sale=view)


@router.post("/{sale_id}/edit")
def sale_edit_again(sale_id: DbId, db: Db, clock: Now, auth: Auth, posted: Posted) -> Response:
    """Back from the read-back of an edit: the form with the values typed, nothing validated."""
    view = get_sale(db, auth, sale_id)
    return form_page(db, auth, clock, sale_forms.read(posted, new_sale=False), sale=view)


@router.post("/{sale_id}/preview-edit")
def sale_preview_edit(
    sale_id: DbId, request: Request, db: Db, clock: Now, auth: Auth, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=False)
    view = get_sale(db, auth, sale_id)
    form = sale_forms.read(posted, new_sale=False)
    if form.add_row:
        return form_page(db, auth, clock, sale_forms.with_extra_row(form), sale=view)
    stale = stale_or_garbled(form, view)
    if stale is not None:
        return detail_page(db, auth, clock, view, error=stale.message, status_code=stale.status)
    user = auth.user
    built = sale_forms.to_input(form, choose_day=False)
    problems = list(built.problems)
    problem = reason_problem(user, form.reason)
    if problem is not None:
        problems.append(problem)
    if problems:
        return form_page(
            db, auth, clock, form, sale=view, problems=problems, rows=built.rows, status_code=400
        )
    try:
        result = sales.preview_sale(db, user, built.data, sale_id=view.id, now=clock())
    except UserError as exc:
        db.rollback()
        return form_page(db, auth, clock, form, sale=view, failure=failure_for(exc, built.rows))
    if isinstance(result, list):
        failure = Failure(400, errors=sale_forms.error_map(result, built.rows))
        return form_page(db, auth, clock, form, sale=view, failure=failure)
    return readback_page(db, auth, clock, form, result, sale=view)


@router.post("/{sale_id}")
def sale_update(
    sale_id: DbId, request: Request, db: Db, clock: Now, auth: Auth, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True)
    view = get_sale(db, auth, sale_id)
    form = sale_forms.read(posted, new_sale=False)
    stale = stale_or_garbled(form, view)
    if stale is not None:
        return detail_page(db, auth, clock, view, error=stale.message, status_code=stale.status)
    user = auth.user
    built = sale_forms.to_input(form, choose_day=False)
    problem = reason_problem(user, form.reason)
    if problem is not None:
        return form_page(db, auth, clock, form, sale=view, problems=[problem], status_code=400)
    try:
        sales.edit_sale(
            db,
            user,
            view.id,
            built.data,
            form.reason,
            now=clock(),
            ip=client_ip(request),
            expected_version=view.version_no,
        )
        db.commit()
    except UserError as exc:
        db.rollback()
        return form_page(db, auth, clock, form, sale=view, failure=failure_for(exc, built.rows))
    return go_to(view.id)


__all__ = ["router"]
