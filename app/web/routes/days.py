"""The day pages: ``GET /days`` (recent days) and ``GET /days/{yyyy-mm-dd}`` (the app's own TJQ
AVANT of one day: its tickets grouped by sale, the totals, the status and its history, and whether
an Amadeus report was received), with the close and reopen actions.

Same rules as the sale pages: CSRF on the router, the agency from the session, ``no-store``, French
text. Staff see only the figures and lines of their own signs and no action; manager and accountant
see everything; closing needs ``can_close_day`` (manager, accountant) and reopening
``can_reopen_day`` (manager, with a reason), both refused with a 403 and a ``permission_denied``
audit event for anybody else. A date without a page (malformed, in the future, over 5 years back)
is a 400 with a French message; a day nobody wrote to yet is an empty page, not an error.
The comparison with the Amadeus report is B8: here an upload is only "received" or "not received".
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import day_page, days, permissions, reasons, sales
from app.auth import ratelimit
from app.clock import Clock
from app.models import DAY_CLOSED, ReportDay
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, require_permission
from app.web.pages import page
from app.web.params import parse_day
from app.web.sale_pages import Db, Now, Posted, throttle
from app.web.shell import ACTIVE_DAYS, shell_context

router = APIRouter(prefix="/days", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
BAD_DATE = "Date invalide."
MAX_REASON = 500

Viewer = Annotated[AuthContext, Depends(require_permission(permissions.can_use_sales_screens))]
Closer = Annotated[AuthContext, Depends(require_permission(permissions.can_close_day))]
Reopener = Annotated[AuthContext, Depends(require_permission(permissions.can_reopen_day))]


def _day_or_error(raw: str, clock: Clock) -> tuple[date | None, str]:
    """The date of the path, or the French reason there is no page for it."""
    try:
        parsed = parse_day(raw)
    except HTTPException:
        return None, BAD_DATE
    if parsed is None:  # pragma: no cover - a path segment is never empty
        return None, BAD_DATE
    try:
        days.check_day(parsed, clock().date())
    except days.DayError as exc:
        return None, str(exc)
    return parsed, ""


def _error_page(db: Session, auth: AuthContext, message: str) -> Response:
    return page(
        "days/error.html",
        status_code=400,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_DAYS),
        error=message,
    )


def _existing(db: Session, auth: AuthContext, day: date) -> ReportDay:
    found = days.get_day(db, auth.user.agency_id, day)
    if found is None:
        raise HTTPException(status_code=404)
    return found


def day_view(
    db: Session,
    auth: AuthContext,
    day: date,
    *,
    error: str = "",
    status_code: int = 200,
) -> Response:
    """The day page of ``day`` for the signed-in user.

    Everyone gets the lines and totals of the signs they may see, the status and whether an Amadeus
    report was received. Only manager and accountant (``sees_all_signs``) also get the upload's
    file, time, uploader and counts and the day's events (who, when, why): for staff that data is
    not read at all. A closed day shows its date to staff, nothing more."""
    user = auth.user
    sees_all = permissions.sees_all_signs(user)
    report_day = days.get_day(db, user.agency_id, day)
    views = sales.sales_for_day_for(db, user, day)
    events = []
    upload = None
    names: dict[int, tuple[str | None, str]] = {}
    if sees_all:
        events = days.day_events(db, user.agency_id, report_day.id) if report_day else []
        upload = day_page.upload_info(db, user, day)
        ids = {e.actor_user_id for e in events}
        if upload is not None and upload.upload.uploaded_by_user_id is not None:
            ids.add(upload.upload.uploaded_by_user_id)
        names = day_page.actor_names(db, user.agency_id, ids)
    received = upload is not None if sees_all else day_page.report_received(db, user.agency_id, day)
    status = report_day.status if report_day else ""
    closed = status == DAY_CLOSED
    uploader = None
    if upload is not None and upload.upload.uploaded_by_user_id is not None:
        uploader = names.get(upload.upload.uploaded_by_user_id)
    return page(
        "days/show.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_DAYS),
        error=error,
        day=day,
        status=status,
        status_label=day_page.status_label(status),
        closed_on=report_day.closed_at.date()
        if closed and report_day and report_day.closed_at
        else None,
        groups=day_page.groups_of(views),
        totals=sales.day_totals(views),
        signs=day_page.sign_totals(views),
        received=received,
        events=[{"e": e, "actor": names.get(e.actor_user_id)} for e in events],
        upload=upload,
        uploader=uploader,
        can_close=report_day is not None and not closed and permissions.can_close_day(user),
        can_reopen=closed and permissions.can_reopen_day(user),
    )


@router.get("")
def day_list(db: Db, auth: Viewer) -> Response:
    return page(
        "days/list.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_DAYS),
        rows=[
            {"r": row, "label": day_page.status_label(row.status)}
            for row in day_page.recent_days(db, auth.user)
        ],
    )


@router.get("/{day}")
def day_show(day: str, db: Db, clock: Now, auth: Viewer) -> Response:
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    return day_view(db, auth, chosen)


@router.get("/{day}/close")
def day_confirm_close(day: str, db: Db, clock: Now, auth: Closer) -> Response:
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = _existing(db, auth, chosen)
    if report_day.status == DAY_CLOSED:
        return day_view(db, auth, chosen, error=days.CLOSED, status_code=409)
    views = sales.sales_for_day_for(db, auth.user, chosen)
    return page(
        "days/close.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_DAYS),
        day=chosen,
        totals=sales.day_totals(views),
    )


@router.post("/{day}/close")
def day_close(day: str, request: Request, db: Db, clock: Now, auth: Closer) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.DAY_ACTION_USER)
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = _existing(db, auth, chosen)
    try:
        days.close_day(db, auth.user, report_day, now=clock(), ip=client_ip(request))
        db.commit()
    except days.DayError as exc:
        db.rollback()
        return day_view(db, auth, chosen, error=str(exc), status_code=409)
    return RedirectResponse(f"/days/{chosen.isoformat()}", status_code=303)


def _reopen_page(
    db: Session,
    auth: AuthContext,
    day: date,
    *,
    reason: str = "",
    error: str = "",
    status: int = 200,
) -> Response:
    return page(
        "days/reopen.html",
        status_code=status,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_DAYS),
        day=day,
        reason=reason,
        error=error,
    )


@router.get("/{day}/reopen")
def day_confirm_reopen(day: str, db: Db, clock: Now, auth: Reopener) -> Response:
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = _existing(db, auth, chosen)
    if report_day.status != DAY_CLOSED:
        return day_view(db, auth, chosen, error=days.NOT_CLOSED, status_code=409)
    return _reopen_page(db, auth, chosen)


@router.post("/{day}/reopen")
def day_reopen(
    day: str, request: Request, db: Db, clock: Now, auth: Reopener, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.DAY_ACTION_USER)
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = _existing(db, auth, chosen)
    raw = posted.get("reason")
    reason = raw[:MAX_REASON] if isinstance(raw, str) else ""
    try:
        reasons.required_reason(reason)
    except reasons.ReasonError as exc:
        return _reopen_page(db, auth, chosen, reason=reason, error=str(exc), status=400)
    try:
        days.reopen_day(db, auth.user, report_day, reason, now=clock(), ip=client_ip(request))
        db.commit()
    except days.DayError as exc:
        db.rollback()
        return day_view(db, auth, chosen, error=str(exc), status_code=409)
    return RedirectResponse(f"/days/{chosen.isoformat()}", status_code=303)


__all__ = ["router"]
