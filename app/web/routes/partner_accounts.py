"""The partner account pages (phase F, slice F2): « Comptes partenaires » and one partner's
account, with « Ajouter une ligne », « Enregistrer un paiement », the decision on a line waiting
for one, and « Clôturer la période ».

Manager and accountant only: every route needs ``can_manage_partners`` (reads) or
``can_validate_money`` (writes); staff get the 403 page and a ``permission_denied`` audit event.
The agency is always the session's: a partner, line or period of another agency is a 404, like
one that does not exist. CSRF on the router; every form carries a ``request_key`` (a double click
writes once; a key reused with other values is refused and the form comes back with a
fresh one). Reads count against ``PARTNER_ACCOUNT_READ_USER``. Every write counts against
``PARTNER_ACCOUNT_WRITE_USER`` (the close against
``PARTNER_PERIOD_CLOSE_USER``). Amounts are typed in MRU and parsed by ``app.money`` (never a
float). The service is ``app/partner_book.py``; a refusal is a French message on the page, and a
refusal by the database (a period closed meanwhile) too, never a 500. Pages carry names and money:
``Cache-Control: no-store``. No inline style or script.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import partner_book, permissions
from app.auth import ratelimit
from app.clock import Clock
from app.errors import RETRY_LATER, is_lock_conflict
from app.models import PARTNER_PAYMENT_METHODS, User
from app.partner_book import (
    Account,
    AccountConflict,
    AccountForbidden,
    AccountNotFound,
    PartnerAccountError,
    StaleRequestKey,
)
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, require_permission
from app.web.pages import page
from app.web.params import DbId, PartnerId, parse_day, parse_db_id
from app.web.sale_pages import Db, Now, Posted, throttle
from app.web.shell import ACTIVE_PARTNER_ACCOUNTS, shell_context

router = APIRouter(prefix="/partner-accounts", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
MAX_FIELD = 600  # characters kept of any posted text (the service checks the real limits)

require_account_reader = require_permission(permissions.can_manage_partners)
require_account_writer = require_permission(permissions.can_validate_money)
Reader = Annotated[AuthContext, Depends(require_account_reader)]
Writer = Annotated[AuthContext, Depends(require_account_writer)]


def _text(posted: Posted, name: str) -> str:
    raw = posted.get(name)
    return raw[:MAX_FIELD] if isinstance(raw, str) else ""


def _back(partner_id: int, already: str | None) -> Response:
    """Back to the account; ``already`` says the same form had been posted before."""
    query = f"?deja={already}" if already else ""
    return RedirectResponse(f"/partner-accounts/{partner_id}{query}", status_code=303)


def _fresh_key_if_stale(exc: PartnerAccountError, form: dict[str, str]) -> None:
    """A key already used for other values gets a new one, so the corrected form can be sent."""
    if isinstance(exc, StaleRequestKey):
        form["request_key"] = partner_book.new_request_key()


def _status(exc: PartnerAccountError) -> int:
    if isinstance(exc, AccountNotFound):
        raise HTTPException(status_code=404)
    if isinstance(exc, AccountForbidden):
        return 403
    if isinstance(exc, AccountConflict):
        return 409
    return 400


def _account_or_404(
    db: Session, auth: AuthContext, partner_id: int, clock: Clock, *, snapshots: bool = False
) -> Account:
    try:
        return partner_book.get_account(
            db, auth.user, partner_id, clock().date(), snapshots=snapshots
        )
    except AccountNotFound:
        raise HTTPException(status_code=404) from None


def _names(db: Session, agency_id: int, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return {
        u.id: (u.display_name or "").strip() or u.role
        for u in db.scalars(select(User).where(User.agency_id == agency_id, User.id.in_(ids)))
    }


def _receivers(db: Session, agency_id: int) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(User.agency_id == agency_id, User.is_active.is_(True))
            .order_by(User.display_name, User.id)
        )
    )


def _account_page(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    partner_id: int,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    acc = _account_or_404(db, auth, partner_id, clock)
    current = acc.first_open
    ids: set[int] = set()
    for p in acc.periods:
        for e in p.entries:
            ids.add(e.entry.created_by_user_id)
            if e.decision is not None:
                ids.add(e.decision.decided_by_user_id)
        for pay in p.payments:
            ids.add(pay.received_by_user_id)
    return page(
        "partner_accounts/account.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        acc=acc,
        current=current,
        names=_names(db, auth.user.agency_id, ids),
        kinds=partner_book.KIND_LABELS,
        methods=partner_book.METHOD_LABELS,
        me=auth.user,
        can_approve=permissions.can_approve_partner_adjustment(auth.user),
        can_write=permissions.can_validate_money(auth.user),
        today=clock().date(),
        request_key=partner_book.new_request_key(),
        error=error,
        notice=notice,
    )


# What the page says when the same form was posted again (``?deja=...``): nothing new written.
ALREADY = {
    "ligne": "Cette ligne était déjà enregistrée : rien n'a été ajouté.",
    "paiement": "Ce paiement était déjà enregistré : rien n'a été ajouté.",
    "cloture": "Cette période était déjà clôturée par ce formulaire.",
}


def _read(db: Session, request: Request, auth: AuthContext, clock: Clock) -> None:
    throttle(db, request, auth, clock, write=False, limit=ratelimit.PARTNER_ACCOUNT_READ_USER)


@router.get("")
def account_list(request: Request, db: Db, clock: Now, auth: Reader) -> Response:
    _read(db, request, auth, clock)
    return page(
        "partner_accounts/list.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        rows=partner_book.account_rows(db, auth.user, clock().date()),
    )


@router.get("/{partner_id}")
def account_show(
    partner_id: PartnerId,
    request: Request,
    db: Db,
    clock: Now,
    auth: Reader,
    deja: Annotated[str | None, Query(max_length=16)] = None,
) -> Response:
    _read(db, request, auth, clock)
    return _account_page(db, auth, clock, partner_id, notice=ALREADY.get(deja or ""))


@router.get("/{partner_id}/periods/{start}")
def period_show(
    partner_id: PartnerId,
    start: str,
    request: Request,
    db: Db,
    clock: Now,
    auth: Reader,
    deja: Annotated[str | None, Query(max_length=16)] = None,
) -> Response:
    """One period: a closed one from its frozen snapshot, an open one as computed now. The only
    page that reads the snapshots."""
    _read(db, request, auth, clock)
    day = parse_day(start)
    if day is None:  # pragma: no cover - a path segment is never empty
        raise HTTPException(status_code=404)
    acc = _account_or_404(db, auth, partner_id, clock, snapshots=True)
    period = next((p for p in acc.periods if p.start == day), None)
    if period is None:
        raise HTTPException(status_code=404)
    snapshot = period.row.snapshot if period.closed and period.row is not None else None
    ids: set[int] = set()
    if snapshot is not None:
        ids = {int(p["received_by_user_id"]) for p in snapshot.get("payments", [])}
        if period.row is not None and period.row.closed_by_user_id is not None:
            ids.add(period.row.closed_by_user_id)
    return page(
        "partner_accounts/period.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        acc=acc,
        period=period,
        snapshot=snapshot,
        names=_names(db, auth.user.agency_id, ids),
        kinds=partner_book.KIND_LABELS,
        methods=partner_book.METHOD_LABELS,
        as_date=date.fromisoformat,
        as_amount=_amount,
        notice=ALREADY.get(deja or ""),
    )


def _amount(text: str) -> Decimal:
    """An amount of the snapshot (stored as text) back as a Decimal, for ``money_fr``."""
    return Decimal(text)


# --- forms ------------------------------------------------------------------------------------


def _line_form(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    partner_id: int,
    *,
    form: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    acc = _account_or_404(db, auth, partner_id, clock)
    return page(
        "partner_accounts/line_form.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        acc=acc,
        kinds=[(k, partner_book.KIND_LABELS[k]) for k in partner_book.MANUAL_KINDS],
        form=form or {"request_key": partner_book.new_request_key()},
        today=clock().date(),
        error=error,
    )


def _payment_form(
    db: Session,
    auth: AuthContext,
    clock: Clock,
    partner_id: int,
    *,
    form: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    acc = _account_or_404(db, auth, partner_id, clock)
    return page(
        "partner_accounts/payment_form.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        acc=acc,
        methods=[(m, partner_book.METHOD_LABELS[m]) for m in PARTNER_PAYMENT_METHODS],
        receivers=_receivers(db, auth.user.agency_id),
        form=form
        or {"request_key": partner_book.new_request_key(), "received_by": str(auth.user.id)},
        today=clock().date(),
        error=error,
    )


@router.get("/{partner_id}/lines/new")
def line_new(partner_id: PartnerId, db: Db, clock: Now, auth: Writer) -> Response:
    return _line_form(db, auth, clock, partner_id)


@router.post("/{partner_id}/lines")
def line_add(
    partner_id: PartnerId, request: Request, db: Db, clock: Now, auth: Writer, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.PARTNER_ACCOUNT_WRITE_USER)
    form = {
        name: _text(posted, name)
        for name in ("kind", "amount", "direction", "entry_date", "reason", "request_key")
    }
    try:
        written = partner_book.add_line(
            db,
            auth.user,
            partner_id,
            kind=form["kind"],
            amount=form["amount"],
            direction=form["direction"],
            entry_date=form["entry_date"],
            reason=form["reason"],
            request_key=form["request_key"],
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except PartnerAccountError as exc:
        db.rollback()
        status = _status(exc)
        _fresh_key_if_stale(exc, form)
        return _line_form(
            db, auth, clock, partner_id, form=form, error=str(exc), status_code=status
        )
    return _back(partner_id, None if written.created else "ligne")


@router.get("/{partner_id}/payments/new")
def payment_new(partner_id: PartnerId, db: Db, clock: Now, auth: Writer) -> Response:
    return _payment_form(db, auth, clock, partner_id)


@router.post("/{partner_id}/payments")
def payment_add(
    partner_id: PartnerId, request: Request, db: Db, clock: Now, auth: Writer, posted: Posted
) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.PARTNER_ACCOUNT_WRITE_USER)
    form = {
        name: _text(posted, name)
        for name in ("amount", "method", "paid_on", "reference", "received_by", "request_key")
    }
    try:
        written = partner_book.record_payment(
            db,
            auth.user,
            partner_id,
            amount=form["amount"],
            method=form["method"],
            paid_on=form["paid_on"],
            reference=form["reference"],
            received_by_user_id=parse_db_id(form["received_by"]),
            request_key=form["request_key"],
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except PartnerAccountError as exc:
        db.rollback()
        status = _status(exc)
        _fresh_key_if_stale(exc, form)
        return _payment_form(
            db, auth, clock, partner_id, form=form, error=str(exc), status_code=status
        )
    return _back(partner_id, None if written.created else "paiement")


@router.post("/{partner_id}/lines/{entry_id}/decision")
def line_decide(
    partner_id: PartnerId,
    entry_id: DbId,
    request: Request,
    db: Db,
    clock: Now,
    auth: Writer,
    posted: Posted,
) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.PARTNER_ACCOUNT_WRITE_USER)
    try:
        # Kept on purpose: every partner-account form posts a key, so a post without one (a
        # page from before a deploy, a forged request) is refused the same way everywhere. A
        # decision itself is idempotent by its own unique key (one decision per line).
        partner_book.check_request_key(_text(posted, "request_key"))
        partner_book.decide_line(
            db,
            auth.user,
            partner_id,
            entry_id,
            decision=_text(posted, "decision"),
            note=_text(posted, "note"),
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except PartnerAccountError as exc:
        db.rollback()
        status = _status(exc)
        return _account_page(db, auth, clock, partner_id, error=str(exc), status_code=status)
    return RedirectResponse(f"/partner-accounts/{partner_id}", status_code=303)


def _period_start(raw: str) -> date:
    day = parse_day(raw)
    if day is None:  # pragma: no cover - a path segment is never empty
        raise HTTPException(status_code=404)
    return day


@router.get("/{partner_id}/periods/{start}/close")
def period_confirm_close(
    partner_id: PartnerId, start: str, db: Db, clock: Now, auth: Writer
) -> Response:
    """The confirmation step: the totals as they would be frozen; nothing changes here."""
    day = _period_start(start)
    acc = _account_or_404(db, auth, partner_id, clock)
    period = next((p for p in acc.periods if p.start == day and not p.closed), None)
    if period is None:
        raise HTTPException(status_code=404)
    return page(
        "partner_accounts/close.html",
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
        acc=acc,
        period=period,
        request_key=partner_book.new_request_key(),
    )


@router.post("/{partner_id}/periods/{start}/close")
def period_close(
    partner_id: PartnerId,
    start: str,
    request: Request,
    db: Db,
    clock: Now,
    auth: Writer,
    posted: Posted,
) -> Response:
    throttle(db, request, auth, clock, write=True, limit=ratelimit.PARTNER_PERIOD_CLOSE_USER)
    day = _period_start(start)
    try:
        written = partner_book.close_period(
            db,
            auth.user,
            partner_id,
            day,
            request_key=_text(posted, "request_key"),
            now=clock(),
            ip=client_ip(request),
        )
        db.commit()
    except PartnerAccountError as exc:
        db.rollback()
        status = _status(exc)
        return _account_page(db, auth, clock, partner_id, error=str(exc), status_code=status)
    except DBAPIError as exc:  # a deadlock or a serialization failure: try again, never a 500
        if not is_lock_conflict(exc):
            raise
        db.rollback()
        return _account_page(db, auth, clock, partner_id, error=RETRY_LATER, status_code=409)
    again = "" if written.created else "?deja=cloture"
    return RedirectResponse(
        f"/partner-accounts/{partner_id}/periods/{day.isoformat()}{again}", status_code=303
    )


__all__ = ["router"]
