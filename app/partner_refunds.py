"""Refunds of partner tickets proposed from the Amadeus report (phase F, slice F4; owner's answer
3): the report shows an ``RFND`` line for a ticket of an active partner sale, so the application
writes an ``RFND`` line on that partner's account, ``source = amadeus``, with the ticket number,
the refund amount of the report (negative) and no decision. It counts only once the accountant or
the manager confirms it (``partner_book.decide_line``); until then it is « à confirmer ».

Called by ``flags.run_comparison`` (after every upload, and on every comparison run again), in
the comparison's transaction, with the report lines it compared.

* **Which lines.** ``TRNC = RFND``, a usable ticket number (``comparison.normalise_doc``), held by
  an ACTIVE sale whose current version is a PARTNER sale (``sales.active_ticket_lookup``): the
  line goes to THAT sale's partner. A direct sale, a voided sale and an unknown ticket propose
  nothing.
* **Amount.** The line's TOTAL DOC, which must be negative with at most two decimals; a missing,
  zero, positive or odd amount proposes nothing and is logged with ids only (no amount, no name).
* **Once per ticket, ever.** A ticket that already has an Amadeus ``RFND`` line in the agency
  (pending, confirmed or rejected) is skipped, and the row carries the deterministic
  ``request_key`` ``amadeus-rfnd-<ticket>``, unique per agency in the database
  (``ix_partner_entries_request_key``): two comparisons racing still write one row, the loser's
  savepoint is rolled back and it moves on.
* **Never backdated.** Under the partner's shared advisory lock (migration 0017's insert guard),
  the line is dated on the report day when that day is after the latest closed period's end (and
  not after today), else today; a closed period always ends before today (``close_period``
  needs the period over), so the date is always in an open period. A report day before the
  partner's switch-over day proposes nothing (the spreadsheet's balance holds it). Should the
  database still refuse the row, the comparison goes on and the refusal is logged.
* **Who.** ``created_by_user_id`` is the person whose upload or comparison run proposed the line;
  the decision is the accountant's or the manager's, as for every Amadeus ``RFND``.

Audit: ``partner_refund_proposed`` with ids, the amount and the date only. Never commits.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, sales
from app.comparison import normalise_doc
from app.formatting import date_fr
from app.models import (
    AG_PARTNER,
    ENTRY_RFND,
    ENTRY_SOURCE_AMADEUS,
    PartnerEntry,
    User,
)
from app.partner_book import (
    is_closed_period_refusal,
    latest_closed_end,
    lock_partner,
    opening_entry,
)
from app.partner_ledger import check_entry_amount, check_entry_source

log = logging.getLogger(__name__)

TRNC_REFUND = "RFND"
KEY_PREFIX = "amadeus-rfnd-"
PROPOSED = "partner_refund_proposed"  # audit event: ids, amount and date only
REASON = "Remboursement du billet {doc} vu dans le rapport Amadeus du {day}, à vérifier"

# why a line proposed nothing (logged, with ids only)
SKIP_AMOUNT = "amount"
SKIP_BEFORE_OPENING = "before_opening"
SKIP_REFUSED = "refused"


class RefundLine(Protocol):
    """What a report line must tell: ``comparison.AmadeusTicket`` fits."""

    @property
    def doc_number(self) -> str: ...
    @property
    def airline(self) -> str: ...
    @property
    def trnc(self) -> str: ...
    @property
    def total_doc(self) -> Decimal | None: ...


@dataclass(frozen=True)
class _Candidate:
    doc: str
    amount: Decimal | None
    sale_id: int
    partner_id: int


def request_key(doc: str) -> str:
    """The deterministic key of the proposal for one ticket (unique per agency)."""
    return KEY_PREFIX + doc


def _refund_amount(raw: Decimal | None) -> Decimal | None:
    """The amount of an RFND line if it fits (negative, two decimals at most), else None."""
    if raw is None:
        return None
    try:
        return check_entry_amount(ENTRY_RFND, raw)
    except (TypeError, ValueError):
        return None


def _skip(why: str, report_day_id: int | None, sale_id: int, partner_id: int) -> None:
    log.warning(
        "amadeus refund not proposed",
        extra={
            "why": why,
            "report_day_id": report_day_id,
            "sale_id": sale_id,
            "partner_id": partner_id,
        },
    )


def _candidates(db: Session, agency_id: int, lines: Iterable[RefundLine]) -> list[_Candidate]:
    """The RFND lines whose ticket belongs to an active partner sale, one per ticket (the first
    line of a ticket wins), by ticket number. Two queries whatever the number of lines."""
    amounts: dict[str, Decimal | None] = {}
    for line in lines:
        if line.trnc.strip().upper() != TRNC_REFUND:
            continue
        doc = normalise_doc(line.doc_number, line.airline)
        if doc and doc not in amounts:
            amounts[doc] = line.total_doc
    if not amounts:
        return []
    located = sales.active_ticket_lookup(db, agency_id, list(amounts))
    if not located:
        return []
    views = {
        v.id: v
        for v in sales.agency_sales_by_id(db, agency_id, {sale for sale, _ in located.values()})
    }
    found: list[_Candidate] = []
    for doc in sorted(located):
        view = views.get(located[doc][0])
        if view is None or view.ag_kind != AG_PARTNER or view.partner_id is None:
            continue
        found.append(_Candidate(doc, amounts[doc], view.id, view.partner_id))
    return found


def _already_proposed(db: Session, agency_id: int, docs: list[str]) -> set[str]:
    if not docs:
        return set()
    rows = db.scalars(
        select(PartnerEntry.doc_number).where(
            PartnerEntry.agency_id == agency_id,
            PartnerEntry.kind == ENTRY_RFND,
            PartnerEntry.source == ENTRY_SOURCE_AMADEUS,
            PartnerEntry.doc_number.in_(docs),
        )
    )
    return {doc for doc in rows if doc is not None}


def _entry_by_key(db: Session, agency_id: int, key: str) -> PartnerEntry | None:
    return db.scalars(
        select(PartnerEntry).where(
            PartnerEntry.agency_id == agency_id, PartnerEntry.request_key == key
        )
    ).one_or_none()


def proposal_date(
    report_day: date, today: date, closed_end: date | None, opening: date | None
) -> date | None:
    """The date of a proposal: the report day when it is after the latest closed end (and not
    after today), else today; None when the report day is before the switch-over day."""
    if opening is not None and report_day < opening:
        return None
    if report_day <= today and (closed_end is None or report_day > closed_end):
        return report_day
    return today


def _propose(
    db: Session,
    actor: User,
    candidate: _Candidate,
    amount: Decimal,
    report_day: date,
    report_day_id: int | None,
    now: datetime,
    ip: str | None,
) -> PartnerEntry | None:
    agency_id = actor.agency_id
    lock_partner(db, candidate.partner_id, shared=True)
    key = request_key(candidate.doc)
    if _entry_by_key(db, agency_id, key) is not None:  # proposed meanwhile, under the lock
        return None
    opening = opening_entry(db, agency_id, candidate.partner_id)
    day = proposal_date(
        report_day,
        now.date(),
        latest_closed_end(db, agency_id, candidate.partner_id),
        None if opening is None else opening.entry_date,
    )
    if day is None:
        _skip(SKIP_BEFORE_OPENING, report_day_id, candidate.sale_id, candidate.partner_id)
        return None
    row = PartnerEntry(
        agency_id=agency_id,
        partner_id=candidate.partner_id,
        entry_date=day,
        kind=ENTRY_RFND,
        amount=amount,
        reason=REASON.format(doc=candidate.doc, day=date_fr(report_day)),
        source=check_entry_source(ENTRY_RFND, ENTRY_SOURCE_AMADEUS),
        doc_number=candidate.doc,
        created_by_user_id=actor.id,
        request_key=key,
        created_at=now,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        if _entry_by_key(db, agency_id, key) is None:
            why = "closed_period" if is_closed_period_refusal(exc) else SKIP_REFUSED
            _skip(why, report_day_id, candidate.sale_id, candidate.partner_id)
        return None
    audit.record(
        db,
        PROPOSED,
        agency_id=agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "partner_id": candidate.partner_id,
            "entry_id": row.id,
            "sale_id": candidate.sale_id,
            "report_day_id": report_day_id,
            "amount": str(amount),
            "entry_date": day.isoformat(),
        },
        now=now,
    )
    return row


def propose_refunds(
    db: Session,
    actor: User,
    report_day: date,
    lines: Iterable[RefundLine],
    *,
    now: datetime,
    report_day_id: int | None = None,
    ip: str | None = None,
) -> list[PartnerEntry]:
    """Propose an ``RFND`` line for every refund of a partner ticket among ``lines`` (the report
    of ``report_day``) that was never proposed before; returns the rows written now."""
    agency_id = actor.agency_id
    candidates = _candidates(db, agency_id, lines)
    done = _already_proposed(db, agency_id, [c.doc for c in candidates])
    written: list[PartnerEntry] = []
    for candidate in candidates:
        if candidate.doc in done:
            continue
        amount = _refund_amount(candidate.amount)
        if amount is None:
            _skip(SKIP_AMOUNT, report_day_id, candidate.sale_id, candidate.partner_id)
            continue
        row = _propose(db, actor, candidate, amount, report_day, report_day_id, now, ip)
        if row is not None:
            written.append(row)
    return written
