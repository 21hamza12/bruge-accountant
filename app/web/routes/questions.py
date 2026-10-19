"""The price questions of one report day, read-only (prices are collected in Phase 3).

``GET /questions?date=YYYY-MM-DD``; without a date, the agency's latest report date that has
tickets. Visibility (docs/phase2-plan.md, decision 5): a manager sees every staff sign of the
agency, with the person it is linked to and a flag on unlinked signs; staff see only the signs
linked to them. The agency always comes from the session, never from the request.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from app import accounts
from app.models import ROLE_MANAGER, Agency
from app.questions import build_questions
from app.repository import (
    latest_report_date,
    recent_report_dates,
    sign_owners,
    tickets_awaiting_price,
)
from app.web.deps import AuthContext, current_user, get_session
from app.web.pages import page

router = APIRouter()

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_day(raw: str | None) -> date | None:
    """``YYYY-MM-DD`` only (``date.fromisoformat`` also takes ``20261001`` and week dates).
    None for a missing or empty value; 400 for anything else."""
    if raw is None or raw == "":
        return None
    if not _ISO_DATE.fullmatch(raw):
        raise HTTPException(status_code=400)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400) from None


@router.get("/questions")
def questions_page(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
    day: Annotated[str | None, Query(alias="date", max_length=32)] = None,
) -> Response:
    user = auth.user
    agency_id = user.agency_id
    chosen = parse_day(day) or latest_report_date(db, agency_id)
    agency = db.scalars(select(Agency).where(Agency.id == agency_id)).one_or_none()
    if agency is None:  # cannot happen (foreign key); fail closed
        raise HTTPException(status_code=404)
    is_manager = user.role == ROLE_MANAGER
    context: dict[str, Any] = {
        "csrf_token": auth.csrf_token,
        "agency": agency,
        "is_manager": is_manager,
        "day": chosen,
        "dates": recent_report_dates(db, agency_id),
        "groups": [],
        "owners": {},
        "my_signs": [],
    }
    if chosen is None:
        return page("questions.html", headers={"Cache-Control": "no-store"}, **context)

    groups = build_questions(tickets_awaiting_price(db, agency_id, chosen))
    my_signs = [link.staff_sign for link in accounts.get_signs(db, agency_id, user.id)]
    if not is_manager:
        mine = set(my_signs)
        groups = [g for g in groups if g.staff_sign in mine]
    return page(
        "questions.html",
        **{
            **context,
            "groups": groups,
            "owners": sign_owners(db, agency_id) if is_manager else {},
            "my_signs": my_signs,
        },
        headers={"Cache-Control": "no-store"},
    )
