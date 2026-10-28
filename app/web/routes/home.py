"""The signed-in home page: a dashboard for managers, a work list for staff.

``GET /?date=YYYY-MM-DD``; without a date, the manager gets the agency's newest report day and
staff get the newest day with a ticket on their own signs (the agency's newest only when they
have none). Visibility
(docs/phase2-plan.md, decision 5): a manager sees every ticket of the agency; staff see only the
tickets of the staff signs linked to them, whatever date they ask for. The agency always comes
from the session, never from the request.

The manager and the accountant also get the partner accounts that are overdue or due soon and the
refunds proposed from the Amadeus report waiting for a decision (``app.partner_alerts``, phase F4:
a constant number of queries whatever the number of partners). Staff never get them.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import partner_alerts, sales
from app.clock import Clock
from app.comparison import normalise_doc
from app.dashboard import chart_bars, is_cancelled, summarize
from app.repository import tickets_for_day
from app.web.deps import AuthContext, current_user, get_clock, get_session
from app.web.pages import page
from app.web.params import DayParam, parse_day
from app.web.shell import ACTIVE_HOME, Shell, shell_context

router = APIRouter()

STAFF_LIST_LIMIT = 50  # rows in the staff member's "Billets à saisir" list
NO_STORE = {"Cache-Control": "no-store"}


def _manager_context(db: Session, shell: Shell, day: date | None, today: date) -> dict[str, Any]:
    summary = summarize(tickets_for_day(db, shell.agency.id, day)) if day else None
    return {
        "day": day,
        "summary": summary,
        "bars": chart_bars(summary.airlines) if summary else (),
        "owners": shell.owners,
        "partner_alerts": partner_alerts.partner_alerts(db, shell.user, today),
    }


def _staff_context(db: Session, shell: Shell, day: date | None) -> dict[str, Any]:
    """The staff member's day: their tickets of the report, the sales they entered (the same
    signs, the same day) and the report tickets that no sale holds yet."""
    mine = (
        tickets_for_day(db, shell.agency.id, day, shell.my_signs) if day and shell.my_signs else []
    )
    summary = summarize(mine) if day is not None and shell.my_signs else None
    my_sales = (
        sales.sales_for_day_within(db, shell.user, day, shell.my_signs)
        if day is not None and shell.my_signs
        else []
    )
    # A report ticket is entered when any active sale of the agency holds it (the comparison's
    # rule): a sale typed by the manager, or under another sign, is not "à saisir" here.
    issued = [t for t in mine if not is_cancelled(t)]
    keys = [normalise_doc(t.ticket_no, t.airline) or t.ticket_no for t in issued]
    sold = sales.active_ticket_lookup(db, shell.agency.id, keys) if keys else {}
    missing = [t for t, key in zip(issued, keys, strict=True) if key not in sold]
    return {
        "day": day,
        "summary": summary,
        "sale_count": len(my_sales),
        "missing": missing[:STAFF_LIST_LIMIT],
        "missing_count": len(missing),
        "hidden_count": max(0, len(missing) - STAFF_LIST_LIMIT),
    }


@router.get("/")
def home(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
    clock: Annotated[Clock, Depends(get_clock)],
    day: DayParam = None,
) -> Response:
    requested = parse_day(day)
    context = shell_context(db, auth, ACTIVE_HOME)
    shell: Shell = context["shell"]
    chosen = requested or shell.latest_day
    if shell.sees_all:  # accountants intentionally get the manager dashboard
        extra = _manager_context(db, shell, chosen, clock().date())
        template = "home_manager.html"
    else:
        extra = _staff_context(db, shell, chosen)
        template = "home_staff.html"
    return page(template, headers=NO_STORE, **context, **extra)
