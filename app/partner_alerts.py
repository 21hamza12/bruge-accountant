"""Partner accounts on the home page (phase F, slice F4): what is overdue, what is due soon and
which refunds proposed from the Amadeus report wait for a decision.

Read for the manager and the accountant only (``permissions.can_manage_partners``); anyone else
gets ``None`` and the page shows nothing. Built on ``partner_book.account_rows``, which reads every
partner of the agency in a constant number of queries (never one per partner).

* **Per partner**, the oldest closed period that still lacks money (``partner_book.oldest_unpaid``,
  first in, first out): « en retard » after its due day (period end + 15 days), « bientôt dû »
  from 3 days before it (``partner_ledger.alert_level``). A partner whose oldest debt is not due
  yet is not listed.
* **Refunds to confirm**: the ``RFND`` lines proposed from the Amadeus report (``source =
  amadeus``) that nobody confirmed or rejected yet. They do not move a balance until confirmed.

Nothing here writes; the agency is the actor's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app import partner_book, permissions
from app.models import User
from app.partner_ledger import ALERT_DUE_SOON, ALERT_OVERDUE


@dataclass(frozen=True)
class PartnerAlert:
    """The oldest unpaid closed period of one partner, due soon or overdue."""

    partner_id: int
    name: str
    amount: Decimal  # what the period still lacks
    start: date
    end: date
    due: date
    level: str  # partner_ledger.ALERT_DUE_SOON or ALERT_OVERDUE
    today: date

    @property
    def days_late(self) -> int:
        """Days since the due day (0 when not overdue yet)."""
        return max(0, (self.today - self.due).days)

    @property
    def days_left(self) -> int:
        """Days until the due day (0 on the day itself or after)."""
        return max(0, (self.due - self.today).days)

    @property
    def overdue(self) -> bool:
        return self.level == ALERT_OVERDUE


@dataclass(frozen=True)
class PendingRefund:
    """An RFND proposed from the Amadeus report, still « à confirmer »."""

    partner_id: int
    name: str
    entry_id: int
    amount: Decimal  # negative
    doc_number: str | None
    entry_date: date


@dataclass(frozen=True)
class PartnerAlerts:
    overdue: tuple[PartnerAlert, ...]  # the longest overdue first
    due_soon: tuple[PartnerAlert, ...]  # the nearest due day first
    refunds: tuple[PendingRefund, ...]  # by partner, then line

    @property
    def any(self) -> bool:
        return bool(self.overdue or self.due_soon or self.refunds)

    @property
    def owing(self) -> tuple[PartnerAlert, ...]:
        """Overdue then due soon: the « qui doit de l'argent ? » order."""
        return self.overdue + self.due_soon


def partner_alerts(db: Session, actor: User, today: date) -> PartnerAlerts | None:
    """The alerts of the actor's agency on ``today``; None for a role that may not see partner
    accounts (staff)."""
    if not permissions.can_manage_partners(actor):
        return None
    overdue: list[PartnerAlert] = []
    due_soon: list[PartnerAlert] = []
    refunds: list[PendingRefund] = []
    for row in partner_book.account_rows(db, actor, today):
        oldest = row.oldest
        if oldest is not None and oldest.level in (ALERT_OVERDUE, ALERT_DUE_SOON):
            alert = PartnerAlert(
                partner_id=row.partner.id,
                name=row.partner.name,
                amount=oldest.amount,
                start=oldest.start,
                end=oldest.end,
                due=oldest.due,
                level=oldest.level,
                today=today,
            )
            (overdue if oldest.level == ALERT_OVERDUE else due_soon).append(alert)
        refunds.extend(
            PendingRefund(
                partner_id=row.partner.id,
                name=row.partner.name,
                entry_id=entry.id,
                amount=entry.amount,
                doc_number=entry.doc_number,
                entry_date=entry.entry_date,
            )
            for entry in sorted(row.pending_refunds, key=lambda e: (e.entry_date, e.id))
        )
    overdue.sort(key=lambda a: (-a.days_late, a.name.casefold(), a.partner_id))
    due_soon.sort(key=lambda a: (a.due, a.name.casefold(), a.partner_id))
    refunds.sort(key=lambda r: (r.name.casefold(), r.partner_id, r.entry_date, r.entry_id))
    return PartnerAlerts(tuple(overdue), tuple(due_soon), tuple(refunds))
