"""The price questions of one report day, read-only (prices are collected in Phase 3).

``GET /questions?date=YYYY-MM-DD&status=awaiting|confirmed|all&q=...&limit=N``; without a date,
a manager gets the agency's newest report day and staff the newest day with a ticket on their own
signs (see ``app.web.shell``). The dates offered are likewise limited for staff. Visibility
(docs/phase2-plan.md, decision 5): a manager sees every staff sign of the agency, with the person
it is linked to and a flag on unlinked signs; staff see only the signs linked to them, and a
search can never reach past that (it filters what they may already see). The agency always
comes from the session, never from the request.

Everything is server-side: the filter tabs are links, the search is a plain GET form, and "show
more" raises ``limit`` (the number of bookings shown per agent) until it reaches ``MAX_LIMIT``,
where the link goes away and a note says the list is cut.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Literal
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.dashboard import (
    STATUS_ALL,
    STATUS_AWAITING,
    STATUS_CONFIRMED,
    AgentGroup,
    Booking,
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


@dataclass(frozen=True)
class AgentView:
    """One agent's block on the page: the bookings shown and what "show more" does."""

    group: AgentGroup
    bookings: tuple[Booking, ...]  # at most ``limit`` of the group's bookings
    hidden: int  # how many are not shown
    more_url: str | None  # None when nothing is hidden or the cap is reached
    capped: bool  # bookings are hidden and no further "show more" exists


@dataclass(frozen=True)
class FilterTab:
    """One filter tab (a plain link)."""

    key: str
    count: int | None  # None: no count shown
    url: str
    current: bool


def _query(day: date | None, status: str, q: str | None, limit: int | None = None) -> str:
    """The query string of a link that keeps the current view (no empty or default values)."""
    pairs: list[tuple[str, str]] = []
    if day is not None:
        pairs.append(("date", day.isoformat()))
    if status != STATUS_AWAITING:
        pairs.append(("status", status))
    if q:
        pairs.append(("q", q))
    if limit is not None:
        pairs.append(("limit", str(limit)))
    return urlencode(pairs)


def questions_link(day: date | None, status: str, q: str | None, limit: int | None = None) -> str:
    query = _query(day, status, q, limit)
    return f"/questions?{query}" if query else "/questions"


def _search_term(raw: str | None) -> str | None:
    """The trimmed search text, or None when empty; control characters (NUL, line breaks, tabs,
    escapes) are not search text and get the 400 page."""
    if raw is None:
        return None
    if any(unicodedata.category(char) == "Cc" for char in raw):
        raise HTTPException(status_code=400)
    return raw.strip() or None


def _agent_view(
    group: AgentGroup, day: date, status: str, term: str | None, limit: int
) -> AgentView:
    hidden = max(0, len(group.bookings) - limit)
    more_url = None
    if hidden and limit < MAX_LIMIT:
        more_url = (
            questions_link(day, status, term, min(limit * 2, MAX_LIMIT))
            + f"#agent-{quote(group.staff_sign)}"
        )
    return AgentView(
        group=group,
        bookings=group.bookings[:limit],
        hidden=hidden,
        more_url=more_url,
        capped=bool(hidden) and more_url is None,
    )


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
    term = _search_term(q)
    context = shell_context(db, auth, ACTIVE_QUESTIONS)
    shell: Shell = context["shell"]
    chosen = requested or shell.latest_day
    visible_signs = shell.visible_signs
    context.update(
        {
            "day": chosen,
            "dates": (
                recent_report_dates(db, shell.agency.id, staff_signs=visible_signs)
                if chosen
                else []
            ),
            "status": status,
            "q": term,
            "limit": limit,
            "max_limit": MAX_LIMIT,
            "summary": None,
            "groups": (),
            "totals": {},
            "tabs": (),
        }
    )
    if chosen is None or (not shell.sees_all and not shell.my_signs):
        return page("questions.html", headers=NO_STORE, **context)

    tickets = tickets_for_day(db, shell.agency.id, chosen, visible_signs)
    summary = summarize(tickets)
    groups = group_by_agent(filter_bookings(summary.bookings, status, term))
    counts = {
        STATUS_AWAITING: summary.awaiting_count,
        STATUS_CONFIRMED: summary.confirmed_count,
        STATUS_ALL: None,
    }
    context.update(
        {
            "summary": summary,
            "groups": [_agent_view(g, chosen, status, term, limit) for g in groups],
            "totals": {a.staff_sign: a for a in summary.agents},
            "tabs": tuple(
                FilterTab(key, count, questions_link(chosen, key, term), key == status)
                for key, count in counts.items()
            ),
        }
    )
    return page("questions.html", headers=NO_STORE, **context)
