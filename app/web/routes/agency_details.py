"""« Coordonnées de l'agence » (phase F, slice F3): the details printed on the partner statements.

The manager and the accountant read the page (``can_view_agency_details``); only the manager
saves it (``can_edit_agency_details``); staff, and the accountant posting, get the 403 page and a
``permission_denied`` audit event. Always the session's own agency. CSRF on the router; the form
carries a ``request_key`` (a double click saves once; a key replayed with other values comes back
with a fresh one). Saves count against ``AGENCY_DETAILS_WRITE_USER``. The service is
``app/agency_details.py``. ``Cache-Control: no-store``. No inline style or script.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import agency_details, partner_book, permissions
from app.agency_details import DetailsError, DetailsForbidden, NothingChanged, StaleRequestKey
from app.auth import ratelimit
from app.models import AGENCY_DETAIL_LIMITS
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, require_permission
from app.web.pages import page
from app.web.sale_pages import Db, Now, Posted, throttle
from app.web.shell import ACTIVE_AGENCY_DETAILS, shell_context

router = APIRouter(prefix="/agency-details", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
MAX_FIELD = 600  # characters kept of any posted text (the service checks the real limits)
SAVED = "Coordonnées enregistrées."

require_details_reader = require_permission(permissions.can_view_agency_details)
require_details_writer = require_permission(permissions.can_edit_agency_details)
Reader = Annotated[AuthContext, Depends(require_details_reader)]
Writer = Annotated[AuthContext, Depends(require_details_writer)]


def _form_values(db: Session, agency_id: int) -> dict[str, str]:
    row = agency_details.get_details(db, agency_id)
    return {name: (getattr(row, name) or "") if row else "" for name in agency_details.FIELDS}


def _page(
    db: Session,
    auth: AuthContext,
    *,
    form: dict[str, str] | None = None,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    values = form or {
        **_form_values(db, auth.user.agency_id),
        "request_key": partner_book.new_request_key(),
    }
    return page(
        "agency/details.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_AGENCY_DETAILS),
        fields=[
            (name, agency_details.LABELS[name], AGENCY_DETAIL_LIMITS[name])
            for name in agency_details.FIELDS
        ],
        form=values,
        can_edit=permissions.can_edit_agency_details(auth.user),
        error=error,
        notice=notice,
    )


@router.get("")
def details_show(
    db: Db,
    auth: Reader,
    enregistre: Annotated[str | None, Query(max_length=4)] = None,
) -> Response:
    return _page(db, auth, notice=SAVED if enregistre else None)


@router.post("")
def details_save(request: Request, db: Db, clock: Now, auth: Writer, posted: Posted) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.AGENCY_DETAILS_WRITE_USER)
    form: dict[str, str] = {}
    for name in (*agency_details.FIELDS, "request_key"):
        raw = posted.get(name)
        form[name] = raw[:MAX_FIELD] if isinstance(raw, str) else ""
    try:
        agency_details.save_details(
            db,
            auth.user,
            form,
            request_key=form["request_key"],
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except NothingChanged as exc:
        db.rollback()
        form["request_key"] = partner_book.new_request_key()
        return _page(db, auth, form=form, notice=str(exc))
    except DetailsError as exc:
        db.rollback()
        if isinstance(exc, StaleRequestKey):
            form["request_key"] = partner_book.new_request_key()
        status = 403 if isinstance(exc, DetailsForbidden) else 400
        return _page(db, auth, form=form, error=str(exc), status_code=status)
    return RedirectResponse("/agency-details?enregistre=1", status_code=303)


__all__ = ["router"]
