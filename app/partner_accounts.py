"""Partner accounts: reading what the database holds for one partner (phase F).

F1 has one read: ``partner_sale_lines`` (``partner_sale_lines_many`` for several partners in
one query), the ticket lines of one partner over a date range, which
``app.partner_ledger.compute_period`` sums. A line is the CURRENT version of an ACTIVE sale whose
current version names this partner (``ag_kind = 'partner'``), filed under a report day inside the
range, both ends included (owner's answer 2: a sale belongs to the period of the day it is filed
under, the ticket's issue day since PR #38). A voided sale, a direct sale and another partner's sale
are left out; an edited sale counts with its latest content, and a sale moved to another day or
partner follows it.

Every query filters on the agency explicitly, on top of row-level security (SQLite has none, and a
forgotten filter must not become an IDOR there either): a partner id of another agency finds
nothing. ORM only; nothing here writes.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import AG_PARTNER, SALE_ACTIVE, ReportDay, Sale, SaleVersion
from app.sales_rules import TicketLine, lines_from_json

BAD_RANGE = "the date range is reversed: start must be on or before end"


@dataclass(frozen=True)
class PartnerSaleLine:
    """One partner sale as it stands now: its tickets (numbers and costs), the fee applied per
    ticket and its P.B (Σ cost + fee × tickets, decision 22)."""

    sale_id: int
    version_no: int
    day: date
    tickets: tuple[TicketLine, ...]
    fee: Decimal
    cost_total: Decimal
    pb: Decimal


def partner_sale_lines(
    db: Session, agency_id: int, partner_id: int, start: date, end: date
) -> list[PartnerSaleLine]:
    """The lines of ``partner_id`` (of ``agency_id``) filed from ``start`` to ``end`` included,
    by day then sale id. One query; uses ``ix_sale_versions_agency_partner``."""
    return partner_sale_lines_many(db, agency_id, [partner_id], start, end).get(partner_id, [])


def partner_sale_lines_many(
    db: Session, agency_id: int, partner_ids: Collection[int], start: date, end: date
) -> dict[int, list[PartnerSaleLine]]:
    """``partner_sale_lines`` for several partners of one agency in ONE query (the list of every
    partner account): partner id to its lines, by day then sale id. A partner without a line is
    absent from the result."""
    if start > end:
        raise ValueError(BAD_RANGE)
    lines: dict[int, list[PartnerSaleLine]] = {}
    if not partner_ids:
        return lines
    query = (
        select(Sale.id, SaleVersion, ReportDay.day)
        .join(
            SaleVersion,
            and_(
                SaleVersion.agency_id == Sale.agency_id,
                SaleVersion.sale_id == Sale.id,
                SaleVersion.version_no == Sale.current_version,
            ),
        )
        .join(
            ReportDay,
            and_(ReportDay.agency_id == Sale.agency_id, ReportDay.id == Sale.report_day_id),
        )
        .where(
            Sale.agency_id == agency_id,
            SaleVersion.agency_id == agency_id,
            Sale.status == SALE_ACTIVE,
            SaleVersion.voided.is_(False),
            SaleVersion.ag_kind == AG_PARTNER,
            SaleVersion.partner_id.in_(sorted(set(partner_ids))),
            ReportDay.day >= start,
            ReportDay.day <= end,
        )
        .order_by(ReportDay.day, Sale.id)
    )
    for sale_id, version, day in db.execute(query):
        if version.fee_applied is None:  # the CHECK partner_sale_has_fee makes this impossible
            raise RuntimeError(f"partner sale {sale_id} has no fee")
        if version.partner_id is None:  # pragma: no cover - the WHERE above
            continue
        lines.setdefault(version.partner_id, []).append(
            PartnerSaleLine(
                sale_id=sale_id,
                version_no=version.version_no,
                day=day,
                tickets=lines_from_json(version.tickets),
                fee=version.fee_applied,
                cost_total=version.cost_total,
                pb=version.pb,
            )
        )
    return lines
