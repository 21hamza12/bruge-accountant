"""What the sale routes share: how a service error becomes a page, the rate limits, and the page
builders (form, read-back, sale with its history, void confirmation, day list).

``app/web/routes/sale_entry.py`` (new, preview, create, edit) and ``app/web/routes/sales.py``
(list, detail, void) only parse the request, call the service in ``app/sales.py`` and hand the
result to one of the builders here. Every page shows names and money: ``Cache-Control: no-store``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from app import audit, days, partners, permissions, reasons, sales
from app.auth import ratelimit
from app.clock import Clock
from app.errors import UserError
from app.models import DAY_CLOSED, PAX_TYPES, SALE_VOIDED, TICKET_TYPES, Partner, SaleVersion, User
from app.repository import sign_owners
from app.sales import (
    DuplicateTicket,
    FieldError,
    InvalidReason,
    InvalidSale,
    SaleForbidden,
    SaleNotFound,
    SaleView,
    SaleVoided,
    StaleSale,
    ValidatedSale,
)
from app.sales_rules import MAX_TICKETS_PER_SALE, TicketLine, ticket_number
from app.web import sale_forms
from app.web.deps import (
    AuthContext,
    client_ip,
    get_clock,
    get_session,
    require_permission,
)
from app.web.pages import page
from app.web.sale_forms import SaleForm
from app.web.shell import ACTIVE_SALE_NEW, ACTIVE_SALES, shell_context

NO_STORE = {"Cache-Control": "no-store"}
require_sales_user = require_permission(permissions.can_use_sales_screens)


async def posted_fields(request: Request) -> FormData:
    """The posted form. ``csrf_protect`` has read it already (the parse is cached on the request),
    so this costs nothing; reading it here keeps every value optional: a missing field is a French
    message, never a 422."""
    return await request.form()


Posted = Annotated[FormData, Depends(posted_fields)]
Db = Annotated[Session, Depends(get_session)]
Now = Annotated[Clock, Depends(get_clock)]
Auth = Annotated[AuthContext, Depends(require_sales_user)]


# --- what went wrong, as a page ---------------------------------------------------------------


@dataclass
class Failure:
    status: int
    message: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    duplicate: DuplicateTicket | None = None


def failure_for(exc: UserError, rows: Sequence[int] = ()) -> Failure:
    """A mistake of the user (or a refusal, or a race lost) as the status, message and per-field
    errors to show; ``rows`` maps the service's ticket index to the form row. A sale that is not
    the actor's is not a mistake: it is a 404."""
    if isinstance(exc, SaleNotFound):
        raise HTTPException(status_code=404)
    if isinstance(exc, InvalidSale):
        return Failure(400, errors=sale_forms.error_map(exc.errors, rows))
    if isinstance(exc, DuplicateTicket):
        return Failure(409, duplicate_message(exc), duplicate=exc)
    if isinstance(exc, SaleForbidden):
        return Failure(403, str(exc))
    if isinstance(exc, days.DayClosed | StaleSale | SaleVoided):
        return Failure(409, str(exc))
    if isinstance(exc, InvalidReason):
        return Failure(400, errors={"reason": str(exc)})
    return Failure(400, str(exc))


def duplicate_message(duplicate: DuplicateTicket) -> str:
    """What the user is told about a ticket already sold. Only when the actor may see that sale
    is there a sale to point at; otherwise nothing but the fact (no agent, client or amount)."""
    if duplicate.sale_id is None:
        return f"Ce billet est déjà enregistré par un autre agent ({duplicate.doc_number})."
    return f"Ce billet est déjà enregistré dans une autre vente ({duplicate.doc_number})."


def stale_or_garbled(form: SaleForm, view: SaleView) -> Failure | None:
    """The form was built on another version of the sale than the one stored now, or carries no
    usable version at all (a missing version is never skipped: it is refused like a garbled one)."""
    version = sale_forms.parse_version(form.version_no)
    if version is None:
        return Failure(400, sale_forms.BAD_VERSION)
    if version != view.version_no:
        return Failure(409, sales.STALE)
    return None


def reason_problem(user: User, raw: str) -> FieldError | None:
    """The reason of an edit: required for manager and accountant, optional (but valid when given)
    for staff editing their own sale."""
    try:
        if permissions.can_edit_any_entry(user):
            reasons.required_reason(raw)
        else:
            reasons.clean_reason(raw)
    except reasons.ReasonError as exc:
        return FieldError("reason", str(exc))
    return None


# --- rate limits ------------------------------------------------------------------------------


def _too_many(
    db: Session,
    request: Request,
    auth: AuthContext,
    clock: Clock,
    limit: ratelimit.Limit,
    decision: ratelimit.RateDecision,
) -> HTTPException:
    """The 429. Only the first refusal of a window is a database row."""
    ip = client_ip(request)
    details = {"scope": limit.name}
    if decision.first_rejection:
        audit.record_and_commit(
            db,
            audit.RATE_LIMITED,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=ip,
            details=details,
            now=clock(),
        )
    else:
        audit.log_only(
            audit.RATE_LIMITED,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=ip,
            details=details,
        )
    return HTTPException(status_code=429, headers={"Retry-After": str(decision.retry_after)})


def throttle(
    db: Session,
    request: Request,
    auth: AuthContext,
    clock: Clock,
    *,
    write: bool,
    limit: ratelimit.Limit | None = None,
) -> None:
    """Count this request against the user's budget (committed at once, so a failing request still
    counts) and raise the 429 when it is spent: ``SALE_WRITE_USER`` for a save, edit or void,
    the looser ``SALE_PREVIEW_USER`` for a read-back. The limit is read at call time."""
    if limit is None:
        limit = ratelimit.SALE_WRITE_USER if write else ratelimit.SALE_PREVIEW_USER
    decision = ratelimit.hit(db, limit, str(auth.user.id), clock())
    db.commit()
    if not decision.allowed:
        raise _too_many(db, request, auth, clock, limit, decision)


# --- lookups ----------------------------------------------------------------------------------


def get_sale(db: Session, auth: AuthContext, sale_id: int) -> SaleView:
    """The sale as the actor may see it, else a 404 (it does not exist, is another agency's, or is
    another sign's for staff)."""
    view = sales.get_sale_for(db, auth.user, sale_id)
    if view is None:
        raise HTTPException(status_code=404)
    return view


def go_to(sale_id: int) -> Response:
    return RedirectResponse(f"/sales/{sale_id}", status_code=303)


def _partner_options(db: Session, agency_id: int, today: date, keep: int | None) -> list[Partner]:
    """The partners a sale can name: the active ones, plus the one this sale already has."""
    return [
        row.partner
        for row in partners.list_partners(db, agency_id, today=today)
        if row.partner.is_active or row.partner.id == keep
    ]


def _sign_choice(db: Session, user: User) -> tuple[str, list[str]]:
    """How the form asks for the sign: ``free`` (manager, accountant: any sign, with the known ones
    suggested), ``select`` (staff with several), ``fixed`` (staff with one), ``none``."""
    own = sales.visible_signs(db, user)
    if own is None:
        return "free", sorted(sign_owners(db, user.agency_id))
    if not own:
        return "none", []
    return ("fixed" if len(own) == 1 else "select"), own


def _payment(method: str) -> str:
    return sale_forms.PAYMENT_NAME.get(method, method)


def _duplicate_rows(form: SaleForm, duplicate: DuplicateTicket) -> list[int]:
    """The form rows whose ticket number (as the service reads it) is the one already sold."""
    found: list[int] = []
    for index, row in enumerate(form.tickets):
        number, _ = ticket_number(row.doc_number, row.airline.strip().upper())
        if number == duplicate.doc_number:
            found.append(index)
    return found


# --- page builders ----------------------------------------------------------------------------


def form_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    form: SaleForm,
    *,
    sale: SaleView | None = None,
    failure: Failure | None = None,
    problems: Sequence[FieldError] = (),
    rows: Sequence[int] = (),
    status_code: int = 200,
) -> Response:
    """The entry form (``sale`` None) or the edit form of ``sale``, filled with ``form``.

    ``failure`` is what the service refused (its message goes in the banner; its field errors,
    and a duplicate ticket on the row that holds it, next to the fields); ``problems`` are
    mistakes found before the service ran, with ``rows`` mapping their ticket index to the form
    row. The error summary comes first in the page, each line linking to its input. The status is
    the failure's unless ``status_code`` says otherwise."""
    user = auth.user
    today = clock().date()
    mode, signs = _sign_choice(db, user)
    errors = dict(failure.errors) if failure else {}
    errors.update(sale_forms.error_map(problems, rows))
    if failure and failure.duplicate is not None:
        for index in _duplicate_rows(form, failure.duplicate):
            errors.setdefault(f"t{index}.doc_number", failure.message)
    if mode == "fixed" and not form.staff_sign:
        form.staff_sign = signs[0]
    keep = sale.partner_id if sale is not None else None
    return page(
        "sales/form.html",
        status_code=failure.status if failure and status_code == 200 else status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_SALE_NEW if sale is None else ACTIVE_SALES),
        f=form,
        sale=sale,
        action="/sales/preview" if sale is None else f"/sales/{sale.id}/preview-edit",
        back_url="/sales" if sale is None else f"/sales/{sale.id}",
        errors=errors,
        summary=sale_forms.error_summary(errors),
        banner=failure.message if failure else "",
        duplicate=failure.duplicate if failure else None,
        partners=_partner_options(db, user.agency_id, today, keep),
        payments=sale_forms.PAYMENT_LABELS,
        sign_mode=mode,
        signs=signs,
        choose_day=permissions.can_edit_any_entry(user),
        needs_reason=sale is not None and permissions.can_edit_any_entry(user),
        today=today,
        max_tickets=MAX_TICKETS_PER_SALE,
        can_add_row=len(form.tickets) < MAX_TICKETS_PER_SALE,
        pax_types=PAX_TYPES,
        ticket_types=TICKET_TYPES,
    )


IGNORED_PRICE = "Prix saisi ignoré : prix partenaire calculé."


def readback_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    form: SaleForm,
    result: ValidatedSale,
    *,
    sale: SaleView | None = None,
) -> Response:
    """The read-back of a validated sale: what will be stored, priced by the server. Its two forms
    (confirm, and back to the form) carry the posted values in hidden fields, nothing computed."""
    user = auth.user
    _, signs = _sign_choice(db, user)
    chosen = form.staff_sign.strip().upper()
    if sale is not None:
        sign = chosen or sale.staff_sign
    elif chosen:
        sign = chosen
    else:
        sign = signs[0] if len(signs) == 1 else ""
    day = sale.day if sale is not None else None
    if day is None:
        day = sale_forms.parse_day(form.day)[0] if permissions.can_edit_any_entry(user) else None
    notes = list(sales.sign_warnings(db, user, sign)) if sign else []
    if result.partner_id is not None and form.pb.strip():
        notes.append(IGNORED_PRICE)  # a typed price on a partner sale is not silently dropped
    return page(
        "sales/readback.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_SALE_NEW if sale is None else ACTIVE_SALES),
        f=form,
        sale=sale,
        v=result,
        notes=notes,
        profit=sales.sale_profit(result),
        sign=sign,
        day=day or clock().date(),
        payment=_payment(result.payment_method),
        pairs=form.hidden_pairs,
        confirm_action="/sales" if sale is None else f"/sales/{sale.id}",
        back_action="/sales/new" if sale is None else f"/sales/{sale.id}/edit",
    )


def _actors(db: Session, agency_id: int, ids: set[int]) -> dict[int, tuple[str | None, str]]:
    """``{user id: (display name or None, role)}``. Never a phone number: a person without a
    display name is shown by their role."""
    rows = db.execute(
        select(User.id, User.display_name, User.role).where(
            User.agency_id == agency_id, User.id.in_(ids)
        )
    )
    return {row.id: ((row.display_name or "").strip() or None, row.role) for row in rows}


def detail_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    view: SaleView,
    *,
    error: str = "",
    status_code: int = 200,
) -> Response:
    """A sale with its warnings and its history, newest version first (who, when, what changed,
    why). The edit and void buttons show only when the sale can still be changed."""
    user = auth.user
    history = sales.sale_history_for(db, user, view.id)
    previous: dict[int, SaleVersion | None] = {}
    last: SaleVersion | None = None
    for version in history:
        previous[version.version_no] = last
        last = version
    actors = _actors(db, user.agency_id, {v.actor_user_id for v in history})
    report_day = days.get_day(db, user.agency_id, view.day)
    closed = report_day is not None and report_day.status == DAY_CLOSED
    editable = view.status != SALE_VOIDED and not closed
    if not permissions.can_edit_any_entry(user):
        editable = editable and view.day == clock().date()
    return page(
        "sales/detail.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_SALES),
        s=view,
        profit=sales.sale_profit(view),
        payment=_payment(view.payment_method),
        warnings=sales.view_warnings(db, user, view) if view.status != SALE_VOIDED else (),
        versions=[
            {
                "v": v,
                "name": actors.get(v.actor_user_id, (None, ""))[0],
                "role": actors.get(v.actor_user_id, (None, ""))[1],
                "changes": sale_forms.changes(previous[v.version_no], v),
                "pay": _payment(v.payment_method),
            }
            for v in reversed(history)
        ],
        editable=editable,
        day_closed=closed,
        error=error,
    )


def void_page(
    db: Session,
    auth: AuthContext,
    view: SaleView,
    *,
    reason: str = "",
    error: str = "",
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    return page(
        "sales/void.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_SALES),
        s=view,
        reason=reason,
        error=error,
        errors=errors or {},
    )


def list_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    day: date,
    views: Sequence[SaleView],
    sign: str,
) -> Response:
    return page(
        "sales/list.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_SALES),
        day=day,
        today=clock().date(),
        rows=[
            {"s": v, "profit": sales.sale_profit(v), "payment": _payment(v.payment_method)}
            for v in views
        ],
        totals=sales.day_totals(views),
        sign=sign,
        known_signs=(
            sorted(sign_owners(db, auth.user.agency_id))
            if permissions.sees_all_signs(auth.user)
            else []
        ),
    )


def docs_of(form: SaleForm) -> set[str]:
    """The ticket numbers of the non-blank rows of a form, as the service reads them."""
    found: set[str] = set()
    for row in form.tickets:
        if row.is_blank:
            continue
        number, _ = ticket_number(row.doc_number, row.airline.strip().upper())
        if number is not None:
            found.add(number)
    return found


def numbers_of(lines: Sequence[TicketLine]) -> set[str]:
    return {line.doc_number for line in lines}
