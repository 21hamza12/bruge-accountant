"""The switch-over pages (phase F, slice F5).

* ``POST /partner-openings/{partner_id}``: enter a partner's opening balance (the form is on the
  partner's account page while it has none; ``GET /partner-openings/{partner_id}/new`` shows it
  alone, and again with the error when a post is refused).
* ``GET /partner-openings``: « Soldes d'ouverture à saisir », one form per active partner still
  without an opening, each posted on its own.
* ``GET`` / ``POST /partner-openings/check``: « Vérification du passage », the app's balance at
  the end of the first half-month after the switch-over and the difference with the balance
  typed from the partner's spreadsheet (never stored, never audited).

The opening is the manager's (``can_enter_partner_opening``); the check is the manager's and the
accountant's (``can_check_partner_switchover``); anybody else gets the 403 page and a
``permission_denied`` audit event. The agency is always the session's: a partner of another
agency is a 404. CSRF on the router; the opening form carries a ``request_key`` (a double click
writes once, a key reused with other values is refused and the form comes back with a fresh
one); openings count against ``PARTNER_OPENING_WRITE_USER``, the check against
``PARTNER_ACCOUNT_READ_USER``. The service is ``app/partner_opening.py``. Pages carry names and
money: ``Cache-Control: no-store``. No inline style or script.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import partner_book, partner_opening, permissions
from app.auth import ratelimit
from app.clock import Clock
from app.models import Agency
from app.partner_book import (
    AccountConflict,
    AccountForbidden,
    AccountNotFound,
    PartnerAccountError,
    StaleRequestKey,
)
from app.partner_opening import NeedsConfirmation
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, require_permission
from app.web.pages import page
from app.web.params import PartnerId, parse_db_id
from app.web.sale_pages import Db, Now, Posted, throttle
from app.web.shell import ACTIVE_PARTNER_ACCOUNTS, shell_context

router = APIRouter(prefix="/partner-openings", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
MAX_FIELD = 600  # characters kept of any posted text (the service checks the real limits)
BACK_TO_LIST = "liste"
SAVED = "Solde d'ouverture enregistré."
FIELDS = (
    "switch_date",
    "amount",
    "sign",
    "solde_nul",
    "reason",
    "request_key",
    "back",
    "verifie",
    "acknowledged",
)
TICKED = "1"  # the value of a ticked checkbox (« Solde nul », « J'ai vérifié ... »)

require_opener = require_permission(permissions.can_enter_partner_opening)
require_checker = require_permission(permissions.can_check_partner_switchover)
Opener = Annotated[AuthContext, Depends(require_opener)]
Checker = Annotated[AuthContext, Depends(require_checker)]


def _text(posted: Posted, name: str) -> str:
    raw = posted.get(name)
    return raw[:MAX_FIELD] if isinstance(raw, str) else ""


def _status(exc: PartnerAccountError) -> int:
    if isinstance(exc, AccountNotFound):
        raise HTTPException(status_code=404)
    if isinstance(exc, AccountForbidden):
        return 403
    if isinstance(exc, AccountConflict):
        return 409
    return 400


def _signs() -> list[tuple[str, str]]:
    return list(partner_opening.SIGN_LABELS.items())


def _common(db: Session, auth: AuthContext, clock: Clock) -> dict[str, Any]:
    return {
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        "signs": _signs(),
        "today": clock().date(),
        "earliest": partner_opening.earliest_switch_day(clock().date()),
    }


# --- the check (declared before /{partner_id}, which would also match "check") ----------------


def _check_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    *,
    rows: list[partner_opening.CheckRow] | None = None,
    form: dict[str, str] | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    if rows is None:
        rows = partner_opening.check_rows(db, auth.user, clock().date())
    return page(
        "partner_accounts/switchover.html",
        status_code=status_code,
        headers=NO_STORE,
        **_common(db, auth, clock),
        rows=rows,
        can_open=permissions.can_enter_partner_opening(auth.user),
        form=form,
        result=result,
        error=error,
    )


def _read(db: Session, request: Request, auth: AuthContext, clock: Clock) -> None:
    throttle(db, request, auth, clock, write=False, limit=ratelimit.PARTNER_ACCOUNT_READ_USER)


@router.get("/check")
def check_show(request: Request, db: Db, clock: Now, auth: Checker) -> Response:
    _read(db, request, auth, clock)
    return _check_page(db, auth, clock)


@router.post("/check")
def check_compare(request: Request, db: Db, clock: Now, auth: Checker, posted: Posted) -> Response:
    """Compare one partner's balance with the typed one: nothing is written."""
    _read(db, request, auth, clock)
    form = {name: _text(posted, name) for name in ("partner_id", "amount", "sign")}
    partner_id = parse_db_id(form["partner_id"])
    rows = partner_opening.check_rows(db, auth.user, clock().date())
    row = next((r for r in rows if r.partner.id == partner_id), None)
    if row is None:
        raise HTTPException(status_code=404)
    try:
        comparison = partner_opening.compare(
            row.app_balance, form["amount"], form["sign"], currency=_currency(db, auth)
        )
    except PartnerAccountError as exc:
        return _check_page(db, auth, clock, rows=rows, form=form, error=str(exc), status_code=400)
    result = {"partner_id": row.partner.id, "comparison": comparison}
    return _check_page(db, auth, clock, rows=rows, form=form, result=result)


def _currency(db: Session, auth: AuthContext) -> str:
    currency = db.scalar(select(Agency.currency).where(Agency.id == auth.user.agency_id))
    return currency or "MRU"


# --- the list of partners without an opening --------------------------------------------------


@router.get("")
def openings_list(
    request: Request,
    db: Db,
    clock: Now,
    auth: Opener,
    saisi: Annotated[str | None, Query(max_length=4)] = None,
) -> Response:
    _read(db, request, auth, clock)
    partners = partner_opening.without_opening(db, auth.user)
    return page(
        "partner_accounts/openings.html",
        headers=NO_STORE,
        **_common(db, auth, clock),
        partners=partners,
        forms={p.id: {"request_key": partner_book.new_request_key()} for p in partners},
        notice=SAVED if saisi else None,
    )


# --- one opening ------------------------------------------------------------------------------


def _form_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    partner_id: int,
    *,
    form: dict[str, str] | None = None,
    error: str | None = None,
    preview: partner_opening.Dropped | None = None,
    status_code: int = 200,
) -> Response:
    """The form alone; with ``preview`` the list of what stops counting and the tick. For a
    partner that cannot take an opening (inactive, opened, a period closed) the reason only."""
    partner = partner_book.get_partner(db, auth.user.agency_id, partner_id)
    if partner is None:
        raise HTTPException(status_code=404)
    return page(
        "partner_accounts/opening_form.html",
        status_code=status_code,
        headers=NO_STORE,
        **_common(db, auth, clock),
        partner=partner,
        blocked=partner_opening.refusal_for(db, partner),
        form=form or {"request_key": partner_book.new_request_key()},
        error=error,
        preview=preview,
        kinds=partner_book.KIND_LABELS,
        methods=partner_book.METHOD_LABELS,
    )


@router.get("/{partner_id}/new")
def opening_new(
    partner_id: PartnerId, request: Request, db: Db, clock: Now, auth: Opener
) -> Response:
    _read(db, request, auth, clock)
    return _form_page(db, auth, clock, partner_id)


@router.post("/{partner_id}")
def opening_add(
    partner_id: PartnerId, request: Request, db: Db, clock: Now, auth: Opener, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.PARTNER_OPENING_WRITE_USER)
    form = {name: _text(posted, name) for name in FIELDS}
    try:
        written = partner_opening.enter_opening(
            db,
            auth.user,
            partner_id,
            amount=form["amount"],
            sign=form["sign"],
            switch_date=form["switch_date"],
            reason=form["reason"],
            request_key=form["request_key"],
            zero_balance=form["solde_nul"] == TICKED,
            confirmed=form["verifie"] == TICKED,
            acknowledged=form["acknowledged"],
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except NeedsConfirmation as asked:
        # Nothing written: show the list; the same form comes back with its fingerprint and
        # the tick to give. A list that changed since the one confirmed is a 409.
        db.rollback()
        form["acknowledged"] = asked.dropped.fingerprint
        form["verifie"] = ""
        return _form_page(
            db,
            auth,
            clock,
            partner_id,
            form=form,
            error=str(asked),
            preview=asked.dropped,
            status_code=409 if asked.changed else 200,
        )
    except PartnerAccountError as exc:
        db.rollback()
        status = _status(exc)
        if isinstance(exc, StaleRequestKey):
            form["request_key"] = partner_book.new_request_key()
        return _form_page(
            db, auth, clock, partner_id, form=form, error=str(exc), status_code=status
        )
    if form["back"] == BACK_TO_LIST:
        return RedirectResponse("/partner-openings?saisi=1", status_code=303)
    again = "" if written.created else "?deja=ouverture"
    return RedirectResponse(f"/partner-accounts/{partner_id}{again}", status_code=303)


__all__ = ["router"]
