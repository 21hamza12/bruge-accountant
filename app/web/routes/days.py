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

B9: closing goes through ``approvals.close_day`` (the close gate). A blocked close is a 409 whose
page lists what blocks (a read-only preview ``approvals.gate_status`` also shows it before anybody
tries). The comparison may be run again by the gate, so a blocked close still commits. Whoever may
validate money records "J'ai vu la liste" for the partner tickets that are not in the report.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import approvals, audit, day_page, days, export_apres, flags, permissions, reasons, sales
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
BAD_VERSION = "Version invalide."
NOT_CLOSED_YET = "Journée non clôturée."
NO_STORED_EXPORT = "Aucun export enregistré pour cette journée."
UNKNOWN_VERSION = "Version introuvable."
CLOSED_USE_STORED = "Cette journée est clôturée : téléchargez la version enregistrée."
VERSION = re.compile(r"[0-9]{1,6}")  # ASCII digits only

Viewer = Annotated[AuthContext, Depends(require_permission(permissions.can_use_sales_screens))]
Closer = Annotated[AuthContext, Depends(require_permission(permissions.can_close_day))]
Validator = Annotated[AuthContext, Depends(require_permission(permissions.can_validate_money))]
BLOCKED = "La journée ne peut pas être clôturée."
Exporter = Annotated[AuthContext, Depends(require_permission(permissions.can_export_day))]
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


def _error_page(db: Session, auth: AuthContext, message: str, status_code: int = 400) -> Response:
    return page(
        "days/error.html",
        status_code=status_code,
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
    show_profit = permissions.can_see_profit(user)  # the manager only: not computed for others
    show_totals = permissions.can_see_totals(user)  # manager and accountant: not for staff
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
    exportable = sees_all and permissions.can_export_day(user)
    exports = (
        export_apres.export_history(db, user.agency_id, report_day.id)
        if exportable and report_day is not None
        else []
    )
    uploader = None
    if upload is not None and upload.upload.uploaded_by_user_id is not None:
        uploader = names.get(upload.upload.uploaded_by_user_id)
    # The comparison with the Amadeus report (B8): the numbers this viewer may see.
    summary = flags.day_summary(db, user, report_day) if report_day else None
    counts = summary.counts if summary else None
    # The close gate (B9), read-only: what would stop a closer today.
    can_close = report_day is not None and not closed and permissions.can_close_day(user)
    gate = (
        approvals.gate_status(db, user, report_day, partner_tickets=summary.partner_not_in_report)
        if can_close and report_day and summary
        else None
    )
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
        totals=sales.day_totals(views, with_profit=show_profit, with_money=show_totals),
        signs=day_page.sign_totals(views, with_profit=show_profit, with_money=show_totals),
        show_profit=show_profit,
        show_totals=show_totals,
        received=received,
        comparison=counts,
        can_run_comparison=received and not closed and permissions.can_run_comparison(user),
        events=[{"e": e, "actor": names.get(e.actor_user_id)} for e in events],
        upload=upload,
        uploader=uploader,
        can_close=can_close,
        gate=gate,
        can_acknowledge=gate is not None
        and approvals.GATE_LIST in gate.reasons
        and permissions.can_validate_money(user),
        can_reopen=closed and permissions.can_reopen_day(user),
        exports=exports,
        can_preview=exportable and not closed and upload is not None,
        no_export_note=exportable and closed and not exports,
        can_compare=closed and bool(exports) and permissions.can_compare_apres(user),
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
        show_profit=permissions.can_see_profit(auth.user),
        show_totals=permissions.can_see_totals(auth.user),
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
        totals=sales.day_totals(
            views,
            with_profit=permissions.can_see_profit(auth.user),
            with_money=permissions.can_see_totals(auth.user),
        ),
        show_totals=permissions.can_see_totals(auth.user),
        gate=approvals.gate_status(db, auth.user, report_day, check_stale=True),
    )


@router.post("/{day}/close")
def day_close(day: str, request: Request, db: Db, clock: Now, auth: Closer) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.DAY_ACTION_USER)
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = _existing(db, auth, chosen)
    try:
        # The close gate (``approvals``) runs inside ``close_day_with_export``, then the day is
        # closed and its TJQ APRES stored (none without an Amadeus report), in one transaction.
        export_apres.close_day_with_export(
            db, auth.user, report_day, now=clock(), ip=client_ip(request)
        )
        db.commit()
    except approvals.CloseBlocked as blocked:
        db.commit()  # the gate may have run the comparison again: keep that
        reasons = "; ".join(blocked.reasons)
        return day_view(db, auth, chosen, error=f"{BLOCKED} {reasons}", status_code=409)
    except days.DayError as exc:
        db.rollback()
        return day_view(db, auth, chosen, error=str(exc), status_code=409)
    return RedirectResponse(f"/days/{chosen.isoformat()}", status_code=303)


@router.post("/{day}/partner-list")
def day_partner_list(
    day: str, request: Request, db: Db, clock: Now, auth: Validator, posted: Posted
) -> Response:
    """ "J'ai vu la liste": the partner tickets that are not in the report were looked at."""
    throttle(db, request, auth, clock, write=True)
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = _existing(db, auth, chosen)
    try:
        raw = posted.get("digest")
        approvals.acknowledge_partner_list(
            db,
            auth.user,
            report_day,
            seen_digest=raw if isinstance(raw, str) else "",
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except days.DayError as exc:
        db.rollback()
        return day_view(db, auth, chosen, error=str(exc), status_code=409)
    except approvals.ApprovalError as exc:
        db.rollback()
        refused = isinstance(exc, approvals.ApprovalForbidden)
        return day_view(db, auth, chosen, error=str(exc), status_code=403 if refused else 400)
    return RedirectResponse(f"/days/{chosen.isoformat()}", status_code=303)


def _download(content: bytes, day: date, *, draft: bool) -> Response:
    """The workbook as an attachment; the name comes from the date only."""
    name = export_apres.export_filename(day, draft=draft)
    return Response(
        content,
        media_type=export_apres.XLSX_MEDIA_TYPE,
        headers={
            **NO_STORE,
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def _audit_download(
    db: Session, request: Request, auth: AuthContext, clock: Clock, details: dict[str, object]
) -> None:
    audit.record(
        db,
        audit.EXPORT_DOWNLOADED,
        agency_id=auth.user.agency_id,
        actor_user_id=auth.user.id,
        ip=client_ip(request),
        details=details,
        now=clock(),
    )
    db.commit()


@router.get("/{day}/apres.xlsx")
def day_apres(
    day: str,
    request: Request,
    db: Db,
    clock: Now,
    auth: Exporter,
    v: Annotated[str | None, Query()] = None,
) -> Response:
    """The stored TJQ APRES of a closed day: the latest version, or ``?v=N``."""
    throttle(db, request, auth, clock, write=False, limit=ratelimit.EXPORT_USER)
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    version: int | None = None
    if v is not None:
        if VERSION.fullmatch(v) is None or int(v) < 1:
            return _error_page(db, auth, BAD_VERSION)
        version = int(v)
    report_day = days.get_day(db, auth.user.agency_id, chosen)
    if report_day is None or report_day.status != DAY_CLOSED:
        return _error_page(db, auth, NOT_CLOSED_YET, 404)
    stored = export_apres.get_export(db, auth.user.agency_id, report_day.id, version)
    if stored is None:
        return _error_page(
            db, auth, UNKNOWN_VERSION if version is not None else NO_STORED_EXPORT, 404
        )
    _audit_download(
        db,
        request,
        auth,
        clock,
        {"report_day_id": report_day.id, "version": stored.version, "draft": False},
    )
    # The stored bytes stay as they are; the margin row goes to the manager only (doc 02).
    return _download(export_apres.workbook_for(auth.user, stored.content), chosen, draft=False)


@router.get("/{day}/apres-preview.xlsx")
def day_apres_preview(day: str, request: Request, db: Db, clock: Now, auth: Exporter) -> Response:
    """A live draft of the TJQ APRES of a day that is not closed: built now, never stored."""
    throttle(db, request, auth, clock, write=False, limit=ratelimit.EXPORT_USER)
    chosen, problem = _day_or_error(day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = days.get_day(db, auth.user.agency_id, chosen)
    if report_day is not None and report_day.status == DAY_CLOSED:
        return _error_page(db, auth, CLOSED_USE_STORED, 409)
    try:
        report = export_apres.apres_rows(db, auth.user, chosen)
    except export_apres.ExportError as exc:
        return _error_page(db, auth, str(exc), 409)
    if report is None:
        return _error_page(db, auth, export_apres.NO_REPORT, 404)
    content = export_apres.build_apres_xlsx(
        report, with_margin=permissions.can_see_profit(auth.user)
    )
    _audit_download(
        db,
        request,
        auth,
        clock,
        {"report_day_id": report_day.id if report_day else None, "draft": True},
    )
    return _download(content, chosen, draft=True)


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
