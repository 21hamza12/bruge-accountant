"""The sale pages that are not the entry form: the day's list, a sale with its history, and the
void confirmation. The entry, read-back, confirm and edit routes are in ``sale_entry.py``; the
shared logic (errors as pages, rate limits, page builders) in ``app/web/sale_pages.py``.

Same rules as the entry routes: CSRF on the router, ``can_use_sales_screens``, the agency from the
session, another agency's or (staff) another sign's sale is a 404, no-store pages.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import Response

from app import accounts, reasons, sales
from app.errors import UserError
from app.models import SALE_VOIDED
from app.web import sale_forms
from app.web.csrf import csrf_protect
from app.web.deps import client_ip
from app.web.params import DayParam, DbId, parse_day
from app.web.sale_pages import (
    Auth,
    Db,
    Now,
    Posted,
    detail_page,
    failure_for,
    get_sale,
    go_to,
    list_page,
    throttle,
    void_page,
)

router = APIRouter(prefix="/sales", dependencies=[Depends(csrf_protect)])

MAX_SIGN_FILTER = 32


@router.get("")
def sale_list(
    db: Db,
    clock: Now,
    auth: Auth,
    day: DayParam = None,
    sign: Annotated[str | None, Query(max_length=MAX_SIGN_FILTER)] = None,
) -> Response:
    chosen = parse_day(day) or clock().date()
    wanted: list[str] | None = None
    if sign and sign.strip():
        try:
            wanted = [accounts.normalize_staff_sign(sign)]
        except accounts.AccountError:
            wanted = [sign.strip()]
    views = sales.sales_for_day_for(db, auth.user, chosen, signs=wanted)
    return list_page(db, auth, clock, chosen, views, wanted[0] if wanted else "")


@router.get("/{sale_id}")
def sale_detail(sale_id: DbId, db: Db, clock: Now, auth: Auth) -> Response:
    return detail_page(db, auth, clock, get_sale(db, auth, sale_id))


@router.get("/{sale_id}/void")
def sale_confirm_void(sale_id: DbId, db: Db, clock: Now, auth: Auth) -> Response:
    """The confirmation step: nothing changes until the form on this page is posted."""
    view = get_sale(db, auth, sale_id)
    if view.status == SALE_VOIDED:
        return detail_page(db, auth, clock, view, error=sales.VOIDED, status_code=409)
    return void_page(db, auth, view)


@router.post("/{sale_id}/void")
def sale_void(
    sale_id: DbId, request: Request, db: Db, clock: Now, auth: Auth, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True)
    view = get_sale(db, auth, sale_id)
    raw = posted.get("reason")
    reason = raw[: sale_forms.CLIP] if isinstance(raw, str) else ""
    raw_version = posted.get("version_no")
    # The version the confirmation page was built on is required: no version, no stale check.
    version = sale_forms.parse_version(raw_version) if isinstance(raw_version, str) else None
    if version is None:
        return detail_page(db, auth, clock, view, error=sale_forms.BAD_VERSION, status_code=400)
    try:
        reasons.required_reason(reason)
    except reasons.ReasonError as exc:
        return void_page(
            db, auth, view, reason=reason, errors={"reason": str(exc)}, status_code=400
        )
    try:
        sales.void_sale(
            db,
            auth.user,
            view.id,
            reason,
            now=clock(),
            ip=client_ip(request),
            expected_version=version,
        )
        db.commit()
    except UserError as exc:
        db.rollback()
        failure = failure_for(exc)
        return detail_page(db, auth, clock, view, error=failure.message, status_code=failure.status)
    return go_to(view.id)


__all__ = ["router"]
