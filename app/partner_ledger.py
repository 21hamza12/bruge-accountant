"""Partner accounts: the arithmetic of a half-month period. Pure functions, no database.

* **Periods** (``half_month``): 1-15 and 16-last day of the month.
* **Deadline** (``due_date``, ``alert_level``, decision 19): a closed period's balance is due 15
  days after the period ends; ``due_soon`` from 3 days before the due day (the due day included),
  ``overdue`` from the day after.
* **Balance** (``compute_period``): ``closing = opening + sales + counted lines - payments``. A
  positive balance means the partner owes the agency. The partner's ``OPENING`` line is not a
  line here: it is the ``opening`` of its first period, so passing it among the lines (it would
  count twice) raises. A line counts when it needs no decision
  (``ADM``, ``CHANGEMENT``, ``CORRECTION``, a manual ``RFND``) or when its decision
  is ``approve`` (an ``AJUSTEMENT``) or ``confirm`` (an ``RFND`` proposed from Amadeus). A line
  still waiting is returned in ``pending``, a refused one in ``rejected``; neither moves the
  balance.

Amounts are ``Decimal`` with at most two decimals, never ``float``: anything else raises
(``TypeError`` for a wrong type, ``ValueError`` for a wrong value), so a bug can never turn into
a wrong balance. The rules on signs mirror the CHECKs of ``partner_entries`` (migration 0017).
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.models import (
    DECISION_APPROVE,
    DECISION_CONFIRM,
    DECISION_REJECT,
    ENTRY_ADM,
    ENTRY_AJUSTEMENT,
    ENTRY_CHANGEMENT,
    ENTRY_CORRECTION,
    ENTRY_KINDS,
    ENTRY_OPENING,
    ENTRY_RFND,
    ENTRY_SOURCE_AMADEUS,
    ENTRY_SOURCE_MANUAL,
    ENTRY_SOURCE_SYSTEM,
    ENTRY_SOURCES,
)

CENT = Decimal("0.01")
MAX_AMOUNT = Decimal(10) ** 12  # exclusive: the columns are Numeric(14, 2)
DUE_AFTER_DAYS = 15
DUE_SOON_DAYS = 3
ALERT_NONE = "none"
ALERT_DUE_SOON = "due_soon"
ALERT_OVERDUE = "overdue"
ZERO = Decimal("0.00")


# --- periods and deadlines --------------------------------------------------------------------


def half_month(day: date) -> tuple[date, date]:
    """The period holding ``day``: (the 1st, the 15th) or (the 16th, the last day of the month)."""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError("half_month takes a date")
    if day.day <= 15:
        return day.replace(day=1), day.replace(day=15)
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=16), day.replace(day=last)


def due_date(period_end: date) -> date:
    """The day a closed period's balance is due: 15 days after its last day."""
    return period_end + timedelta(days=DUE_AFTER_DAYS)


def alert_level(today: date, due: date) -> str:
    """``overdue`` after the due day, ``due_soon`` from 3 days before it, else ``none``."""
    if today > due:
        return ALERT_OVERDUE
    if (due - today).days <= DUE_SOON_DAYS:
        return ALERT_DUE_SOON
    return ALERT_NONE


# --- the balance of a period ------------------------------------------------------------------


class Priced(Protocol):
    """A ticket sale line of the period: only its P.B (cost + fee) matters to the balance."""

    @property
    def pb(self) -> Decimal: ...


@dataclass(frozen=True)
class LedgerEntry:
    """One manual (or automatic) line, with the decision taken on it if any."""

    kind: str
    amount: Decimal
    source: str = ENTRY_SOURCE_MANUAL
    decision: str | None = None
    entry_id: int | None = None


@dataclass(frozen=True)
class LedgerPayment:
    amount: Decimal
    payment_id: int | None = None


@dataclass(frozen=True)
class PeriodTotals:
    opening: Decimal
    sales_total: Decimal
    entries_total: Decimal  # the counted lines only
    payments_total: Decimal
    closing: Decimal
    counted: tuple[LedgerEntry, ...]
    pending: tuple[LedgerEntry, ...]
    rejected: tuple[LedgerEntry, ...]


def _money(value: object, what: str) -> Decimal:
    """``value`` if it fits a ``Numeric(14, 2)`` column; ValueError (never InvalidOperation)
    otherwise, TypeError when it is not a Decimal."""
    if not isinstance(value, Decimal):
        raise TypeError(f"{what} must be a Decimal, not {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"{what} must be a finite amount")
    if abs(value) >= MAX_AMOUNT:  # before quantize, which raises on a huge exponent
        raise ValueError(f"{what} is too large for an amount")
    try:
        rounded = value.quantize(CENT)
    except InvalidOperation:
        raise ValueError(f"{what} is too large for an amount") from None
    if value != rounded:
        raise ValueError(f"{what} has more than two decimals")
    return value


def needs_decision(kind: str, source: str) -> bool:
    """An ``AJUSTEMENT``, and an ``RFND`` proposed from Amadeus, count only once decided."""
    return kind == ENTRY_AJUSTEMENT or (kind == ENTRY_RFND and source == ENTRY_SOURCE_AMADEUS)


def check_entry_amount(kind: str, amount: Decimal) -> Decimal:
    """``amount`` if its sign fits ``kind`` (the CHECK of ``partner_entries``), else ValueError."""
    if kind not in ENTRY_KINDS:
        raise ValueError(f"unknown entry kind {kind!r}")
    _money(amount, "an entry amount")
    if kind in (ENTRY_ADM, ENTRY_CHANGEMENT) and amount <= 0:
        raise ValueError(f"the sign of a {kind} line must be positive")
    if kind == ENTRY_RFND and amount >= 0:
        raise ValueError("the sign of an RFND line must be negative")
    if kind in (ENTRY_AJUSTEMENT, ENTRY_CORRECTION) and amount == 0:
        raise ValueError(f"the sign of a {kind} line cannot be zero")
    return amount


def check_entry_source(kind: str, source: str) -> str:
    """``source`` if it fits ``kind`` (the CHECKs of ``partner_entries``: a ``CORRECTION`` is
    exactly a ``system`` line, and Amadeus only proposes ``RFND`` lines), else ValueError."""
    if source not in ENTRY_SOURCES:
        raise ValueError(f"unknown entry source {source!r}")
    if (kind == ENTRY_CORRECTION) != (source == ENTRY_SOURCE_SYSTEM):
        raise ValueError(f"the source {source!r} does not fit a {kind} line")
    if source == ENTRY_SOURCE_AMADEUS and kind != ENTRY_RFND:
        raise ValueError(f"the source {source!r} does not fit a {kind} line")
    return source


def _accepting_decision(entry: LedgerEntry) -> str | None:
    """The decision that makes ``entry`` count, or None when it needs no decision; raises when
    the entry carries a decision that does not fit it (the database refuses that too, since
    migration 0017's decision CHECK: here it would mean a bug)."""
    check_entry_source(entry.kind, entry.source)
    if not needs_decision(entry.kind, entry.source):
        if entry.decision is not None:
            raise ValueError(f"a {entry.kind} line takes no decision")
        return None
    accepting = DECISION_APPROVE if entry.kind == ENTRY_AJUSTEMENT else DECISION_CONFIRM
    if entry.decision not in (None, accepting, DECISION_REJECT):
        raise ValueError(f"the decision {entry.decision!r} does not fit a {entry.kind} line")
    return accepting


def compute_period(
    opening: Decimal,
    sale_lines: Iterable[Priced],
    entries: Iterable[LedgerEntry],
    payments: Iterable[LedgerPayment],
) -> PeriodTotals:
    """The totals and the closing balance of one period (see the module docstring)."""
    opening = _money(opening, "the opening balance")
    sales_total = sum((_money(line.pb, "a sale's P.B") for line in sale_lines), ZERO)
    counted: list[LedgerEntry] = []
    pending: list[LedgerEntry] = []
    rejected: list[LedgerEntry] = []
    for entry in entries:
        if entry.kind == ENTRY_OPENING:
            raise ValueError(
                "an OPENING line is the opening balance: pass it as ``opening``, not as a line"
            )
        check_entry_amount(entry.kind, entry.amount)
        accepting = _accepting_decision(entry)
        if accepting is None or entry.decision == accepting:
            counted.append(entry)
        elif entry.decision is None:
            pending.append(entry)
        else:
            rejected.append(entry)
    entries_total = sum((e.amount for e in counted), ZERO)
    paid = []
    for payment in payments:
        if _money(payment.amount, "a payment") <= 0:
            raise ValueError("a payment must be positive")
        paid.append(payment.amount)
    payments_total = sum(paid, ZERO)
    closing = opening + sales_total + entries_total - payments_total
    return PeriodTotals(
        opening=opening.quantize(CENT),
        sales_total=sales_total.quantize(CENT),
        entries_total=entries_total.quantize(CENT),
        payments_total=payments_total.quantize(CENT),
        closing=closing.quantize(CENT),
        counted=tuple(counted),
        pending=tuple(pending),
        rejected=tuple(rejected),
    )
