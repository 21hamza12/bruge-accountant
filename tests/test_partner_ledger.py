"""app/partner_ledger.py: the arithmetic of a partner account (half-months, due dates, alerts and
the balance of a period). Pure functions: no database.

The numbers follow the shape of the BADI workbook (fee 300, an ADM of 11 306, a refund of 12 311,
lump payments of 20 000 and 60 000) with made-up tickets."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app import partner_ledger as ledger
from app.partner_ledger import (
    ALERT_DUE_SOON,
    ALERT_NONE,
    ALERT_OVERDUE,
    LedgerEntry,
    LedgerPayment,
    alert_level,
    compute_period,
    due_date,
    half_month,
)


@dataclass(frozen=True)
class Sale:
    pb: Decimal


def d(text: str) -> Decimal:
    return Decimal(text)


# --- half_month -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "start", "end"),
    [
        (date(2026, 10, 1), date(2026, 10, 1), date(2026, 10, 15)),
        (date(2026, 10, 15), date(2026, 10, 1), date(2026, 10, 15)),
        (date(2026, 10, 16), date(2026, 10, 16), date(2026, 10, 31)),
        (date(2026, 10, 28), date(2026, 10, 16), date(2026, 10, 31)),
        (date(2026, 10, 29), date(2026, 10, 16), date(2026, 10, 31)),
        (date(2026, 10, 30), date(2026, 10, 16), date(2026, 10, 31)),
        (date(2026, 10, 31), date(2026, 10, 16), date(2026, 10, 31)),
        (date(2026, 11, 30), date(2026, 11, 16), date(2026, 11, 30)),
        (date(2026, 11, 16), date(2026, 11, 16), date(2026, 11, 30)),
        (date(2026, 12, 31), date(2026, 12, 16), date(2026, 12, 31)),
        (date(2026, 2, 28), date(2026, 2, 16), date(2026, 2, 28)),
        (date(2026, 2, 15), date(2026, 2, 1), date(2026, 2, 15)),
        (date(2028, 2, 28), date(2028, 2, 16), date(2028, 2, 29)),  # leap year
        (date(2028, 2, 29), date(2028, 2, 16), date(2028, 2, 29)),
        (date(2000, 2, 20), date(2000, 2, 16), date(2000, 2, 29)),  # divisible by 400: leap
        (date(2100, 2, 20), date(2100, 2, 16), date(2100, 2, 28)),  # divisible by 100: not
    ],
)
def test_half_month(day: date, start: date, end: date) -> None:
    assert half_month(day) == (start, end)


def test_every_day_of_four_years_falls_in_exactly_one_period() -> None:
    day, seen = date(2026, 1, 1), set()
    while day < date(2030, 1, 1):
        start, end = half_month(day)
        assert start <= day <= end
        assert start.day in (1, 16) and (start.year, start.month) == (end.year, end.month)
        assert end.day == 15 or (end + timedelta(days=1)).day == 1
        seen.add(start)
        day += timedelta(days=1)
    assert len(seen) == 4 * 24


def test_half_month_refuses_a_datetime() -> None:
    from datetime import datetime

    with pytest.raises(TypeError):
        half_month(datetime(2026, 10, 1, 12, 0))


# --- due date and alerts ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("end", "due"),
    [
        (date(2026, 10, 15), date(2026, 10, 30)),
        (date(2026, 10, 31), date(2026, 11, 15)),
        (date(2026, 12, 31), date(2027, 1, 15)),
        (date(2028, 2, 29), date(2028, 3, 15)),
        (date(2026, 2, 15), date(2026, 3, 2)),
    ],
)
def test_the_balance_is_due_fifteen_days_after_the_period_ends(end: date, due: date) -> None:
    assert due_date(end) == due


@pytest.mark.parametrize(
    ("today", "level"),
    [
        (date(2026, 10, 1), ALERT_NONE),
        (date(2026, 10, 26), ALERT_NONE),  # 4 days before
        (date(2026, 10, 27), ALERT_DUE_SOON),  # 3 days before
        (date(2026, 10, 29), ALERT_DUE_SOON),
        (date(2026, 10, 30), ALERT_DUE_SOON),  # the due day itself is not late yet
        (date(2026, 10, 31), ALERT_OVERDUE),
        (date(2027, 1, 1), ALERT_OVERDUE),
    ],
)
def test_alert_levels_around_the_due_date(today: date, level: str) -> None:
    assert alert_level(today, date(2026, 10, 30)) == level


# --- compute_period: the BADI-style example ---------------------------------------------------

SALES = (Sale(d("21668.00")), Sale(d("10300.00")))  # 21 368 + 300 and 10 000 + 300
ADM = LedgerEntry(kind="ADM", amount=d("11306.00"))
RFND = LedgerEntry(kind="RFND", amount=d("-12311.00"))
PAYMENTS = (LedgerPayment(d("20000.00")), LedgerPayment(d("60000.00")))


def test_the_badi_style_period_closes_on_the_hand_checked_balance() -> None:
    # 50 000 + (21 668 + 10 300) + (11 306 - 12 311) - (20 000 + 60 000) = 963
    totals = compute_period(d("50000.00"), SALES, (ADM, RFND), PAYMENTS)
    assert totals.opening == d("50000.00")
    assert totals.sales_total == d("31968.00")
    assert totals.entries_total == d("-1005.00")
    assert totals.payments_total == d("80000.00")
    assert totals.closing == d("963.00")
    assert totals.counted == (ADM, RFND)
    assert totals.pending == () and totals.rejected == ()


def test_a_partner_that_paid_too_much_has_a_negative_balance() -> None:
    totals = compute_period(d("0"), SALES, (ADM, RFND), PAYMENTS)
    assert totals.closing == d("-49037.00")  # Bruge owes the partner


def test_an_empty_period_carries_the_opening_balance() -> None:
    totals = compute_period(d("1234.50"), (), (), ())
    zero = Decimal("0.00")
    assert (totals.sales_total, totals.entries_total, totals.payments_total) == (zero,) * 3
    assert totals.closing == d("1234.50")


def test_every_total_has_two_decimals() -> None:
    totals = compute_period(d("0"), (Sale(d("10")),), (), ())
    for value in (totals.opening, totals.sales_total, totals.entries_total, totals.closing):
        assert value.as_tuple().exponent == -2


@pytest.mark.parametrize("kind", ["ADM", "CHANGEMENT", "CORRECTION"])
def test_lines_that_need_no_decision_always_count(kind: str) -> None:
    entry = LedgerEntry(
        kind=kind, amount=d("500.00"), source="system" if kind == "CORRECTION" else "manual"
    )
    totals = compute_period(d("0"), (), (entry,), ())
    assert totals.counted == (entry,) and totals.closing == d("500.00")


def test_a_manual_refund_counts_without_a_decision() -> None:
    totals = compute_period(d("0"), (), (RFND,), ())
    assert totals.closing == d("-12311.00")


def test_an_adjustment_counts_only_once_approved() -> None:
    waiting = LedgerEntry(kind="AJUSTEMENT", amount=d("500.00"))
    approved = LedgerEntry(kind="AJUSTEMENT", amount=d("-200.00"), decision="approve")
    refused = LedgerEntry(kind="AJUSTEMENT", amount=d("9999.00"), decision="reject")
    totals = compute_period(d("50000.00"), SALES, (ADM, RFND, waiting, approved, refused), PAYMENTS)
    assert totals.entries_total == d("-1205.00")
    assert totals.closing == d("763.00")
    assert totals.counted == (ADM, RFND, approved)
    assert totals.pending == (waiting,)
    assert totals.rejected == (refused,)


def test_a_refund_proposed_from_amadeus_counts_only_once_confirmed() -> None:
    proposed = LedgerEntry(kind="RFND", amount=d("-2000.00"), source="amadeus")
    confirmed = LedgerEntry(kind="RFND", amount=d("-1000.00"), source="amadeus", decision="confirm")
    refused = LedgerEntry(kind="RFND", amount=d("-3000.00"), source="amadeus", decision="reject")
    totals = compute_period(d("0"), (), (proposed, confirmed, refused), ())
    assert totals.closing == d("-1000.00")
    assert totals.pending == (proposed,) and totals.rejected == (refused,)


@pytest.mark.parametrize(
    "entry",
    [
        LedgerEntry(kind="AJUSTEMENT", amount=d("5.00"), decision="confirm"),
        LedgerEntry(kind="RFND", amount=d("-5.00"), source="amadeus", decision="approve"),
        LedgerEntry(kind="ADM", amount=d("5.00"), decision="approve"),
        LedgerEntry(kind="AJUSTEMENT", amount=d("5.00"), decision="maybe"),
    ],
)
def test_a_decision_that_does_not_fit_the_line_is_refused(entry: LedgerEntry) -> None:
    with pytest.raises(ValueError, match="decision"):
        compute_period(d("0"), (), (entry,), ())


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        ("ADM", "0.00"),
        ("ADM", "-1.00"),
        ("CHANGEMENT", "0.00"),
        ("CHANGEMENT", "-5.00"),
        ("RFND", "0.00"),
        ("RFND", "5.00"),
        ("AJUSTEMENT", "0.00"),
        ("CORRECTION", "0.00"),
    ],
)
def test_a_line_with_the_wrong_sign_is_refused(kind: str, amount: str) -> None:
    source = "system" if kind == "CORRECTION" else "manual"
    with pytest.raises(ValueError, match="sign"):
        compute_period(d("0"), (), (LedgerEntry(kind=kind, amount=d(amount), source=source),), ())


@pytest.mark.parametrize("amount", ["48000.00", "0.00", "-500.00"])
def test_an_opening_line_is_refused_among_the_lines(amount: str) -> None:
    """The opening balance is the ``opening`` argument: counting the line too would double it."""
    with pytest.raises(ValueError, match="OPENING"):
        compute_period(d("0"), (), (LedgerEntry(kind="OPENING", amount=d(amount)),), ())


def test_an_opening_amount_of_any_sign_fits_its_kind() -> None:
    for amount in ("48000.00", "0.00", "-500.00"):
        assert ledger.check_entry_amount("OPENING", d(amount)) == d(amount)


@pytest.mark.parametrize(
    ("kind", "source"),
    [
        ("CORRECTION", "manual"),
        ("CORRECTION", "amadeus"),
        ("ADM", "system"),
        ("RFND", "system"),
        ("AJUSTEMENT", "system"),
        ("ADM", "amadeus"),
        ("CHANGEMENT", "amadeus"),
        ("AJUSTEMENT", "amadeus"),
    ],
)
def test_a_source_that_does_not_fit_the_kind_is_refused(kind: str, source: str) -> None:
    """Mirror of the CHECKs of ``partner_entries``: a CORRECTION is exactly a system line, and
    Amadeus only proposes refunds. Python never counts a line the database would refuse."""
    amount = d("-5.00") if kind == "RFND" else d("5.00")
    with pytest.raises(ValueError, match="source"):
        compute_period(d("0"), (), (LedgerEntry(kind=kind, amount=amount, source=source),), ())


@pytest.mark.parametrize("bad", [d("1E+30"), d("1000000000000.00"), d("-1000000000000.00")])
def test_amounts_beyond_the_column_are_refused_with_a_value_error(bad: Decimal) -> None:
    """Numeric(14, 2) holds less than 10**12; quantizing 1E+30 would raise InvalidOperation."""
    with pytest.raises(ValueError, match="too large"):
        compute_period(bad, (), (), ())
    with pytest.raises(ValueError, match="too large"):
        compute_period(d("0"), (Sale(bad),), (), ())
    with pytest.raises(ValueError, match="too large"):
        compute_period(d("0"), (), (), (LedgerPayment(abs(bad)),))


def test_the_largest_amount_of_the_column_is_accepted() -> None:
    top = d("999999999999.99")
    assert compute_period(top, (), (), ()).closing == top


@pytest.mark.parametrize("bad", [d("0"), d("-1.00")])
def test_a_payment_must_be_positive(bad: Decimal) -> None:
    with pytest.raises(ValueError, match="payment"):
        compute_period(d("0"), (), (), (LedgerPayment(bad),))


def test_unknown_kinds_and_sources_are_refused() -> None:
    with pytest.raises(ValueError, match="kind"):
        compute_period(d("0"), (), (LedgerEntry(kind="BONUS", amount=d("1.00")),), ())
    with pytest.raises(ValueError, match="source"):
        compute_period(d("0"), (), (LedgerEntry(kind="ADM", amount=d("1"), source="excel"),), ())


@pytest.mark.parametrize(
    "case",
    ["opening", "sale", "entry", "payment"],
)
def test_floats_are_refused_everywhere(case: str) -> None:
    opening: object = d("0")
    sales: tuple[object, ...] = ()
    entries: tuple[LedgerEntry, ...] = ()
    payments: tuple[LedgerPayment, ...] = ()
    if case == "opening":
        opening = 1.5
    elif case == "sale":
        sales = (Sale(1.5),)  # type: ignore[arg-type]
    elif case == "entry":
        entries = (LedgerEntry(kind="ADM", amount=1.5),)  # type: ignore[arg-type]
    else:
        payments = (LedgerPayment(1.5),)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        compute_period(opening, sales, entries, payments)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [d("1.005"), d("NaN"), d("Infinity")])
def test_more_than_two_decimals_and_non_finite_amounts_are_refused(bad: Decimal) -> None:
    with pytest.raises(ValueError):
        compute_period(bad, (), (), ())
    with pytest.raises(ValueError):
        compute_period(d("0"), (Sale(bad),), (), ())


def test_the_balance_always_reconciles() -> None:
    """Property: for random periods, closing = opening + sales + counted lines - payments, the
    lines are split without loss, and pending or rejected lines never move the balance."""
    rng = random.Random(20261027)  # noqa: S311 - test data, not security

    def money(low: int, high: int) -> Decimal:
        return Decimal(rng.randint(low, high)) / 100

    kinds = [
        ("ADM", "manual", None, (1, 10**7)),
        ("CHANGEMENT", "manual", None, (1, 10**7)),
        ("RFND", "manual", None, (-(10**7), -1)),
        ("CORRECTION", "system", None, (1, 10**7)),
        ("AJUSTEMENT", "manual", rng.choice, (1, 10**7)),
        ("RFND", "amadeus", rng.choice, (-(10**7), -1)),
    ]
    for _ in range(300):
        opening = money(-(10**9), 10**9)
        sales = tuple(Sale(money(0, 10**8)) for _ in range(rng.randint(0, 12)))
        entries = []
        for _ in range(rng.randint(0, 10)):
            kind, source, choose, (low, high) = rng.choice(kinds)
            decision = None
            if choose is not None:
                decision = rng.choice(
                    [None, "reject", "approve" if kind == "AJUSTEMENT" else "confirm"]
                )
            amount = money(low, high)
            if kind == "AJUSTEMENT" and rng.random() < 0.5:
                amount = -amount
            entries.append(LedgerEntry(kind=kind, amount=amount, source=source, decision=decision))
        payments = tuple(LedgerPayment(money(1, 10**8)) for _ in range(rng.randint(0, 5)))
        totals = compute_period(opening, sales, tuple(entries), payments)
        counted = sum((e.amount for e in totals.counted), Decimal(0))
        assert totals.entries_total == counted
        assert totals.sales_total == sum((s.pb for s in sales), Decimal(0))
        assert totals.payments_total == sum((p.amount for p in payments), Decimal(0))
        assert totals.closing == opening + totals.sales_total + counted - totals.payments_total
        assert len(totals.counted) + len(totals.pending) + len(totals.rejected) == len(entries)
        assert all(ledger.needs_decision(e.kind, e.source) for e in totals.pending)
        assert all(e.decision is None for e in totals.pending)
