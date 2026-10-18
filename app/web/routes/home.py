"""The signed-in home page (PR 4 replaces it with the real dashboard)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from app.models import Agency
from app.web.deps import AuthContext, current_user, get_session
from app.web.pages import page

router = APIRouter()


@router.get("/")
def home(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    agency = db.scalars(select(Agency).where(Agency.id == auth.user.agency_id)).one_or_none()
    if agency is None:  # cannot happen (foreign key); fail closed rather than render half a page
        raise HTTPException(status_code=404)
    return page("home.html", user=auth.user, agency=agency, csrf_token=auth.csrf_token)
