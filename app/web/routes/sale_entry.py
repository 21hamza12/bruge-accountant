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

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import permissions, sales
from app.errors import UserError
from app.models import User
from app.sales import DuplicateTicket, SaleView
from app.web import sale_forms
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
    detail_page,
    docs_of,
    failure_for,
    form_page,
    get_sale,
    go_to,
    numbers_of,
    readback_page,
    reason_problem,
    stale_or_garbled,
    throttle,
)

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
    try:
        created = sales.create_sale(
            db, user, built.data, day=built.day, now=clock(), ip=client_ip(request)
        )
        db.commit()
    except DuplicateTicket as exc:
        db.rollback()
        again = _just_created(db, user, exc, form, clock())
        if again is not None:
            return go_to(again.id)
        return form_page(db, auth, clock, form, failure=failure_for(exc, built.rows))
    except UserError as exc:
        db.rollback()
        return form_page(db, auth, clock, form, failure=failure_for(exc, built.rows))
    return go_to(created.sale.id)


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
    return form_page(db, auth, clock, sale_forms.read(posted), sale=view)


@router.post("/{sale_id}/preview-edit")
def sale_preview_edit(
    sale_id: DbId, request: Request, db: Db, clock: Now, auth: Auth, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=False)
    view = get_sale(db, auth, sale_id)
    form = sale_forms.read(posted)
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
    form = sale_forms.read(posted)
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
