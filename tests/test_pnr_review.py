"""Review fixes for the PNR reader and the sales mapping (security and code reviews of B6)."""

from __future__ import annotations

import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import AG_DIRECT, AG_PARTNER
from app.parsing.pnr import RmPrices, parse_pnr_text, parse_rm
from app.parsing.pnr_sales import ProposedSale, find_partners, proposed_sales

FIXTURES = Path(__file__).parent / "fixtures" / "pnr"
PARTNERS = [
    (1, "ABSHER VOYAGE"),
    (2, "REND TOURISME ET VOYAGES"),
    (3, "WORLD GATE TOURISM"),
]
HEAD = "RP/NKC262213/NKC262213            SV/SU  21OCT26/0000Z   ZX6AGG\n"
FA1 = " 20 FA PAX 495-3940299481/ETL6/MRU5022.00/21OCT26/NKC262213/51211381/S4/P1\n"
BRUGE = " 10 AP NKC +222 37353400 - BRUGE - A\n"


def d(value: str) -> Decimal:
    return Decimal(value)


def sales_of(text: str, partners: list[tuple[int, str]] | None = None) -> list[ProposedSale]:
    return proposed_sales(parse_pnr_text(text), PARTNERS if partners is None else partners)


# --- 1, 2: currency and child price ---------------------------------------------------------


def test_mro_is_not_mru() -> None:
    assert parse_rm("5400 MRO") is None
    assert parse_rm("5400 UM") == RmPrices(None, None, None, d("5400"))
    r = parse_pnr_text(HEAD + "  1.A/B\n  2 RM 5400 MRO\n")
    assert r.rm_prices is None and any("RM" in w for w in r.warnings)


def test_child_never_gets_the_labelled_adult_price() -> None:
    assert RmPrices(d("5400"), None, None, None).for_type("CHD") is None
    assert RmPrices(d("5400"), None, d("900"), None).for_type("CHD") is None
    assert RmPrices(None, None, None, d("5400")).for_type("CHD") == d("5400")


def test_child_sale_without_child_price_warns() -> None:
    text = HEAD + "  1.A/B(CHD)\n  2 RM ADT 5400\n" + BRUGE + FA1
    (s,) = sales_of(text)
    assert s.pb is None and "Prix CHD absent de la ligne RM : à saisir." in s.warnings


def test_child_price_without_child_passenger_is_harmless() -> None:
    text = HEAD + "  1.A/B\n  2 RM ADT = 5400 , CHD = 4400\n" + BRUGE + FA1
    (s,) = sales_of(text)
    assert s.pb == d("5400") and s.warnings == ()


# --- 3: ReDoS --------------------------------------------------------------------------------


def test_dash_flood_in_ap_is_fast() -> None:
    flood = "\n".join("     " + "- " * 38 for _ in range(190))
    text = HEAD + "  1.A/B\n  2 AP NKC +222 1\n" + flood + "\n"
    assert len(text) < 16 * 1024
    start = time.perf_counter()
    r = parse_pnr_text(text)
    assert time.perf_counter() - start < 0.2
    assert r.ap_agency is None


@pytest.mark.parametrize("code", ["FP NONREF", "FA PAX", "FE PAX", "AP NKC"])
def test_long_element_bodies_are_capped(code: str) -> None:
    flood = "\n".join("     " + "A/B " * 15 for _ in range(100))
    start = time.perf_counter()
    r = parse_pnr_text(HEAD + f"  2 {code} X\n" + flood + "\n")
    assert time.perf_counter() - start < 0.2
    assert r.fp_hint is None or len(r.fp_hint) <= 100


# --- 4: thousands ----------------------------------------------------------------------------


def test_thousands_grouping() -> None:
    assert parse_rm("51 300 MRU") == RmPrices(None, None, None, d("51300"))
    assert parse_rm("5 400") == RmPrices(None, None, None, d("5400"))
    assert parse_rm("ADT 5 400 CHD 4 400") == RmPrices(d("5400"), d("4400"), None, None)


@pytest.mark.parametrize("raw", ["900 450 BB", "900 450", "5400 900", "951 300 BB"])
def test_two_prices_are_never_glued(raw: str) -> None:
    assert parse_rm(raw) is None


# --- 5: amounts ------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "0 MRU", "ADT 0", "1000000001"])
def test_rm_amount_bounds(raw: str) -> None:
    assert parse_rm(raw) is None


def test_low_rm_price_warns_but_infant_price_does_not() -> None:
    r = parse_pnr_text(HEAD + "  1.A/B\n  2 RM 540\n")
    assert r.rm_prices == RmPrices(None, None, None, d("540"))
    assert "prix RM inhabituellement bas : vérifier" in r.warnings
    ok = parse_pnr_text(HEAD + "  1.A/B(INFC/D)\n  2 RM 5400 ADT 900 BB\n")
    assert "prix RM inhabituellement bas : vérifier" not in ok.warnings


def test_huge_ticket_cost_is_unreadable_but_zero_is_allowed() -> None:
    r = parse_pnr_text(HEAD + "  1.A/B\n" + FA1.replace("MRU5022.00", "MRU100000001.00"))
    assert r.tickets[0].cost is None and any("montant" in w for w in r.warnings)
    z = parse_pnr_text(HEAD + "  1.A/B\n" + FA1.replace("MRU5022.00", "MRU0.00"))
    assert z.tickets[0].cost == d("0.00")
    p = parse_pnr_text(HEAD + " 3 FE PAX MRU999999999999.00 NONREF\n")
    assert p.nonrefundable_amounts == ()


# --- 6: partner matching ---------------------------------------------------------------------


def test_find_partners_reports_the_rule() -> None:
    assert find_partners("absher voyage", PARTNERS).rule == "exact"
    assert find_partners("WORLDGATETOURISM", PARTNERS).rule == "packed"
    assert find_partners("WORLDGATE", PARTNERS).rule == "prefix"
    assert find_partners("ZZZZ", PARTNERS).rule is None


def test_prefix_must_end_on_a_word_boundary() -> None:
    assert find_partners("ABSHERVOY", PARTNERS).ids == ()
    assert find_partners("WORLDGA", PARTNERS).ids == ()
    assert find_partners("ABSHER VOYAGE TOURS", PARTNERS).ids == (1,)


def test_empty_or_bad_partner_names_are_ignored() -> None:
    odd: list[tuple[int, str]] = [(1, ""), (2, "   "), (3, None), (4, "REND")]  # type: ignore[list-item]
    assert find_partners("REND", odd).ids == (4,)
    assert find_partners("", odd).ids == ()


def test_prefix_match_is_flagged_for_review() -> None:
    text = HEAD + "  1.A/B\n  2 AP NKC +222 1 - WORLDGATE - A\n" + FA1
    (s,) = sales_of(text)
    assert (s.ag_kind, s.partner_id) == (AG_PARTNER, 3)
    assert "partenaire déduit de « WORLD GATE TOURISM » : vérifier" in s.warnings


def test_exact_match_has_no_review_warning() -> None:
    text = HEAD + "  1.A/B\n  2 AP NKC +222 1 - ABSHER VOYAGE - A\n" + FA1
    (s,) = sales_of(text)
    assert s.partner_id == 1 and s.warnings == ()


def test_fp_fallback_is_flagged_and_does_not_echo_the_hint() -> None:
    text = HEAD + "  1.A/B\n  2 AP NKC +222 1 - INCONNU - A\n  3 FP NONREF ABSHER VOYAGE\n" + FA1
    (s,) = sales_of(text)
    assert s.partner_id == 1
    assert "agence inconnue : INCONNU" in s.warnings
    assert "partenaire déduit de « ABSHER VOYAGE » : vérifier" in s.warnings


def test_direct_from_fp_is_flagged() -> None:
    text = HEAD + "  1.A/B\n  2 FP NONREF BRUGE\n" + FA1
    (s,) = sales_of(text)
    assert s.ag_kind == AG_DIRECT
    assert "vente directe déduite de l'indication de paiement : vérifier" in s.warnings


def test_ambiguous_ap_does_not_fall_back_to_fp() -> None:
    text = HEAD + "  1.A/B\n  2 AP NKC +222 1 - REND - A\n  3 FP NONREF ABSHER\n" + FA1
    (s,) = sales_of(text, [(1, "ABSHER VOYAGE"), (2, "REND TOURISME"), (3, "REND VOYAGES")])
    assert (s.ag_kind, s.partner_id) == (None, None)
    assert "agence ambiguë : REND" in s.warnings


def test_warnings_never_echo_the_client_name() -> None:
    text = HEAD + "  1.A/B\n  2 FP NONREF MARIE DUPONTELLE\n" + FA1
    (s,) = sales_of(text)
    assert s.ag_kind is None
    assert all("DUPONTELLE" not in w for w in s.warnings)


def test_ap_agency_in_warning_is_truncated() -> None:
    text = HEAD + "  1.A/B\n  2 AP NKC +222 1 - " + "X" * 90 + " - A\n" + FA1
    (s,) = sales_of(text)
    (w,) = [w for w in s.warnings if w.startswith("agence inconnue")]
    assert len(w) <= len("agence inconnue : ") + 40


# --- 7, 8: infants ---------------------------------------------------------------------------


TWO_INF = (
    HEAD
    + "  1.MOM/ONE(INFBABY/A)(INFBABY/B)\n"
    + BRUGE
    + " 11 RM 5400 ADT 900 BB\n"
    + " 20 FA INF 495-3940299491/ETL6/MRU497.00/21OCT26/NKC262213/51211381/S4/P1\n"
    + " 21 FA INF 495-3940299492/ETL6/MRU497.00/21OCT26/NKC262213/51211381/S4/P1\n"
)


def test_two_infants_on_one_adult() -> None:
    r = parse_pnr_text(TWO_INF)
    assert [(p.name, p.pax_type, p.adult_number) for p in r.passengers] == [
        ("MOM/ONE", "ADT", None),
        ("BABY/A", "INF", 1),
        ("BABY/B", "INF", 1),
    ]
    result = sales_of(TWO_INF)
    assert [(s.pax_name, s.tickets[0].doc_number) for s in result] == [
        ("BABY/A", "3940299491"),
        ("BABY/B", "3940299492"),
    ]


def test_infant_tickets_not_paired_one_to_one_warn() -> None:
    text = TWO_INF.replace(
        " 21 FA INF 495-3940299492/ETL6/MRU497.00/21OCT26/NKC262213/51211381/S4/P1\n", ""
    )
    result = sales_of(text)
    assert [s.pax_name for s in result] == ["BABY/A"]
    assert any("bébé" in w for w in result[0].warnings)


def test_birth_date_is_stripped_from_infant_name() -> None:
    r = parse_pnr_text(HEAD + "  1.MOM/ONE(INFA/C/01JAN26)\n")
    assert r.passengers[1].name == "A/C"


# --- 9: orphans ------------------------------------------------------------------------------


def test_orphan_sale_has_no_pb() -> None:
    text = HEAD + "  1.A/B   2.C/D\n  3 RM 5400\n" + BRUGE + FA1.replace("/P1", "")
    (s,) = sales_of(text)
    assert s.pax_name == "" and s.pb is None


# --- 10: dedup across screens ----------------------------------------------------------------


def test_same_element_number_with_other_content_warns_and_keeps_first() -> None:
    page1 = HEAD + "  1.A/B\n 20 FP NONREF ABSHER\n"
    page2 = HEAD + " 20 FP NONREF OTHER\n"
    r = parse_pnr_text(page1 + page2)
    assert r.fp_hint == "ABSHER"
    assert "élément 20 différent entre deux écrans : vérifier" in r.warnings


# --- 11: ranges ------------------------------------------------------------------------------


def test_passenger_range_is_not_attached_to_a_single_passenger() -> None:
    text = HEAD + "  1.A/B\n" + FA1.replace("/P1", "/P1-2")
    r = parse_pnr_text(text)
    assert r.tickets[0].passenger is None
    assert any("plusieurs passagers" in w for w in r.warnings)


# --- 13: suspicious leading characters -------------------------------------------------------


def test_leading_formula_characters_are_removed() -> None:
    text = HEAD + "  1.=CMD/X\n  2 AP NKC +222 1 - @EVIL - A\n  3 FP NONREF +SUM\n"
    r = parse_pnr_text(text)
    assert r.passengers[0].name == "CMD/X"
    assert r.ap_agency == "EVIL" and r.fp_hint == "SUM"
    assert "caractère suspect retiré" in r.warnings


# --- 14: sale-level ----------------------------------------------------------------------------


def test_passenger_without_ticket_is_reported() -> None:
    text = HEAD + "  1.A/B(INFC/D)\n" + BRUGE + FA1
    (s,) = sales_of(text)
    assert any("Aucun billet" in w for w in s.warnings)


def test_two_screens_same_passenger_one_sale_with_both_tickets() -> None:
    page1 = HEAD + "  1.A/B\n" + BRUGE + FA1
    page2 = HEAD + FA1.replace(" 20 ", " 21 ").replace("3940299481", "3940299482")
    (s,) = sales_of(page1 + page2)
    assert [t.doc_number for t in s.tickets] == ["3940299481", "3940299482"]


def test_direct_sale_with_unreadable_rm_has_no_pb_and_a_warning() -> None:
    text = HEAD + "  1.A/B\n  2 RM PAYE CASH\n" + BRUGE + FA1
    (s,) = sales_of(text)
    assert s.pb is None and any("RM" in w for w in s.warnings)


def test_literal_constants_match_the_models() -> None:
    (s,) = sales_of(HEAD + "  1.A/B\n" + BRUGE + FA1)
    assert s.ag_kind == AG_DIRECT


# --- FE is the endorsement element -----------------------------------------------------------


def test_fe_endorsements_are_kept_as_text() -> None:
    text = (
        HEAD
        + " 3 FE PAX VOLUNTARY CHNGS RESTRICTED/S4/P1\n"
        + " 4 FE TKT VALD 1Y FRM ISSUE DATE\n"
        + " 5 FE PAX MRU8245.00 NONREF/S4\n"
        + " 6 FE PAX VOLUNTARY CHNGS RESTRICTED/S4/P1\n"
    )
    r = parse_pnr_text(text)
    assert r.endorsements == ("VOLUNTARY CHNGS RESTRICTED", "TKT VALD 1Y FRM ISSUE DATE")
    assert r.nonrefundable_amounts == (d("8245.00"),)


def test_nonrefundable_amount_is_information_only() -> None:
    text = HEAD + "  1.A/B\n  2 RM 5400\n" + BRUGE + FA1 + " 30 FE PAX MRU8245.00 NONREF/S4\n"
    (s,) = sales_of(text)
    assert s.pb == d("5400") and s.tickets[0].total_doc == d("5022.00")
    assert "montant non remboursable : 8245.00 MRU" in s.warnings
