from __future__ import annotations

import dataclasses
import random
from decimal import Decimal

from app.parsing.amadeus import ParsedReport, ParsedTicket
from app.questions import StaffQuestions, build_questions
from tests.builders import build_workbook, ticket_row


def make(**overrides: object) -> ParsedTicket:
    base = ParsedTicket(
        seq_no="000001",
        airline="199",
        ticket_no="3940000001",
        cost=Decimal(1000),
        passenger_name="DOE/JOHN",
        pax_type="ADT",
        staff_sign="0004SV",
        transaction_type="TKTT",
        booking_ref="AAAAAA",
        flags=(),
        source_row=6,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def staff(result: list[StaffQuestions], sign: str) -> StaffQuestions:
    return next(s for s in result if s.staff_sign == sign)


# --- acceptance on the real fixture -------------------------------------------------


def test_fixture_acceptance(parsed_fixture: ParsedReport) -> None:
    result = build_questions(parsed_fixture.tickets)
    assert [s.staff_sign for s in result] == ["0004SV", "0009ER"]
    sv, er = result
    assert (sv.ticket_count, sv.group_count, sv.total_cost) == (34, 25, Decimal(746423))
    assert (er.ticket_count, er.group_count, er.total_cost) == (14, 8, Decimal(372434))
    assert sv.group_count + er.group_count == 33
    assert len(sv.questions) == 25


def test_fixture_mixed_airline_groups(parsed_fixture: ParsedReport) -> None:
    sv = staff(build_questions(parsed_fixture.tickets), "0004SV")
    by_ref = {q.booking_ref: q for q in sv.questions}
    assert by_ref["QA0011"].airlines == ("235", "495")
    assert by_ref["QA0011"].ticket_count == 2
    assert by_ref["QA0011"].total_cost == Decimal(14160 + 42284)
    assert by_ref["QA0013"].airlines == ("157", "199")
    assert by_ref["QA0013"].ticket_count == 3
    assert [p.name for p in by_ref["QA0013"].passengers] == ["PAX24/GIVEN24"] * 3


def test_fixture_passenger_fields(parsed_fixture: ParsedReport) -> None:
    er = staff(build_questions(parsed_fixture.tickets), "0009ER")
    q = next(q for q in er.questions if q.booking_ref == "QA0030")
    assert q.ticket_count == 3
    assert {p.pax_type for p in q.passengers} == {"CHD", "ADT", "INF"}
    inf = next(p for p in q.passengers if p.pax_type == "INF")
    assert (inf.name, inf.ticket_no, inf.airline, inf.cost) == (
        "PAX02/GIVEN02",
        "9990000156",
        "495",
        Decimal(901),
    )


def test_fixture_questions_sorted_by_booking_ref(parsed_fixture: ParsedReport) -> None:
    for s in build_questions(parsed_fixture.tickets):
        refs = [q.booking_ref for q in s.questions]
        assert refs == sorted(refs)


def test_deterministic_regardless_of_input_order(parsed_fixture: ParsedReport) -> None:
    expected = build_questions(parsed_fixture.tickets)
    shuffled = list(parsed_fixture.tickets)
    random.Random(7).shuffle(shuffled)  # noqa: S311
    assert build_questions(shuffled) == expected


# --- edge cases ---------------------------------------------------------------------


def test_empty_input() -> None:
    assert build_questions([]) == []


def test_same_booking_ref_under_different_staff_is_two_groups() -> None:
    tickets = [
        make(ticket_no="1", staff_sign="0009ER", booking_ref="SAME"),
        make(ticket_no="2", staff_sign="0004SV", booking_ref="SAME"),
    ]
    result = build_questions(tickets)
    assert [(s.staff_sign, s.group_count) for s in result] == [("0004SV", 1), ("0009ER", 1)]


def test_missing_cost_makes_total_none_and_flags_group() -> None:
    tickets = [
        make(ticket_no="1", cost=Decimal(500)),
        make(ticket_no="2", cost=None, flags=("missing_cost",)),
        make(ticket_no="3", booking_ref="OTHER", cost=Decimal(200)),
    ]
    sv = build_questions(tickets)[0]
    bad, good = (
        next(q for q in sv.questions if q.booking_ref == "AAAAAA"),
        next(q for q in sv.questions if q.booking_ref == "OTHER"),
    )
    assert bad.total_cost is None
    assert "missing_cost" in bad.flags
    assert good.total_cost == Decimal(200)
    assert good.flags == ()
    assert sv.total_cost is None
    assert sv.ticket_count == 3


def test_missing_cost_flag_added_even_if_ticket_flag_absent() -> None:
    q = build_questions([make(cost=None)])[0].questions[0]
    assert q.flags == ("missing_cost",)


def test_flags_are_a_sorted_union() -> None:
    tickets = [
        make(ticket_no="1", flags=("unknown_trnc",)),
        make(ticket_no="2", flags=("missing_cost", "unknown_trnc"), cost=None),
    ]
    assert build_questions(tickets)[0].questions[0].flags == ("missing_cost", "unknown_trnc")


def test_passengers_sorted_by_ticket_no() -> None:
    tickets = [make(ticket_no="3"), make(ticket_no="1"), make(ticket_no="2")]
    passengers = build_questions(tickets)[0].questions[0].passengers
    assert [p.ticket_no for p in passengers] == ["1", "2", "3"]


def test_works_on_synthetic_report_with_unicode() -> None:
    from app.parsing.amadeus import parse_report

    parsed = parse_report(build_workbook([ticket_row(name="ÉLODIE/BÉBÉ 😀 (INF)")]))
    q = build_questions(parsed.tickets)[0].questions[0]
    assert q.passengers[0].name == "ÉLODIE/BÉBÉ 😀"
    assert q.passengers[0].pax_type == "INF"


# --- totals ---------------------------------------------------------------------------


def test_known_total_and_missing_count_exposed() -> None:
    tickets = [
        make(ticket_no="1", cost=Decimal(500)),
        make(ticket_no="2", cost=None),
        make(ticket_no="3", booking_ref="OTHER", cost=Decimal(200)),
    ]
    sv = build_questions(tickets)[0]
    bad = next(q for q in sv.questions if q.booking_ref == "AAAAAA")
    good = next(q for q in sv.questions if q.booking_ref == "OTHER")
    assert (bad.total_cost, bad.known_cost_total, bad.missing_cost_count) == (None, Decimal(500), 1)
    assert (good.total_cost, good.known_cost_total, good.missing_cost_count) == (
        Decimal(200),
        Decimal(200),
        0,
    )
    assert (sv.total_cost, sv.known_cost_total, sv.missing_cost_count) == (None, Decimal(700), 1)


def test_all_costs_missing_known_total_is_zero() -> None:
    q = build_questions([make(cost=None)])[0]
    assert (q.total_cost, q.known_cost_total, q.missing_cost_count) == (None, Decimal(0), 1)


def test_results_are_immutable_tuples(parsed_fixture: ParsedReport) -> None:
    staff_q = build_questions(parsed_fixture.tickets)[0]
    q = staff_q.questions[0]
    assert isinstance(staff_q.questions, tuple)
    assert isinstance(q.passengers, tuple)
    assert isinstance(q.airlines, tuple)
    assert isinstance(q.flags, tuple)


# --- blank booking reference / staff sign -----------------------------------------------


def test_blank_booking_refs_are_never_merged() -> None:
    tickets = [
        make(ticket_no="3", booking_ref="", flags=("missing_booking_ref",)),
        make(ticket_no="1", booking_ref="", flags=("missing_booking_ref",)),
        make(ticket_no="2", booking_ref="REAL"),
        make(ticket_no="4", booking_ref="REAL"),
    ]
    sv = build_questions(tickets)[0]
    assert sv.group_count == 3
    blanks = [q for q in sv.questions if q.booking_ref == ""]
    assert [q.passengers[0].ticket_no for q in blanks] == ["1", "3"]
    assert all(q.ticket_count == 1 for q in blanks)
    assert all("missing_booking_ref" in q.flags for q in blanks)
    real = next(q for q in sv.questions if q.booking_ref == "REAL")
    assert real.ticket_count == 2 and "missing_booking_ref" not in real.flags


def test_blank_booking_ref_flag_added_even_if_ticket_lacks_it() -> None:
    q = build_questions([make(booking_ref="")])[0].questions[0]
    assert "missing_booking_ref" in q.flags


def test_blank_staff_sign_grouped_under_empty_sign() -> None:
    tickets = [
        make(ticket_no="1", staff_sign="", booking_ref="", flags=("missing_staff_sign",)),
        make(ticket_no="2", staff_sign="", booking_ref="", flags=("missing_staff_sign",)),
        make(ticket_no="3", staff_sign="", booking_ref="SAME"),
        make(ticket_no="4", staff_sign="", booking_ref="SAME"),
    ]
    result = build_questions(tickets)
    assert [s.staff_sign for s in result] == [""]
    assert result[0].group_count == 3
    assert result[0].ticket_count == 4


def test_deterministic_with_blank_refs() -> None:
    tickets = [make(ticket_no=str(i), booking_ref="") for i in range(5)]
    expected = build_questions(tickets)
    assert build_questions(list(reversed(tickets))) == expected


def test_flag_constants_have_one_definition() -> None:
    from app import questions
    from app.parsing import amadeus

    assert questions.FLAG_MISSING_COST is amadeus.FLAG_MISSING_COST
    assert questions.FLAG_MISSING_BOOKING_REF is amadeus.FLAG_MISSING_BOOKING_REF
