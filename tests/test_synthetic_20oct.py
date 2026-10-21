"""The made-up 20 October report (tests/fixtures/synthetic_20oct.py) is what it claims to be."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal

from app.parsing.amadeus import ParsedReport, parse_report
from tests.fixtures import synthetic_20oct as fx


def parsed() -> ParsedReport:
    return parse_report(fx.build_synthetic_20oct())


def test_it_parses_with_the_real_parser_and_no_warnings() -> None:
    report = parsed()
    assert report.warnings == ()
    assert report.header.report_date == date(2026, 10, 20)
    assert report.header.generated_on == date(2026, 10, 21)
    assert len(report.tickets) == 43
    assert all(t.flags in ((), ("cancelled",)) for t in report.tickets)


def test_counts_and_totals() -> None:
    tickets = parsed().tickets
    issued = [t for t in tickets if t.transaction_type == "TKTT"]
    cancelled = [t for t in tickets if t.transaction_type == "CANX"]
    assert (len(issued), len(cancelled)) == (34, 9)
    assert (fx.TKTT_COUNT, fx.CANX_COUNT) == (34, 9)
    assert sum((t.cost or Decimal(0)) for t in issued) == Decimal(624_126)
    assert sum((t.cost or Decimal(0)) for t in cancelled) == Decimal(177_372)
    assert (fx.TKTT_TOTAL, fx.CANX_TOTAL) == (624_126, 177_372)


def test_signs_and_airlines() -> None:
    tickets = parsed().tickets
    assert {t.staff_sign for t in tickets} == {"0004SV", "0006AC", "0009ER"}
    assert {t.airline for t in tickets} == {"199", "147", "495", "235", "169", "124"}


def test_the_cancel_and_reissue_pair_exists() -> None:
    pair = [t for t in parsed().tickets if t.booking_ref == fx.PAIR_RLOC]
    assert sorted(t.transaction_type for t in pair) == ["CANX", "TKTT"]
    assert {t.passenger_name for t in pair} == {fx.PAIR_PAX}
    assert {t.cost for t in pair} == {Decimal(fx.PAIR_AMOUNT)}
    assert len({t.ticket_no for t in pair}) == 2


def test_two_zero_cost_495_lines_share_a_booking_reference() -> None:
    zero = [
        t
        for t in parsed().tickets
        if t.airline == "495" and t.transaction_type == "TKTT" and t.cost == 0
    ]
    assert len(zero) == 2
    assert {t.booking_ref for t in zero} == {fx.ZERO_COST_RLOC}


def test_comm_values_and_form_of_payment() -> None:
    tickets = parsed().tickets
    assert any((t.comm or 0) > 0 for t in tickets)
    assert {t.form_of_payment for t in tickets} == {"CA"}
    assert all(t.tax is not None and t.comm is not None for t in tickets)


def test_document_numbers_are_unique_and_names_are_placeholders() -> None:
    tickets = parsed().tickets
    assert max(Counter(t.ticket_no for t in tickets).values()) == 1
    assert all(t.passenger_name.startswith("SYNTH") for t in tickets)
    assert [t.seq_no for t in tickets] == [f"{n:06d}" for n in range(1, 44)]
