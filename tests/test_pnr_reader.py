"""The PNR text reader (app/parsing/pnr.py): fixtures are synthetic, shapes copied from screens."""

from __future__ import annotations

import random
import string
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.parsing.pnr import (
    MAX_LINES,
    Passenger,
    PnrReading,
    RmPrices,
    TicketLine,
    parse_pnr_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pnr"


def load(name: str) -> PnrReading:
    return parse_pnr_text((FIXTURES / f"{name}.txt").read_text(encoding="utf-8"))


def d(value: str) -> Decimal:
    return Decimal(value)


def ticket(doc: str, prefix: str, carrier: str, cost: str, **kw: Any) -> TicketLine:
    base: dict[str, Any] = {
        "doc_number": doc,
        "airline": prefix,
        "carrier": carrier,
        "emd": False,
        "cost": d(cost),
        "issue_date": date(2026, 10, 21),
        "issuing_office": "NKC262213",
        "iata": "51211381",
        "segments": "S4",
        "passenger": 1,
        "is_infant": False,
    }
    base.update(kw)
    return TicketLine(**base)


def test_three_adults_with_one_rm_price() -> None:
    r = load("three_adults_rm")
    assert (r.rloc, r.office, r.agent_sign, r.created) == (
        "ZX6AGG",
        "NKC262213",
        "SV",
        date(2026, 10, 21),
    )
    assert r.passengers == (
        Passenger(1, "SYNTHA/ONE", "ADT", None),
        Passenger(2, "SYNTHB/TWO", "ADT", None),
        Passenger(3, "SYNTHC/THREE", "ADT", None),
    )
    assert r.tickets == tuple(
        ticket(f"394029948{n}", "495", "L6", "5022.00", passenger=n) for n in (1, 2, 3)
    )
    assert r.rm_raw == "5400 MRU"
    assert r.rm_prices == RmPrices(adt=None, chd=None, inf=None, default=d("5400"))
    assert r.ap_agency == "BRUGE"
    assert r.ap_phones == ()
    assert r.fp_hint == "SYNTHCLIENT PAYEUR"
    assert (r.exchanges, r.nonrefundable_amounts, r.endorsements, r.warnings) == ((), (), (), ())


def test_adults_with_infants_rm_has_infant_price() -> None:
    r = load("adults_with_infants")
    assert r.rloc == "ZOMZOM"
    assert r.passengers == (
        Passenger(1, "SYNTHD/MOTHER", "ADT", None),
        Passenger(1, "SYNTHD/BABYONE", "INF", 1),
        Passenger(2, "SYNTHE/MOTHER", "ADT", None),
        Passenger(2, "SYNTHE/BABYTWO", "INF", 2),
    )
    kw = {"issue_date": date(2026, 10, 16), "segments": "S3"}
    assert r.tickets == (
        ticket("3940299332", "495", "L6", "5021.00", passenger=1, **kw),
        ticket("3940299333", "495", "L6", "5021.00", passenger=2, **kw),
        ticket("3940299334", "495", "L6", "497.00", passenger=1, is_infant=True, **kw),
        ticket("3940299335", "495", "L6", "497.00", passenger=2, is_infant=True, **kw),
    )
    assert r.rm_prices == RmPrices(adt=d("5400"), chd=None, inf=d("900"), default=None)
    assert r.rm_raw == "5400 MRU ADLT 900 MRU BB"
    assert r.warnings == ()


def test_adult_and_child_rm_by_type() -> None:
    r = load("adult_and_child")
    assert r.passengers == (
        Passenger(1, "SYNTHF/KID", "CHD", None),
        Passenger(2, "SYNTHG/PARENT", "ADT", None),
    )
    assert [(t.doc_number, t.cost, t.passenger) for t in r.tickets] == [
        ("3940299349", d("4031.00"), 1),
        ("3940299350", d("5021.00"), 2),
    ]
    assert r.rm_prices == RmPrices(adt=d("5400"), chd=d("4400"), inf=None, default=None)
    assert r.warnings == ()


def test_layover_two_pages_deduplicated() -> None:
    r = load("layover_two_pages")
    assert (r.rloc, r.created) == ("YH927B", date(2026, 10, 20))
    assert [p.name for p in r.passengers] == ["SYNTHH/SISTERONE", "SYNTHH/SISTERTWO"]
    assert [(t.doc_number, t.airline, t.carrier, t.cost, t.passenger) for t in r.tickets] == [
        ("3940299469", "199", "TU", d("21368.00"), 1),
        ("3940299470", "199", "TU", d("21368.00"), 2),
        ("3940299471", "169", "HR", d("27878.00"), 1),
        ("3940299472", "169", "HR", d("27878.00"), 2),
    ]
    assert [t.segments for t in r.tickets] == ["S3,7", "S3,7", "S4-6", "S4-6"]
    assert r.rm_prices == RmPrices(None, None, None, d("51300"))
    assert r.ap_phones == ("+222 22 97 15 38",)
    assert r.ap_agency == "BRUGE"
    assert r.fp_hint == "CLIENT"
    assert r.warnings == ()


def test_partner_without_rm() -> None:
    r = load("partner_no_rm")
    assert (r.office, r.agent_sign, r.rloc) == ("NKC262285", "GS", "XF7QUW")
    assert r.rm_raw == ""
    assert r.rm_prices is None
    assert r.ap_agency == "REND TOURISME ET VOYAGES"
    assert r.fp_hint == "REND"
    (t,) = r.tickets
    assert (t.doc_number, t.issuing_office, t.iata, t.passenger) == (
        "3940281472",
        "NKC262353",
        "51211845",
        1,
    )
    assert r.warnings == ()


def test_exchange_and_nonrefundable_amount() -> None:
    r = load("exchange")
    assert r.exchanges == ("3940246514",)
    assert r.nonrefundable_amounts == (d("8245.00"),)
    assert r.endorsements == ()
    assert r.ap_agency == "ABSHER VOYAGE"
    assert r.fp_hint == "ABSHER"
    assert [t.doc_number for t in r.tickets] == ["3940246514", "3940281473"]
    assert r.rm_prices == RmPrices(None, None, None, d("32000"))
    assert r.agent_sign == "TK"
    assert r.warnings == ()


def test_emd_foreign_office() -> None:
    r = load("emd")
    assert r.office == "LAD282527"
    assert [(t.doc_number, t.airline, t.emd, t.cost) for t in r.tickets] == [
        ("3940256960", "057", False, d("8.00")),
        ("1949701315", "057", True, d("12015.00")),
        ("1949701316", "057", True, d("0.00")),
    ]
    assert [t.passenger for t in r.tickets] == [1, 1, 1]
    assert r.exchanges == ("3940233069",)
    assert r.nonrefundable_amounts == (d("8787.00"),)
    assert r.ap_agency == "MUNDO DE VIAGENS"
    assert r.passengers == (Passenger(1, "SYNTHK/LADY MRS", "ADT", None),)
    assert r.fp_hint is None


def test_no_float_anywhere() -> None:
    def walk(obj: Any) -> None:
        assert not isinstance(obj, float)
        if is_dataclass(obj):
            for f in fields(obj):
                walk(getattr(obj, f.name))
        elif isinstance(obj, tuple):
            for x in obj:
                walk(x)

    for name in ("three_adults_rm", "adults_with_infants", "exchange", "emd"):
        walk(load(name))


# --- line shapes ------------------------------------------------------------------------------


MIN = (
    "RP/NKC262213/NKC262213            SV/SU  21OCT26/0000Z   ZX6AGG\n"
    "  1.SYNTHA/ONE\n"
    "  2 FA PAX 495-3940299481/ETL6/MRU5022.00/21OCT26/NKC262213/51211381/S4/P1\n"
)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_newline_styles_and_trailing_spaces(newline: str) -> None:
    text = newline.join(line + "   " for line in MIN.splitlines())
    r = parse_pnr_text(text)
    assert r.rloc == "ZX6AGG"
    assert r.tickets[0].cost == d("5022.00")


def test_fa_split_mid_number_is_joined() -> None:
    text = MIN.replace("MRU5022.00", "MRU50\n       22.00").replace(
        "3940299481", "39402\n     99481"
    )
    (t,) = parse_pnr_text(text).tickets
    assert (t.doc_number, t.cost) == ("3940299481", d("5022.00"))


def test_star_line_continuation_is_not_glued_to_previous_element() -> None:
    text = MIN + "  * ES/G 21OCT/ABSU/NKC262285\n    NKC262213-B/NKC262353-B\n"
    r = parse_pnr_text(text)
    assert r.tickets[0].segments == "S4" and r.fp_hint is None and r.warnings == ()


def test_prompts_are_ignored() -> None:
    r = parse_pnr_text(">\n)>\n>|\n" + MIN + ")>RT ZX6AGG\n>")
    assert r.rloc == "ZX6AGG" and len(r.tickets) == 1 and r.warnings == ()


def test_names_with_apostrophe_and_hyphen() -> None:
    text = (
        "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n"
        "  1.O'BRIEN/MARY-ANN   2.DE-LA/J(CHD)\n"
    )
    r = parse_pnr_text(text)
    assert [(p.name, p.pax_type) for p in r.passengers] == [
        ("O'BRIEN/MARY-ANN", "ADT"),
        ("DE-LA/J", "CHD"),
    ]


def test_control_and_bidi_characters_are_stripped() -> None:
    r = parse_pnr_text(MIN.replace("SYNTHA", "SYN‮THA\x00"))
    assert r.passengers[0].name == "SYNTHA/ONE"


def test_fp_variants() -> None:
    base = MIN + " 9 FP NONREF{}\n"
    assert parse_pnr_text(base.format("WORLDGATE")).fp_hint == "WORLDGATE"
    assert parse_pnr_text(base.format(" WORLDGATE")).fp_hint == "WORLDGATE"
    assert parse_pnr_text(base.format("")).fp_hint is None
    assert parse_pnr_text(MIN + " 9 FP CASH\n").fp_hint is None


def test_ap_variants() -> None:
    text = (
        MIN
        + " 5 AP NKC +222 36617800 - WORLD GATE TOURISM - A\n"
        + " 6 AP +222  22 97   15 38\n"
        + " 7 AP NKC 22971538\n"
        + " 8 AP SYNTHMAIL@EXAMPLE.COM\n"
    )
    r = parse_pnr_text(text)
    assert r.ap_agency == "WORLD GATE TOURISM"
    assert r.ap_phones == ("+222 22 97 15 38", "22971538")


# --- RM -----------------------------------------------------------------------------------------


def rm(line: str) -> PnrReading:
    return parse_pnr_text(MIN + f" 8 RM {line}\n")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("5400 MRU", RmPrices(None, None, None, d("5400"))),
        ("5400", RmPrices(None, None, None, d("5400"))),
        ("51 300 MRU", RmPrices(None, None, None, d("51300"))),
        ("5400.50 MRU", RmPrices(None, None, None, d("5400.50"))),
        ("5400 MRU ADLT 900 MRU BB", RmPrices(d("5400"), None, d("900"), None)),
        ("5400 ADT 900 INF", RmPrices(d("5400"), None, d("900"), None)),
        ("5400 MRU 900 MRU BEBE", RmPrices(None, None, d("900"), d("5400"))),
        ("ADT = 5400 MRU , CHD = 4400 MRU", RmPrices(d("5400"), d("4400"), None, None)),
        ("ADT:5400 CHD:4400 BB:900", RmPrices(d("5400"), d("4400"), d("900"), None)),
        ("ADULTE 51 300 ENF 40 000", RmPrices(d("51300"), d("40000"), None, None)),
        ("adt 5400 mru", RmPrices(d("5400"), None, None, None)),
    ],
)
def test_rm_variants(line: str, expected: RmPrices) -> None:
    r = rm(line)
    assert r.rm_prices == expected
    assert r.rm_raw == line
    assert r.warnings == ()


@pytest.mark.parametrize(
    "line",
    [
        "5400 4400 900",
        "ADT 5400 ADT 5000",
        "PAYE CASH 5400",
        "ADT = CHD = 4400",
        "5400 ADT CHD 4400",
        "MRU",
        "99999999999999 MRU",
    ],
)
def test_rm_ambiguous_is_none_with_warning(line: str) -> None:
    r = rm(line)
    assert r.rm_prices is None
    assert r.rm_raw == line
    assert any("RM" in w for w in r.warnings)


def test_rm_multiple_lines_are_joined() -> None:
    r = parse_pnr_text(MIN + " 8 RM 5400 MRU ADLT\n 9 RM 900 MRU BB\n")
    assert r.rm_raw == "5400 MRU ADLT\n900 MRU BB"
    assert r.rm_prices == RmPrices(d("5400"), None, d("900"), None)


def test_rm_for_type_rules() -> None:
    single = RmPrices(None, None, None, d("5400"))
    assert single.for_type("ADT") == d("5400")
    assert single.for_type("CHD") == d("5400")
    assert single.for_type("INF") is None
    typed = RmPrices(d("5400"), d("4400"), d("900"), None)
    assert [typed.for_type(t) for t in ("ADT", "CHD", "INF")] == [d("5400"), d("4400"), d("900")]
    adult_only = RmPrices(d("5400"), None, None, None)
    assert adult_only.for_type("CHD") is None  # never the adult price by default
    adult_inf = RmPrices(d("5400"), None, d("900"), None)
    assert adult_inf.for_type("CHD") is None
    assert adult_inf.for_type("XXX") is None


def test_infant_without_rm_price_warns() -> None:
    text = (
        "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n  1.A/B(INFA/C)\n  2 RM 5400 MRU\n"
    )
    assert any("bébé" in w for w in parse_pnr_text(text).warnings)


# --- hostile input ------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "   \n\n", "hello world", "\x00\x01\x02", ">\n)>\n"])
def test_empty_or_garbage_gives_empty_reading_and_warning(text: str) -> None:
    r = parse_pnr_text(text)
    assert r.rloc is None and r.passengers == () and r.tickets == ()
    assert r.rm_prices is None and r.warnings


def test_non_string_input_does_not_raise() -> None:
    r = parse_pnr_text(None)  # type: ignore[arg-type]
    assert r.tickets == () and r.warnings


def test_oversize_text_is_refused() -> None:
    text = MIN + "\n" + "X" * 20_000
    assert len(text.encode()) > 20_000
    r = parse_pnr_text(text)
    assert r.tickets == () and r.rloc is None
    assert any("trop long" in w for w in r.warnings)


def test_text_just_under_the_limit_is_read() -> None:
    text = MIN + "\n" * (MAX_LINES - 10)
    assert parse_pnr_text(text).tickets


def test_too_many_lines_is_refused() -> None:
    r = parse_pnr_text(MIN + "\n" * MAX_LINES)
    assert r.tickets == () and any("trop long" in w for w in r.warnings)


def test_two_different_pnrs_keeps_first() -> None:
    other = (
        MIN.replace("ZX6AGG", "OTHER1")
        .replace("SYNTHA", "SYNTHZ")
        .replace("3940299481", "3940299999")
    )
    r = parse_pnr_text(MIN + other)
    assert r.rloc == "ZX6AGG"
    assert [t.doc_number for t in r.tickets] == ["3940299481"]
    assert any("plusieurs réservations" in w for w in r.warnings)


def test_same_pnr_pasted_twice_is_not_doubled() -> None:
    r = parse_pnr_text(MIN + MIN)
    assert len(r.tickets) == 1 and len(r.passengers) == 1 and r.warnings == ()


def test_non_mru_currency_warns() -> None:
    r = parse_pnr_text(MIN.replace("MRU5022.00", "EUR120.00"))
    assert r.tickets[0].cost is None
    assert any("devise" in w for w in r.warnings)


def test_bad_amount_and_unreadable_fa_warn() -> None:
    r = parse_pnr_text(MIN.replace("MRU5022.00", "MRU5O22.00"))
    assert r.tickets[0].cost is None and any("montant" in w for w in r.warnings)
    r = parse_pnr_text(MIN.replace("495-3940299481", "495-39402994"))
    assert r.tickets == () and any("FA" in w for w in r.warnings)


def test_bad_date_gives_none_created() -> None:
    r = parse_pnr_text(MIN.replace("21OCT26/0000Z", "31FEB26/0000Z"))
    assert r.created is None and r.rloc == "ZX6AGG"


def test_ticket_without_passenger_number_with_two_passengers_warns() -> None:
    text = (
        "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n  1.A/B   2.C/D\n"
        "  3 FA PAX 495-3940299481/ETL6/MRU5022.00/21OCT26/NKC262213/51211381/S4\n"
    )
    r = parse_pnr_text(text)
    assert r.tickets[0].passenger is None and r.warnings


def test_without_rp_line_elements_are_still_read() -> None:
    text = MIN.split("\n", 1)[1]
    r = parse_pnr_text(text)
    assert r.rloc is None and len(r.tickets) == 1
    assert any("RP" in w for w in r.warnings)


def test_never_raises_on_random_text() -> None:
    rng = random.Random(20261022)  # noqa: S311
    alphabet = string.printable + "éà‮​€"
    pieces = [
        "FA PAX ",
        "RM ",
        "AP ",
        "RP/",
        "MRU",
        " 12 ",
        "\n",
        "  ",
        "ETL6",
        "/P1",
        "(INF",
        "1.",
    ]
    for _ in range(300):
        parts = []
        for _ in range(rng.randint(0, 40)):
            if rng.random() < 0.5:
                parts.append(rng.choice(pieces))
            else:
                parts.append("".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))))
        assert isinstance(parse_pnr_text("".join(parts)), PnrReading)
    for _ in range(100):  # mutations of a real shape
        chars = list(MIN)
        for _ in range(rng.randint(1, 15)):
            chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        assert isinstance(parse_pnr_text("".join(chars)), PnrReading)
