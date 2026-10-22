"""Proposed sales from a PNR reading (app/parsing/pnr_sales.py): suggestions only."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.models import AG_DIRECT, AG_PARTNER
from app.parsing.pnr import parse_pnr_text
from app.parsing.pnr_sales import (
    ProposedSale,
    find_partners,
    normalise_key,
    proposed_sales,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pnr"
PARTNERS = [
    (1, "ABSHER VOYAGE"),
    (2, "REND TOURISME ET VOYAGES"),
    (3, "WORLD GATE TOURISM"),
    (4, "MUNDO DE VIAGENS"),
]


def d(value: str) -> Decimal:
    return Decimal(value)


def sales(name: str, partners: list[tuple[int, str]] | None = None) -> list[ProposedSale]:
    text = (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")
    return proposed_sales(parse_pnr_text(text), PARTNERS if partners is None else partners)


def from_text(text: str, partners: list[tuple[int, str]] | None = None) -> list[ProposedSale]:
    return proposed_sales(parse_pnr_text(text), PARTNERS if partners is None else partners)


def test_three_adults_three_sales_one_price_each() -> None:
    result = sales("three_adults_rm")
    assert [s.pax_name for s in result] == ["SYNTHA/ONE", "SYNTHB/TWO", "SYNTHC/THREE"]
    for s in result:
        assert (s.ag_kind, s.partner_id, s.pb) == (AG_DIRECT, None, d("5400"))
        assert s.staff_sign == "SV"
        assert s.clt == "SYNTHCLIENT PAYEUR"
        assert s.ap is None
        assert s.warnings == ()
        (t,) = s.tickets
        assert (t.airline, t.total_doc, t.pax_type, t.rloc, t.trnc) == (
            "495",
            d("5022.00"),
            "ADT",
            "ZX6AGG",
            "TKTT",
        )
    assert [s.tickets[0].doc_number for s in result] == [
        "3940299481",
        "3940299482",
        "3940299483",
    ]


def test_layover_is_two_sales_of_two_tickets() -> None:
    result = sales("layover_two_pages")
    assert len(result) == 2
    first, second = result
    assert [(t.doc_number, t.airline, t.total_doc) for t in first.tickets] == [
        ("3940299469", "199", d("21368.00")),
        ("3940299471", "169", d("27878.00")),
    ]
    assert [t.doc_number for t in second.tickets] == ["3940299470", "3940299472"]
    for s in result:
        assert (s.ag_kind, s.pb, s.clt, s.ap) == (AG_DIRECT, d("51300"), None, "+222 22 97 15 38")
        assert {t.pax_name for t in s.tickets} == {s.pax_name}
        assert s.warnings == ()


def test_infants_get_their_own_sale_and_price() -> None:
    result = sales("adults_with_infants")
    assert [(s.pax_name, s.tickets[0].pax_type, s.pb) for s in result] == [
        ("SYNTHD/MOTHER", "ADT", d("5400")),
        ("SYNTHD/BABYONE", "INF", d("900")),
        ("SYNTHE/MOTHER", "ADT", d("5400")),
        ("SYNTHE/BABYTWO", "INF", d("900")),
    ]
    assert [s.tickets[0].doc_number for s in result] == [
        "3940299332",
        "3940299334",
        "3940299333",
        "3940299335",
    ]
    assert all(len(s.tickets) == 1 for s in result)


def test_child_price_by_type() -> None:
    result = sales("adult_and_child")
    assert [(s.pax_name, s.tickets[0].pax_type, s.pb) for s in result] == [
        ("SYNTHF/KID", "CHD", d("4400")),
        ("SYNTHG/PARENT", "ADT", d("5400")),
    ]


def test_infant_without_price_has_no_pb_and_a_warning() -> None:
    text = (FIXTURES / "adults_with_infants.txt").read_text(encoding="utf-8")
    result = from_text(text.replace("5400 MRU ADLT 900 MRU BB", "5400 MRU"))
    infant = result[1]
    assert infant.pb is None
    assert any("Prix INF" in w for w in infant.warnings)


def test_partner_booking_matches_partner_and_has_no_pb() -> None:
    (s,) = sales("partner_no_rm")
    assert (s.ag_kind, s.partner_id, s.pb) == (AG_PARTNER, 2, None)
    assert s.clt is None  # "REND" is the agency, not a client
    assert s.staff_sign == "GS"
    assert s.warnings == ()


def test_unknown_agency_warns() -> None:
    (s,) = sales("partner_no_rm", partners=[(1, "ABSHER VOYAGE")])
    assert (s.ag_kind, s.partner_id, s.pb) == (None, None, None)
    assert "agence inconnue : REND TOURISME ET VOYAGES" in s.warnings


def test_exchange_warns_and_matches_absher() -> None:
    result = sales("exchange")
    assert len(result) == 1
    s = result[0]
    assert (s.ag_kind, s.partner_id, s.pb) == (AG_PARTNER, 1, None)
    assert "échange : vérifier le prix" in s.warnings
    assert [t.doc_number for t in s.tickets] == ["3940246514", "3940281473"]


def test_emd_tickets_are_emds_with_a_note() -> None:
    (s,) = sales("emd")
    assert [(t.doc_number, t.trnc, t.total_doc) for t in s.tickets] == [
        ("3940256960", "TKTT", d("8.00")),
        ("1949701315", "EMDS", d("12015.00")),
        ("1949701316", "EMDS", d("0.00")),
    ]
    assert (s.ag_kind, s.partner_id) == (AG_PARTNER, 4)
    assert any("EMD" in w for w in s.warnings)
    assert "échange : vérifier le prix" in s.warnings


def test_no_float_anywhere() -> None:
    def walk(obj: Any) -> None:
        assert not isinstance(obj, float)
        if is_dataclass(obj):
            for f in fields(obj):
                walk(getattr(obj, f.name))
        elif isinstance(obj, tuple | list):
            for x in obj:
                walk(x)

    for name in ("layover_two_pages", "emd", "exchange", "adults_with_infants"):
        walk(sales(name))


# --- partner matching ---------------------------------------------------------------------------


def test_normalise_key() -> None:
    assert normalise_key("  Rénd   Tourisme-Et  Voyages ") == "rend tourisme et voyages"
    assert normalise_key("") == ""


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("ABSHER VOYAGE", [1]),
        ("absher  voyage", [1]),
        ("WORLD GATE TOURISM", [3]),
        ("WORLDGATE", [3]),  # no spaces: prefix of WORLDGATETOURISM
        ("World Gate", [3]),
        ("REND", [2]),
        ("ABSHER", [1]),
        ("MÚNDO DE VIAGENS", [4]),
        ("AB", []),  # too short for a prefix match
        ("GATE", []),  # a prefix only, not a suffix or a middle word
        ("", []),
        ("ZZZ", []),
    ],
)
def test_find_partners(name: str, expected: list[int]) -> None:
    assert list(find_partners(name, PARTNERS).ids) == expected


def test_find_partners_ambiguous_prefix_returns_all() -> None:
    partners = [(1, "ALPHA TRAVEL"), (2, "ALPHA TOURS")]
    assert list(find_partners("ALPHA", partners).ids) == [1, 2]
    assert list(find_partners("ALPHA TOURS", partners).ids) == [
        2
    ]  # an exact match wins over prefixes


def test_ambiguous_agency_is_not_guessed() -> None:
    text = (FIXTURES / "partner_no_rm.txt").read_text(encoding="utf-8")
    result = from_text(
        text.replace("REND TOURISME ET VOYAGES", "REND"),
        partners=[(1, "REND TOURISME"), (2, "REND VOYAGES")],
    )
    (s,) = result
    assert s.partner_id is None and any("ambiguë" in w for w in s.warnings)


def test_bruge_in_any_case_is_direct() -> None:
    text = (FIXTURES / "three_adults_rm.txt").read_text(encoding="utf-8")
    result = from_text(text.replace("- BRUGE -", "- Bruge -"))
    assert {s.ag_kind for s in result} == {AG_DIRECT}


def test_agency_falls_back_to_fp_hint() -> None:
    text = (FIXTURES / "partner_no_rm.txt").read_text(encoding="utf-8")
    text = text.replace("REND TOURISME ET VOYAGES", "INCONNU").replace(
        "NONREF REND", "NONREF WORLDGATE"
    )
    (s,) = from_text(text)
    assert (s.ag_kind, s.partner_id) == (AG_PARTNER, 3)
    assert any("INCONNU" in w for w in s.warnings)


def test_no_agency_and_no_fp_warns() -> None:
    text = (FIXTURES / "emd.txt").read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if " AP " not in line)
    (s,) = from_text(text)
    assert s.ag_kind is None and any("agence" in w for w in s.warnings)


def test_client_hint_rules() -> None:
    text = (FIXTURES / "three_adults_rm.txt").read_text(encoding="utf-8")
    for fp, expected in [
        ("CLIENT", None),
        ("client", None),
        ("BRUGE", None),
        ("WORLDGATE", "WORLDGATE"),  # loose prefix match only: stays a hint
        ("World Gate Tourism", None),  # the partner itself
        ("MARIE DUPONT", "MARIE DUPONT"),
    ]:
        result = from_text(text.replace("SYNTHCLIENT PAYEUR", fp))
        assert result[0].clt == expected, fp


def test_ticket_without_known_passenger_goes_to_a_nameless_sale() -> None:
    text = (
        "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n  1.A/B   2.C/D\n"
        "  3 FA PAX 495-3940299481/ETL6/MRU5022.00/21OCT26/NKC262213/51211381/S4\n"
        "  4 AP NKC +222 37353400 - BRUGE - A\n"
    )
    (s,) = from_text(text)
    assert s.pax_name == "" and len(s.tickets) == 1
    assert any("passager" in w for w in s.warnings)


def test_passenger_without_ticket_makes_no_sale() -> None:
    text = (
        "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n  1.A/B   2.C/D\n"
        "  3 FA PAX 495-3940299481/ETL6/MRU5022.00/21OCT26/NKC262213/51211381/S4/P2\n"
    )
    (s,) = from_text(text)
    assert s.pax_name == "C/D"


def test_ticket_with_unreadable_cost_has_none_and_warning() -> None:
    text = (FIXTURES / "partner_no_rm.txt").read_text(encoding="utf-8")
    (s,) = from_text(text.replace("MRU5023.00", "EUR50.00"))
    assert s.tickets[0].total_doc is None
    assert any("coût" in w for w in s.warnings)


def test_empty_reading_gives_no_sale() -> None:
    assert from_text("") == []
    assert from_text("garbage") == []
