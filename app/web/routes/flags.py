"""The comparison flags (B8): ``GET /flags?date=`` lists them, ``POST /flags/run?date=`` runs the
comparison of a day again (manager and accountant).

Any signed-in user may read the list: a staff member sees only the flags of their own signs
(``app/flags.py`` filters, nothing is filtered in the template), a manager or accountant sees
all. The cancelled lines and the partner tickets that are not in the report are information
sections, never flags. Answering a flag is B9. Every page is French, ``Cache-Control: no-store``
(ticket numbers, names, amounts) and CSRF-protected on the router; no inline style or script.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import days, flags, permissions
from app.auth import ratelimit
from app.clock import Clock
from app.errors import UserError
from app.formatting import date_fr, money_fr
from app.models import ComparisonFlag
from app.repository import latest_report_date
from app.web.csrf import csrf_protect
from app.web.deps import (
    AuthContext,
    client_ip,
    current_user,
    get_clock,
    get_session,
    require_permission,
)
from app.web.pages import page
from app.web.params import DayParam, parse_day
from app.web.shell import ACTIVE_FLAGS, shell_context

router = APIRouter(prefix="/flags", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
Db = Annotated[Session, Depends(get_session)]
Now = Annotated[Clock, Depends(get_clock)]
Viewer = Annotated[AuthContext, Depends(current_user)]
Runner = Annotated[AuthContext, Depends(require_permission(permissions.can_run_comparison))]

KIND_LABELS = {
    "different": "Différent",
    "not_sent": "Non envoyé",
    "not_in_amadeus": "Absent d'Amadeus",
    "elsewhere": "Ailleurs",
    "bad_number": "Numéro invalide",
}
FIELD_LABELS = {
    "airline": "Compagnie",
    "total_doc": "Coût (TOTAL DOC)",
    "pax_name": "Nom du passager",
    "pax_type": "Type de passager",
    "staff_sign": "Signe (AS)",
    "rloc": "Réservation (RLOC)",
    "trnc": "Type de billet",
    "doc_number": "Numéro de billet",
}
STATUS_LABELS = {"open": "Ouvert", "answered": "Répondu", "resolved": "Résolu"}
DAY_LABELS = {
    "open": "Ouverte",
    "compared": "Comparée",
    "approved": "Approuvée",
    "closed": "Clôturée",
}
BAD_NUMBER_NOTES = {"unusable": "illisible", "duplicate": "en double"}
NOTHING = "—"


@dataclass(frozen=True)
class FlagRow:
    """One flag as the page prints it."""

    id: int
    kind: str
    field: str
    doc_number: str
    app_value: str
    amadeus_value: str
    sign: str
    status: str
    needs_validation: bool


def _money(raw: str) -> str:
    try:
        return money_fr(Decimal(raw))
    except InvalidOperation:
        return raw


def _value(flag: ComparisonFlag, raw: str | None, *, from_app: bool) -> str:
    """A stored value as text. The meaning depends on the kind: for ``elsewhere`` the app value is
    the day the ticket is on; for ``bad_number`` the values are short notes."""
    if not raw:
        return NOTHING
    if flag.kind == "different" and flag.field == "total_doc":
        return _money(raw)
    if flag.kind == "elsewhere" and from_app:
        try:
            return date_fr(date.fromisoformat(raw))
        except ValueError:
            return raw
    if flag.kind == "bad_number":
        return BAD_NUMBER_NOTES.get(raw, raw)
    return raw


def flag_row(flag: ComparisonFlag) -> FlagRow:
    return FlagRow(
        id=flag.id,
        kind=KIND_LABELS.get(flag.kind, flag.kind),
        field=FIELD_LABELS.get(flag.field or "", "") if flag.field else "",
        doc_number=flag.doc_number or NOTHING,
        app_value=_value(flag, flag.app_value, from_app=True),
        amadeus_value=_value(flag, flag.amadeus_value, from_app=False),
        sign=flag.responsible_sign or NOTHING,
        status=STATUS_LABELS.get(flag.status, flag.status),
        needs_validation=flag.needs_validation,
    )


def _list_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    chosen: date,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    agency_id = auth.user.agency_id
    report_day = days.get_day(db, agency_id, chosen)
    summary = flags.day_summary(db, auth.user, report_day) if report_day is not None else None
    upload = flags.latest_upload_for_day(db, agency_id, chosen)
    return page(
        "flags/list.html",
        status_code=status_code,
        headers=NO_STORE,
        day=chosen,
        report_day=report_day,
        day_status=DAY_LABELS.get(report_day.status, report_day.status) if report_day else "",
        summary=summary,
        rows=[flag_row(f) for f in summary.flags] if summary is not None else [],
        has_report=upload is not None,
        can_run=permissions.can_run_comparison(auth.user),
        error=error,
        **shell_context(db, auth, ACTIVE_FLAGS),
    )


@router.get("")
def flag_list(db: Db, clock: Now, auth: Viewer, day: DayParam = None) -> Response:
    chosen = parse_day(day) or latest_report_date(db, auth.user.agency_id) or clock().date()
    return _list_page(db, auth, clock, chosen)


def _throttle(db: Session, auth: AuthContext, clock: Clock) -> None:
    limit = ratelimit.COMPARISON_RUN_USER
    decision = ratelimit.hit(db, limit, str(auth.user.id), clock())
    db.commit()
    if not decision.allowed:
        raise HTTPException(status_code=429, headers={"Retry-After": str(decision.retry_after)})


@router.post("/run")
def flag_run(request: Request, db: Db, clock: Now, auth: Runner, day: DayParam = None) -> Response:
    """Run the comparison of the day again (the upload did it already, unless it failed)."""
    chosen = parse_day(day)
    if chosen is None:
        raise HTTPException(status_code=400)
    _throttle(db, auth, clock)
    agency_id = auth.user.agency_id
    now = clock()
    try:
        if flags.latest_upload_for_day(db, agency_id, chosen) is None:
            raise flags.FlagError(flags.MSG_NO_REPORT)
        report_day = days.get_or_open_day(db, auth.user, chosen, now=now, ip=client_ip(request))
        flags.run_comparison(db, auth.user, report_day, now=now, ip=client_ip(request))
        db.commit()
    except UserError as exc:
        db.rollback()
        status = 409 if isinstance(exc, days.DayClosed) else 400
        return _list_page(db, auth, clock, chosen, error=str(exc), status_code=status)
    return RedirectResponse(f"/flags?date={chosen.isoformat()}", status_code=303)


__all__ = ["router"]
