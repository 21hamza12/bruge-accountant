"""The pure comparison engine (B8 core): cases of docs/design/05-daily-comparison.md section 3."""

from __future__ import annotations

import itertools
import random
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.comparison import (
    KIND_BAD_NUMBER,
    KIND_DIFFERENT,
    KIND_ELSEWHERE,
    KIND_NOT_IN_AMADEUS,
    KIND_NOT_SENT,
    KNOWN_KINDS,
    AmadeusTicket,
    AppTicket,
    ComparisonResult,
    Move,
    amadeus_ticket_from_line,
    app_ticket_from_row,
    compare,
    normalise_doc,
)
from app.models import AG_DIRECT, AG_PARTNER, AmadeusLine
from app.parsing.amadeus import parse_report
from app.sales_rules import TicketRow
from tests.fixtures import synthetic_20oct as fx

DAY = date(2026, 10, 20)
EARLIER = date(2026, 10, 19)
LATER = date(2026, 10, 21)
DOC = "1992600001"


def amd(**kw: Any) -> AmadeusTicket:
    base = AmadeusTicket(
        doc_number=DOC,
        airline="199",
        total_doc=Decimal("5022"),
        pax_name="DUPONT/JEAN",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
        staff_sign="0004SV",
        seq_order=1,
    )
    return replace(base, **kw)


def app_t(**kw: Any) -> AppTicket:
    base = AppTicket(
        doc_number=DOC,
        airline="199",
        total_doc=Decimal("5022"),
        pax_name="DUPONT/JEAN",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
        staff_sign="0004SV",
        sale_id=1,
        ag_kind=AG_DIRECT,
        report_day=DAY,
    )
    return replace(base, **kw)


def run(
    a: list[AppTicket],
    b: list[AmadeusTicket],
    other: dict[str, AppTicket] | None = None,
) -> ComparisonResult:
    return compare(a, b, day=DAY, other_days_app=other)


def test_identical_inputs_have_no_flags() -> None:
    r = run([app_t()], [amd()])
    assert r.flags == ()
    assert r.matched == (DOC,)
    assert r.matched_tickets == (amd(),)
    assert r.cancelled == () and r.moves == () and r.not_in_report_partner == ()
    assert r.warnings == ()


def test_empty_inputs() -> None:
    assert run([], []) == ComparisonResult((), (), (), (), (), ())


@pytest.mark.parametrize(
    ("field", "app_kw", "validate"),
    [
        ("airline", {"airline": "147"}, False),
        ("total_doc", {"total_doc": Decimal("5100")}, True),
        ("pax_name", {"pax_name": "MARTIN/PAUL"}, False),
        ("pax_type", {"pax_type": "CHD"}, True),
        ("staff_sign", {"staff_sign": "0006AC"}, True),
        ("rloc", {"rloc": "ZZZ999"}, False),
        ("trnc", {"trnc": "EMD"}, True),
    ],
)
def test_each_field_difference_is_one_flag(
    field: str, app_kw: dict[str, Any], validate: bool
) -> None:
    r = run([app_t(sale_id=5, **app_kw)], [amd(seq_order=3)])
    assert len(r.flags) == 1
    flag = r.flags[0]
    assert (flag.kind, flag.field) == (KIND_DIFFERENT, field)
    assert flag.needs_validation is validate
    assert flag.blocking is True
    assert flag.key == (DOC, KIND_DIFFERENT, field)
    assert flag.app_value != flag.amadeus_value
    assert (flag.sale_id, flag.amadeus_seq) == (5, 3)
    assert r.matched == ()


def test_difference_is_routed_to_the_app_tickets_sign() -> None:
    r = run([app_t(rloc="X", staff_sign="0006AC")], [amd(staff_sign="0004SV")])
    rloc = next(f for f in r.flags if f.field == "rloc")
    assert rloc.responsible_sign == "0006AC"
    assert rloc.amadeus_sign == "0004SV"


def test_several_differences_give_one_flag_each() -> None:
    r = run([app_t(rloc="X", pax_type="CHD")], [amd()])
    assert [f.field for f in r.flags] == ["pax_type", "rloc"]


def test_flag_values_are_display_strings() -> None:
    r = run([app_t(total_doc=Decimal("45200.00"))], [amd(total_doc=Decimal("45020"))])
    assert (r.flags[0].app_value, r.flags[0].amadeus_value) == ("45200", "45020")


@pytest.mark.parametrize(
    ("app_name", "amd_name"),
    [
        ("Dupont/Jean", "DUPONT/JEAN"),
        ("DUPONT  /  JEAN", "DUPONT/JEAN"),
        ("  DUPONT/JEAN  MR", "DUPONT/JEAN MR"),
        ("DUPONT/JEAN MR", "DUPONT/JEAN MR"),  # non-breaking space
        ("DUPONT/JEAN\tMR", "DUPONT/JEAN MR"),
    ],
)
def test_name_rules_equal(app_name: str, amd_name: str) -> None:
    assert run([app_t(pax_name=app_name)], [amd(pax_name=amd_name)]).flags == ()


@pytest.mark.parametrize(
    ("app_name", "amd_name"),
    [
        ("DUPONT/JEAN", "DUPONT/JEAM"),  # one-letter typo
        ("DUPONT/JEAN MARIE", "DUPONT/JEAN"),  # no truncation tolerance any more
        ("DUPONT/JEAN", "DUPONT/JEAN MARIE"),
        ("ELEA/ZOE", "ÉLÉA/ZOÉ"),  # no accent folding
        ("DUPONT/JEAN", ""),
    ],
)
def test_name_rules_different(app_name: str, amd_name: str) -> None:
    r = run([app_t(pax_name=app_name)], [amd(pax_name=amd_name)])
    assert [(f.field, f.needs_validation) for f in r.flags] == [("pax_name", False)]


@pytest.mark.parametrize(
    ("app_sign", "amd_sign", "equal"),
    [
        ("0004SV", "0004SV", True),
        ("0004sv", " 0004SV ", True),
        ("0004SV", "0011SV", False),
        ("SV", "0011SV", False),
        ("SV", "0004SV", False),
        ("0004SV", "0004AC", False),
    ],
)
def test_signs_are_compared_in_full(app_sign: str, amd_sign: str, equal: bool) -> None:
    r = run([app_t(staff_sign=app_sign)], [amd(staff_sign=amd_sign)])
    assert (r.flags == ()) is equal


def test_trnc_airline_and_rloc_are_case_insensitive_and_stripped() -> None:
    r = run([app_t(trnc="tktt ", airline=" 199", rloc="abc123")], [amd()])
    assert r.flags == ()


def test_total_doc_is_exact_to_the_cent() -> None:
    assert run([app_t(total_doc=Decimal("5022.00"))], [amd(total_doc=Decimal("5022"))]).flags == ()
    r = run([app_t(total_doc=Decimal("5022.00"))], [amd(total_doc=Decimal("5022.01"))])
    assert [(f.field, f.app_value, f.amadeus_value) for f in r.flags] == [
        ("total_doc", "5022", "5022.01")
    ]


def test_missing_amadeus_cost_is_a_difference() -> None:
    r = run([app_t(total_doc=Decimal(0))], [amd(total_doc=None)])
    assert [(f.field, f.amadeus_value, f.needs_validation) for f in r.flags] == [
        ("total_doc", "", True)
    ]


# --- doc numbers ------------------------------------------------------------------------------


def test_normalise_doc() -> None:
    assert normalise_doc(" 199 2600001 ", "199") == DOC
    assert normalise_doc("495-3940281465", "495") == "3940281465"
    assert normalise_doc("3940281465", "495") == "3940281465"
    assert normalise_doc("4953940281465", " 495 ") == "3940281465"
    assert normalise_doc("1993940281465", "495") == ""  # 13 digits, wrong prefix
    assert normalise_doc("12345", "199") == ""
    assert normalise_doc("", "199") == ""
    assert normalise_doc("１９９２６００００１", "199") == ""  # full-width digits


def test_doc_numbers_match_with_or_without_the_airline_prefix() -> None:
    a = app_t(doc_number="495-3940281465", airline="495")
    b = amd(doc_number="3940281465", airline="495")
    r = run([a], [b])
    assert r.flags == () and r.matched == ("3940281465",)


def test_doc_numbers_match_on_digits_only() -> None:
    r = run([app_t(doc_number="199-2600001")], [amd()])
    assert r.flags == () and r.matched == (DOC,)


@pytest.mark.parametrize("raw", ["", "abc", "12345", "１９９２６００００１", "4953940281465"])
def test_unusable_app_number_blocks(raw: str) -> None:
    r = run([app_t(doc_number=raw, staff_sign="0009ER", sale_id=4)], [])
    assert len(r.flags) == 1
    f = r.flags[0]
    assert (f.kind, f.field, f.blocking, f.needs_validation) == (
        KIND_BAD_NUMBER,
        "doc_number",
        True,
        True,
    )
    assert (f.responsible_sign, f.sale_id) == ("0009ER", 4)
    assert r.warnings == ()


@pytest.mark.parametrize("raw", ["", "abc", "12345", "１９９２６００００１"])
def test_unusable_amadeus_number_blocks(raw: str) -> None:
    r = run([], [amd(doc_number=raw, staff_sign="0006AC", seq_order=7)])
    assert len(r.flags) == 1
    f = r.flags[0]
    assert (f.kind, f.blocking, f.responsible_sign, f.amadeus_seq) == (
        KIND_BAD_NUMBER,
        True,
        "0006AC",
        7,
    )
    assert r.cancelled == ()


def test_duplicate_in_the_app_blocks_even_if_the_first_matches() -> None:
    r = run([app_t(), app_t(rloc="ZZZ999")], [amd()])
    assert [(f.kind, f.field, f.blocking) for f in r.flags] == [
        (KIND_BAD_NUMBER, "doc_number", True)
    ]
    assert r.matched == ()  # a blocked number is not "matched"
    assert r.warnings == ()


def test_duplicate_in_amadeus_blocks() -> None:
    r = run([app_t()], [amd(), amd(seq_order=2, rloc="ZZZ999")])
    assert [f.kind for f in r.flags] == [KIND_BAD_NUMBER]
    assert r.matched == ()


def test_duplicate_that_differs_from_amadeus_blocks() -> None:
    r = run([app_t(rloc="ZZZ999"), app_t(rloc="ZZZ998")], [amd()])
    assert KIND_BAD_NUMBER in {f.kind for f in r.flags}
    assert r.blocking_flags == r.flags


def test_duplicate_on_both_sides_gives_one_flag_with_a_unique_key() -> None:
    r = run([app_t(), app_t(sale_id=2)], [amd(), amd(seq_order=2)])
    assert len(r.flags) == 1


def test_results_do_not_depend_on_input_order_with_duplicates() -> None:
    a = [
        app_t(rloc="Q"),
        app_t(rloc="A", sale_id=2),
        app_t(doc_number="1992600002", sale_id=3),
        app_t(doc_number="1992600002", sale_id=3, pax_name="X/Y"),
    ]
    b = [
        amd(),
        amd(seq_order=2, rloc="ZZ"),
        amd(doc_number="1992600002", seq_order=3, rloc="Q"),
        amd(doc_number="1992600002", seq_order=3, rloc="P"),
    ]
    first = run(a, b)
    for pa in itertools.permutations(a):
        for pb in itertools.permutations(b):
            assert run(list(pa), list(pb)) == first


# --- CANX, NOT SENT, NOT IN AMADEUS -------------------------------------------------------------


def test_canx_only_in_amadeus_is_listed_not_flagged() -> None:
    canx = amd(doc_number="1992600002", trnc="CANX")
    r = run([app_t()], [amd(), canx])
    assert r.flags == ()
    assert r.cancelled == (canx,)


def test_canx_in_amadeus_but_active_in_app_is_a_trnc_difference() -> None:
    r = run([app_t()], [amd(trnc="CANX")])
    assert r.cancelled == ()
    assert [(f.kind, f.field, f.needs_validation) for f in r.flags] == [
        (KIND_DIFFERENT, "trnc", True)
    ]
    assert (r.flags[0].app_value, r.flags[0].amadeus_value) == ("TKTT", "CANX")


def test_canx_on_both_sides_is_matched() -> None:
    r = run([app_t(trnc="CANX")], [amd(trnc="CANX")])
    assert r.flags == () and r.matched == (DOC,) and r.cancelled == ()


def test_app_canx_with_no_amadeus_line_blocks() -> None:
    r = run([app_t(trnc="CANX")], [])
    assert [(f.kind, f.blocking) for f in r.flags] == [(KIND_NOT_IN_AMADEUS, True)]


@pytest.mark.parametrize("trnc", ["TKTT", "EMDA", "EMDS", "RFND", "XXXX", ""])
def test_issued_or_unknown_only_in_amadeus_is_not_sent_and_blocks(trnc: str) -> None:
    line = amd(doc_number="1992600009", trnc=trnc, staff_sign="0006AC", seq_order=4)
    r = run([], [line])
    assert len(r.flags) == 1
    f = r.flags[0]
    assert (f.kind, f.responsible_sign, f.blocking, f.field) == (
        KIND_NOT_SENT,
        "0006AC",
        True,
        "",
    )
    assert (f.sale_id, f.amadeus_seq, f.amadeus_sign) == (None, 4, "0006AC")
    assert f.key == ("1992600009", KIND_NOT_SENT, "")


def test_app_only_direct_blocks() -> None:
    r = run([app_t(staff_sign="0009ER", sale_id=8)], [])
    assert len(r.flags) == 1
    f = r.flags[0]
    assert (f.kind, f.blocking, f.responsible_sign, f.sale_id) == (
        KIND_NOT_IN_AMADEUS,
        True,
        "0009ER",
        8,
    )
    assert f.amadeus_seq is None
    assert r.not_in_report_partner == ()


def test_app_only_partner_does_not_block() -> None:
    p = app_t(ag_kind=AG_PARTNER)
    r = run([p], [])
    assert r.flags == () and r.not_in_report_partner == (p,)


def test_partner_ticket_in_the_report_but_different_still_blocks() -> None:
    r = run([app_t(ag_kind=AG_PARTNER, total_doc=Decimal(1))], [amd()])
    assert [(f.field, f.blocking) for f in r.flags] == [("total_doc", True)]
    assert r.not_in_report_partner == ()


# --- other days ---------------------------------------------------------------------------------


def test_moved_from_an_earlier_day_is_a_move_not_not_sent() -> None:
    earlier = app_t(report_day=EARLIER, sale_id=7)
    r = run([], [amd(seq_order=6)], {DOC: earlier})
    assert r.flags == ()
    assert r.moves == (Move(DOC, 7, EARLIER, DAY, amadeus_seq=6),)
    assert r.matched == (DOC,)


def test_moved_ticket_is_still_compared() -> None:
    earlier = app_t(report_day=EARLIER, total_doc=Decimal(1))
    r = run([], [amd()], {DOC: earlier})
    assert [f.field for f in r.flags] == ["total_doc"]
    assert len(r.moves) == 1


@pytest.mark.parametrize("trnc", ["CANX", "RFND", "canx ", "rfnd"])
def test_a_canx_or_rfnd_line_never_moves_a_sale_of_an_earlier_day(trnc: str) -> None:
    """Review of F4: a refund or a cancellation is about a ticket sold before; moving its sale
    would change the sale's filing day and its partner period. It is ELSEWHERE instead."""
    r = run([], [amd(trnc=trnc, seq_order=3)], {DOC: app_t(report_day=EARLIER, sale_id=7)})
    assert r.moves == () and r.cancelled == ()
    assert [(f.kind, f.sale_id, f.app_value) for f in r.flags] == [
        (KIND_ELSEWHERE, 7, EARLIER.isoformat())
    ]


@pytest.mark.parametrize("trnc", ["TKTT", "EMDA", "EMDS"])
def test_an_issue_line_still_moves_a_sale_of_an_earlier_day(trnc: str) -> None:
    r = run([], [amd(trnc=trnc)], {DOC: app_t(report_day=EARLIER, trnc=trnc)})
    assert len(r.moves) == 1


def test_other_days_keys_are_normalised() -> None:
    r = run([], [amd()], {"199-2600001": app_t(report_day=EARLIER)})
    assert len(r.moves) == 1


@pytest.mark.parametrize("sale_day", [DAY, LATER])
def test_ticket_of_the_same_or_a_later_day_is_elsewhere_not_not_sent(sale_day: date) -> None:
    other = app_t(report_day=sale_day, sale_id=9, staff_sign="0006AC")
    r = run([], [amd(seq_order=2)], {DOC: other})
    assert r.moves == ()
    assert len(r.flags) == 1
    f = r.flags[0]
    assert (f.kind, f.blocking, f.sale_id, f.amadeus_seq) == (KIND_ELSEWHERE, True, 9, 2)
    assert f.app_value == sale_day.isoformat()
    assert f.responsible_sign == "0006AC"
    assert f.key == (DOC, KIND_ELSEWHERE, "")


def test_ticket_in_both_is_not_a_move() -> None:
    r = run([app_t()], [amd()], {DOC: app_t(report_day=EARLIER)})
    assert r.moves == () and r.flags == ()


def test_other_day_entry_with_an_unusable_number_is_ignored() -> None:
    r = run([], [amd()], {"zz": app_t(report_day=EARLIER)})
    assert r.moves == () and [f.kind for f in r.flags] == [KIND_NOT_SENT]


def test_unknown_sale_kind_is_noted_and_treated_as_direct() -> None:
    r = run([app_t(ag_kind="weird")], [])
    assert [f.kind for f in r.flags] == [KIND_NOT_IN_AMADEUS]
    assert any("sale kind" in w for w in r.warnings)


def test_flags_are_ordered_by_amadeus_order_then_app_only() -> None:
    d = [f"199000000{n}" for n in range(10)]
    a = [
        app_t(doc_number=d[3], rloc="X"),
        app_t(doc_number=d[1], rloc="X"),
        app_t(doc_number=d[9]),
        app_t(doc_number=d[8]),
    ]
    b = [
        amd(doc_number=d[1], seq_order=5),
        amd(doc_number=d[3], seq_order=2),
        amd(doc_number=d[7], seq_order=9),
    ]
    r = run(a, b)
    assert [(f.doc_number, f.kind) for f in r.flags] == [
        (d[3], KIND_DIFFERENT),
        (d[1], KIND_DIFFERENT),
        (d[7], KIND_NOT_SENT),
        (d[8], KIND_NOT_IN_AMADEUS),
        (d[9], KIND_NOT_IN_AMADEUS),
    ]


def test_ties_on_seq_order_are_ordered_by_doc_number() -> None:
    d1, d2, d3 = "1990000001", "1990000002", "1990000003"
    b = [
        amd(doc_number=d3, seq_order=1, trnc="CANX"),
        amd(doc_number=d2, seq_order=1),
        amd(doc_number=d1, seq_order=1),
        amd(doc_number="1990000004", seq_order=1, trnc="CANX"),
    ]
    first = run([], b)
    assert [f.doc_number for f in first.flags] == [d1, d2]
    assert [t.doc_number for t in first.cancelled] == [d3, "1990000004"]
    assert run([], b[::-1]) == first


# --- builders ---------------------------------------------------------------------------------


def test_builder_from_ticket_row() -> None:
    row = TicketRow(
        doc_number=DOC,
        airline="199",
        total_doc=Decimal("5022"),
        pax_name="DUPONT/JEAN",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
        staff_sign="0004SV",
        sale_id=1,
        report_day_id=3,
        position=1,
        sale_pb=Decimal(6000),
        pb_line=Decimal(6000),
    )
    assert app_ticket_from_row(row, ag_kind=AG_DIRECT, report_day=DAY) == app_t()


def test_builder_from_amadeus_line() -> None:
    line = AmadeusLine(
        doc_number=DOC,
        airline="199",
        total_doc=Decimal("5022"),
        pax_name="DUPONT/JEAN",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
        staff_sign="0004SV",
        seq_order=1,
    )
    assert amadeus_ticket_from_line(line) == amd()


# --- the synthetic 20 October report ----------------------------------------------------------


def synthetic() -> tuple[list[AmadeusTicket], list[AppTicket]]:
    report = parse_report(fx.build_synthetic_20oct())
    amadeus = [
        AmadeusTicket(
            doc_number=t.ticket_no,
            airline=t.airline,
            total_doc=t.cost,
            pax_name=t.passenger_name,
            pax_type=t.pax_type,
            rloc=t.booking_ref,
            trnc=t.transaction_type,
            staff_sign=t.staff_sign,
            seq_order=i,
        )
        for i, t in enumerate(report.tickets, start=1)
    ]
    mine = [
        AppTicket(
            doc_number=t.doc_number,
            airline=t.airline,
            total_doc=t.total_doc or Decimal(0),
            pax_name=t.pax_name,
            pax_type=t.pax_type,
            rloc=t.rloc,
            trnc=t.trnc,
            staff_sign=t.staff_sign,
            sale_id=n,
            ag_kind=AG_DIRECT,
            report_day=DAY,
        )
        for n, t in enumerate((t for t in amadeus if t.trnc == "TKTT"), start=1)
    ]
    return amadeus, mine


def test_synthetic_report_identical_gives_no_flags() -> None:
    amadeus, mine = synthetic()
    assert len(mine) == fx.TKTT_COUNT
    r = run(mine, amadeus)
    assert r.flags == () and r.warnings == ()
    assert len(r.matched) == len(r.matched_tickets) == 34
    assert len(r.cancelled) == fx.CANX_COUNT
    assert sum((t.total_doc or 0) for t in r.cancelled) == fx.CANX_TOTAL
    assert sum((t.total_doc or 0) for t in r.matched_tickets) == fx.TKTT_TOTAL
    assert sum(t.total_doc for t in mine) == fx.TKTT_TOTAL


def test_synthetic_pair_and_zero_cost_lines() -> None:
    amadeus, mine = synthetic()
    r = run(mine, amadeus)
    pair = [t for t in amadeus if t.pax_name == fx.PAIR_PAX]
    assert {t.trnc for t in pair} == {"TKTT", "CANX"}
    reissued = next(t for t in pair if t.trnc == "TKTT")
    cancelled = next(t for t in pair if t.trnc == "CANX")
    assert reissued.doc_number in r.matched
    assert cancelled in r.cancelled
    zero = [t for t in amadeus if t.airline == fx.ZERO_COST_AIRLINE and t.total_doc == 0]
    assert len(zero) == 2 and all(t.doc_number in r.matched for t in zero)


def test_synthetic_three_changes_give_exactly_three_flags() -> None:
    amadeus, mine = synthetic()
    changed = replace(mine[3], total_doc=mine[3].total_doc + 100)
    dropped = mine[10]
    extra = replace(mine[0], doc_number="1999999999", sale_id=99)
    edited = [*mine[:3], changed, *mine[4:10], *mine[11:], extra]
    r = run(edited, amadeus)
    assert [(f.kind, f.field, f.doc_number) for f in r.flags] == [
        (KIND_DIFFERENT, "total_doc", changed.doc_number),
        (KIND_NOT_SENT, "", dropped.doc_number),
        (KIND_NOT_IN_AMADEUS, "", "1999999999"),
    ]
    assert len(r.cancelled) == 9


def test_stable_ordering_on_shuffled_inputs() -> None:
    amadeus, mine = synthetic()
    edited = [replace(t, rloc="ZZ") if t.sale_id % 5 == 0 else t for t in mine][:-2]
    first = run(edited, amadeus)
    rng = random.Random(7)  # noqa: S311 - seeded test data
    for _ in range(5):
        a, b = edited[:], amadeus[:]
        rng.shuffle(a)
        rng.shuffle(b)
        assert run(a, b) == first


# --- never raises, invariants --------------------------------------------------------------------


def test_random_well_typed_inputs_hold_the_invariants() -> None:
    rng = random.Random(20261022)  # noqa: S311 - seeded test data
    words = [
        "",
        "A",
        "DUPONT/JEAN",
        " du  pont / jean ",
        "ÉLÉA/ZOÉ 😀",
        "'; DROP--",
        "0004SV",
        "SV",
    ]
    docs = ["", "abc", "199-1", "1991", DOC, "1992600002", "4953940281465", "１９９２６００００１"]
    kinds = ["TKTT", "CANX", "EMDA", "RFND", "XXXX", ""]

    def money() -> Decimal:
        return Decimal(rng.choice(["0", "1", "5022", "5022.01", "-3", "1e3"]))

    def pick(options: list[str]) -> str:
        return rng.choice(options)

    for _ in range(400):
        a = [
            AppTicket(
                pick(docs), pick(["199", "495", "x"]), money(), pick(words), pick(words),
                pick(words), pick(kinds), pick(words), rng.randrange(5),
                pick([AG_DIRECT, AG_PARTNER, "x"]), DAY + timedelta(days=rng.randrange(-2, 3)),
            )
            for _ in range(rng.randrange(7))
        ]  # fmt: skip
        b = [
            AmadeusTicket(
                pick(docs), pick(["199", "495", "x"]), rng.choice([None, money()]),
                pick(words), pick(words), pick(words), pick(kinds), pick(words),
                rng.randrange(1, 4),
            )
            for _ in range(rng.randrange(7))
        ]  # fmt: skip
        other = {t.doc_number: t for t in a if rng.random() < 0.5}
        r = run(a, b, other)
        again = run(a[::-1], b[::-1], dict(reversed(list(other.items()))))
        assert again.flags == r.flags and again.matched == r.matched

        assert all(f.kind in KNOWN_KINDS for f in r.flags)
        assert all(f.blocking for f in r.flags)
        keys = [f.key for f in r.flags]
        assert len(keys) == len(set(keys))
        flagged = {f.doc_number for f in r.flags}
        matched = set(r.matched)
        cancelled = {normalise_doc(t.doc_number, t.airline) for t in r.cancelled}
        assert not matched & flagged
        assert not cancelled & flagged
        assert not matched & cancelled
        assert len(r.matched_tickets) == len(r.matched)
