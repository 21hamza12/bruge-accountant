"""The signed-in home page: a dashboard for managers, a work list for staff.

``GET /?date=YYYY-MM-DD``; without a date, the manager gets the agency's newest report day and
staff get the newest day with a ticket on their own signs (the agency's newest only when they
have none). Visibility
(docs/phase2-plan.md, decision 5): a manager sees every ticket of the agency; staff see only the
tickets of the staff signs linked to them, whatever date they ask for. The agency always comes
from the session, never from the request.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.clock import Clock
from app.dashboard import STATUS_ALL, DaySummary, chart_bars, summarize
from app.repository import tickets_for_day
from app.web.deps import AuthContext, current_user, get_clock, get_session
from app.web.pages import page
from app.web.params import DayParam, parse_day
from app.web.routes.questions import MAX_LIMIT, questions_link
from app.web.shell import ACTIVE_HOME, Shell, shell_context

router = APIRouter()

TOP_WAITING = 5  # rows in the manager's "Questions en attente" table
STAFF_LIST_LIMIT = 50  # rows in the staff member's "Mes réservations" list
NO_STORE = {"Cache-Control": "no-store"}


def _manager_context(db: Session, shell: Shell, day: date | None) -> dict[str, Any]:
    summary = summarize(tickets_for_day(db, shell.agency.id, day)) if day else None
    return {
        "day": day,
        "summary": summary,
        "bars": chart_bars(summary.airlines) if summary else (),
        "waiting": summary.top_waiting(TOP_WAITING) if summary else (),
        "owners": shell.owners,
    }


def _staff_context(db: Session, shell: Shell, day: date | None, today: date) -> dict[str, Any]:
    summary: DaySummary | None = None
    if day is not None and shell.my_signs:
        summary = summarize(tickets_for_day(db, shell.agency.id, day, shell.my_signs))
    bookings = summary.bookings if summary else ()
    listed = bookings[:STAFF_LIST_LIMIT]
    return {
        "day": day,
        "summary": summary,
        "is_today": day == today,
        "waiting_tickets": sum(b.question.ticket_count for b in bookings if not b.confirmed),
        "listed": listed,
        "hidden_count": len(bookings) - len(listed),
        # Everything of theirs, not the questions page's default cut of 50 bookings per agent.
        "all_url": questions_link(day, STATUS_ALL, None, MAX_LIMIT),
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
        extra = _manager_context(db, shell, chosen)
        template = "home_manager.html"
    else:
        extra = _staff_context(db, shell, chosen, clock().date())
        template = "home_staff.html"
    return page(template, headers=NO_STORE, **context, **extra)
