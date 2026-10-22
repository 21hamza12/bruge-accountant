"""Answering and validating comparison flags (B9): the POSTs behind the buttons of ``/flags``, the
"À valider" screen of the manager and the accountant, and the prefilled entry form for a ticket
that was not sent.

* ``POST /flags/{id}/answer`` (staff for the flags of their own signs, manager and accountant for
  any): ``choice`` and ``reason``. The service decides everything (``app/approvals.py``); a flag
  that is not the user's is a 404, like one that does not exist.
* ``GET /flags/validate``, ``POST /flags/{id}/validate``, ``POST /flags/overrides/{sale}/validate``
  (manager and accountant): approve, or reject with a reason. A validator never validates their own
  answer: the button is disabled and the server refuses with a 403 anyway.
* ``GET /flags/{id}/enter``: the entry form of the sale, filled from the report line. Staff may
  only enter a sale of today (a rule of the sale service), so for an older day the page says who
  can.

Every write counts against the user's sale budget (``ratelimit.SALE_WRITE_USER``), is CSRF-protected
on the router and refuses with a French message. Pages are ``no-store`` and carry no inline style or
script. User text (reasons) is escaped by the templates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import approvals, day_page, days, flags, permissions, sales
from app.clock import Clock
from app.errors import UserError
from app.formatting import money
from app.models import FlagDecision, ReportDay, User
from app.sales import SaleView
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, require_permission
from app.web.pages import page
from app.web.params import DbId
from app.web.routes.flags import CHOICE_LABELS, FlagRow, flag_row, list_page
from app.web.sale_forms import SaleForm, TicketForm
from app.web.sale_pages import Db, Now, Posted, form_page, throttle
from app.web.shell import ACTIVE_VALIDATE, shell_context

router = APIRouter(prefix="/flags", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
MAX_REASON = 500
APPROVE, REJECT = "approve", "reject"
BAD_DECISION = "Choisissez d'approuver ou de rejeter."
PAST_DAY = "Seul le gérant ou le comptable peut saisir un billet d'une journée passée."

Answerer = Annotated[AuthContext, Depends(require_permission(permissions.can_answer_flags))]
Validator = Annotated[AuthContext, Depends(require_permission(permissions.can_validate_money))]


def _text(posted: Posted, name: str) -> str:
    raw = posted.get(name)
    return raw[:MAX_REASON] if isinstance(raw, str) else ""


def _status_of(exc: UserError) -> int:
    """The HTTP status of a refused action; a thing that is not the user's is a 404."""
    if isinstance(exc, approvals.ApprovalNotFound | sales.SaleNotFound):
        raise HTTPException(status_code=404)
    if isinstance(exc, approvals.ApprovalForbidden | sales.SaleForbidden):
        return 403
    if isinstance(exc, days.DayClosed | sales.StaleSale | sales.SaleVoided | sales.DuplicateTicket):
        return 409
    return 400


# --- answering ------------------------------------------------------------------------------


@router.post("/{flag_id}/answer")
def flag_answer(
    flag_id: DbId, request: Request, db: Db, clock: Now, auth: Answerer, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True)
    try:
        approvals.answer_flag(
            db,
            auth.user,
            flag_id,
            _text(posted, "choice"),
            _text(posted, "reason"),
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except UserError as exc:
        db.rollback()
        status = _status_of(exc)
        return list_page(
            db,
            auth,
            clock,
            _day_of(db, auth.user, flag_id, clock),
            error=str(exc),
            status_code=status,
        )
    return RedirectResponse(
        f"/flags?date={_day_of(db, auth.user, flag_id, clock).isoformat()}", 303
    )


def _day_of(db: Session, user: User, flag_id: int, clock: Clock) -> date:
    """The day of a flag the user may see (else today), for the page to go back to."""
    found = flags.flag_for(db, user, flag_id)
    if found is None:
        return clock().date()
    row = db.get(ReportDay, found.report_day_id)
    return row.day if row is not None else clock().date()


@router.get("/{flag_id}/enter")
def flag_enter(flag_id: DbId, db: Db, clock: Now, auth: Answerer) -> Response:
    """The entry form filled from the report line of a ticket that was not entered."""
    user = auth.user
    try:
        prefill = approvals.prefill_for_not_sent(db, user, flag_id)
    except UserError as exc:
        db.rollback()
        status = _status_of(exc)
        return list_page(
            db, auth, clock, _day_of(db, user, flag_id, clock), error=str(exc), status_code=status
        )
    sees_all = permissions.sees_all_signs(user)
    if not sees_all and prefill.report_day != clock().date():
        return list_page(db, auth, clock, prefill.report_day, error=PAST_DAY, status_code=403)
    form = SaleForm(
        tickets=[
            TicketForm(
                doc_number=prefill.doc_number,
                airline=prefill.airline,
                total_doc="" if prefill.total_doc is None else money(prefill.total_doc),
                pax_name=prefill.pax_name,
                pax_type=prefill.pax_type,
                rloc=prefill.rloc,
                trnc=prefill.trnc,
            )
        ],
        staff_sign=prefill.staff_sign,
        day=prefill.report_day.isoformat() if sees_all else "",
    )
    return form_page(db, auth, clock, form)


# --- validating -----------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueFlag:
    row: FlagRow
    day: date
    answer: str  # what the agent chose
    reason: str
    by: str
    own: bool  # the viewer gave this answer: they cannot validate it


@dataclass(frozen=True)
class QueueOverride:
    sale: SaleView
    by: str
    own: bool  # the viewer wrote this fee


def _names(db: Session, user: User, ids: set[int]) -> dict[int, str]:
    return {
        i: (name or "—")
        for i, (name, _role) in day_page.actor_names(db, user.agency_id, ids).items()
    }


def validate_page(
    db: Session, auth: AuthContext, *, error: str = "", status_code: int = 200
) -> Response:
    user = auth.user
    queue = approvals.validation_queue(db, user)
    decided = approvals.decisions_for(db, user, [f.id for f in queue.flags])
    day_ids = {f.report_day_id for f in queue.flags}
    dates = {
        r.id: r.day
        for r in db.scalars(
            select(ReportDay).where(
                ReportDay.agency_id == user.agency_id, ReportDay.id.in_(day_ids)
            )
        )
    }
    agents: dict[int, FlagDecision | None] = {}
    for f in queue.flags:
        mine = [d for d in decided.get(f.id, []) if d.stage == "agent"]
        agents[f.id] = mine[-1] if mine else None
    names = _names(
        db,
        user,
        {d.actor_user_id for d in agents.values() if d is not None}
        | {v.actor_user_id for v in queue.overrides},
    )
    flag_items: list[QueueFlag] = []
    for f in queue.flags:
        given = agents[f.id]
        flag_items.append(
            QueueFlag(
                row=flag_row(f),
                day=dates.get(f.report_day_id, date.min),
                answer=CHOICE_LABELS.get(given.choice, "") if given else "",
                reason=(given.reason or "") if given else "",
                by=names.get(given.actor_user_id, "—") if given else "—",
                own=given is not None and given.actor_user_id == user.id,
            )
        )
    override_items = [
        QueueOverride(v, names.get(v.actor_user_id, "—"), v.actor_user_id == user.id)
        for v in queue.overrides
    ]
    return page(
        "flags/validate.html",
        status_code=status_code,
        headers=NO_STORE,
        error=error,
        flag_items=flag_items,
        override_items=override_items,
        **shell_context(db, auth, ACTIVE_VALIDATE),
    )


@router.get("/validate")
def flag_validate_page(db: Db, auth: Validator) -> Response:
    return validate_page(db, auth)


def _decision(posted: Posted) -> bool | None:
    word = _text(posted, "decision")
    return {APPROVE: True, REJECT: False}.get(word)


@router.post("/{flag_id}/validate")
def flag_validate(
    flag_id: DbId, request: Request, db: Db, clock: Now, auth: Validator, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True)
    approve = _decision(posted)
    if approve is None:
        return validate_page(db, auth, error=BAD_DECISION, status_code=400)
    try:
        approvals.validate_flag(
            db,
            auth.user,
            flag_id,
            approve,
            _text(posted, "reason"),
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except UserError as exc:
        db.rollback()
        return validate_page(db, auth, error=str(exc), status_code=_status_of(exc))
    return RedirectResponse("/flags/validate", status_code=303)


@router.post("/overrides/{sale_id}/validate")
def override_validate(
    sale_id: DbId, request: Request, db: Db, clock: Now, auth: Validator, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True)
    approve = _decision(posted)
    if approve is None:
        return validate_page(db, auth, error=BAD_DECISION, status_code=400)
    try:
        approvals.validate_override(
            db,
            auth.user,
            sale_id,
            approve,
            _text(posted, "reason"),
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except UserError as exc:
        db.rollback()
        return validate_page(db, auth, error=str(exc), status_code=_status_of(exc))
    return RedirectResponse("/flags/validate", status_code=303)


__all__ = ["router"]
