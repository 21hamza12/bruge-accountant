"""The price questions of one report day, read-only (prices are collected in Phase 3).

``GET /questions?date=YYYY-MM-DD&status=awaiting|confirmed|all&q=...&limit=N``; without a date,
the agency's newest report day. Visibility (docs/phase2-plan.md, decision 5): a manager sees every
staff sign of the agency, with the person it is linked to and a flag on unlinked signs; staff see
only the signs linked to them, and a search can never reach past that (it filters what they may
already see). The agency always comes from the session, never from the request.

Everything is server-side: the filter tabs are links, the search is a plain GET form, and "show
more" raises ``limit`` (the number of bookings shown per agent).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.dashboard import (
    STATUS_ALL,
    STATUS_AWAITING,
    STATUS_CONFIRMED,
    filter_bookings,
    group_by_agent,
    summarize,
)
from app.repository import recent_report_dates, tickets_for_day
from app.web.deps import AuthContext, current_user, get_session
from app.web.pages import page
from app.web.params import DayParam, parse_day
from app.web.shell import ACTIVE_QUESTIONS, Shell, shell_context

router = APIRouter()

DEFAULT_LIMIT = 50  # bookings shown per agent
MAX_LIMIT = 1000
MAX_QUERY = 100
NO_STORE = {"Cache-Control": "no-store"}

Status = Literal["awaiting", "confirmed", "all"]
__all__ = ["parse_day", "router"]


def _query(day: object, status: str, q: str | None, limit: int | None = None) -> str:
    """The query string of a link that keeps the current view (no empty or default values)."""
    pairs: list[tuple[str, str]] = []
    if day is not None:
        pairs.append(("date", str(day)))
    if status != STATUS_AWAITING:
        pairs.append(("status", status))
    if q:
        pairs.append(("q", q))
    if limit is not None:
        pairs.append(("limit", str(limit)))
    return urlencode(pairs)


def _link(day: object, status: str, q: str | None, limit: int | None = None) -> str:
    query = _query(day, status, q, limit)
    return f"/questions?{query}" if query else "/questions"


@router.get("/questions")
def questions_page(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
    day: DayParam = None,
    status: Annotated[Status, Query()] = STATUS_AWAITING,
    q: Annotated[str | None, Query(max_length=MAX_QUERY)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> Response:
    requested = parse_day(day)
    context = shell_context(db, auth, ACTIVE_QUESTIONS)
    shell: Shell = context["shell"]
    chosen = requested or shell.latest_day
    term = (q or "").strip() or None
    context.update(
        {
            "day": chosen,
            "dates": recent_report_dates(db, shell.agency.id) if chosen else [],
            "status": status,
            "q": term,
            "summary": None,
            "groups": (),
            "totals": {},
            "tabs": (),
        }
    )
    if chosen is None or (not shell.is_manager and not shell.my_signs):
        return page("questions.html", headers=NO_STORE, **context)

    visible_signs = None if shell.is_manager else shell.my_signs
    tickets = tickets_for_day(db, shell.agency.id, chosen, visible_signs)
    summary = summarize(tickets)
    groups = group_by_agent(filter_bookings(summary.bookings, status, term))
    shown = [
        {
            "group": g,
            "bookings": g.bookings[:limit],
            "hidden": max(0, len(g.bookings) - limit),
            "more_url": _link(chosen, status, term, min(limit * 2, MAX_LIMIT))
            + f"#agent-{quote(g.staff_sign)}",
        }
        for g in groups
    ]
    tabs: list[dict[str, Any]] = [
        {"key": STATUS_AWAITING, "count": summary.awaiting_count},
        {"key": STATUS_CONFIRMED, "count": summary.confirmed_count},
        {"key": STATUS_ALL, "count": None},
    ]
    for tab in tabs:
        tab["url"] = _link(chosen, tab["key"], term)
        tab["current"] = tab["key"] == status
    context.update(
        {
            "summary": summary,
            "groups": shown,
            "totals": {a.staff_sign: a for a in summary.agents},
            "tabs": tabs,
        }
    )
    return page("questions.html", headers=NO_STORE, **context)
