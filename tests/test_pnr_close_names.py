"""Close-name suggestions for an agency text that matches no partner (typos, initials), and the
match information the paste screen needs. Suggestions only: nothing is ever chosen silently."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.parsing.pnr import parse_pnr_text
from app.parsing.pnr_sales import damerau_levenshtein, proposed_sales, suggest_close

PARTNERS = [
    (1, "ABSHER VOYAGE"),
    (2, "REND TOURISME ET VOYAGES"),
    (3, "WORLD GATE TOURISM"),
    (4, "MUNDO DE VIAGENS"),
]
FIXTURES = Path(__file__).parent / "fixtures" / "pnr"


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [
        ("abc", "abc", 0),
        ("abc", "ab", 1),
        ("abc", "abd", 1),
        ("abc", "acb", 1),  # a swap of neighbours costs one
        ("", "abc", 3),
        ("kitten", "sitting", 3),
    ],
)
def test_damerau_levenshtein(a: str, b: str, distance: int) -> None:
    assert damerau_levenshtein(a, b) == distance
    assert damerau_levenshtein(b, a) == distance


def test_one_letter_dropped() -> None:
    assert suggest_close("ABSHR VOYAGE", PARTNERS) == 1


def test_one_letter_added_and_case_and_accents_do_not_matter() -> None:
    assert suggest_close("absher  voyagee", PARTNERS) == 1
    assert (
        suggest_close("Mundo dé Viagens", PARTNERS) == 4
    )  # distance 0: the caller matched it first
    assert suggest_close("Mundo de Viagenz", PARTNERS) == 4


def test_a_swap_of_two_letters() -> None:
    assert suggest_close("ABSEHR VOYAGE", PARTNERS) == 1


def test_short_keys_allow_one_mistake_and_long_keys_two() -> None:
    assert suggest_close("REND", [(9, "RENT")]) is None  # under five characters: no guessing
    assert suggest_close("ABSHER", [(9, "ABSHAR")]) == 9  # six characters, one mistake
    assert suggest_close("ABSHER", [(9, "ABSHXX")]) is None  # two mistakes are too many here
    assert suggest_close("WORLD GATE TURISM", PARTNERS) == 3  # one mistake
    assert suggest_close("WORLD GTE TURISM", PARTNERS) == 3  # two mistakes, a long key


def test_too_far_gives_nothing() -> None:
    assert suggest_close("ZZZZZZ TRAVEL", PARTNERS) is None
    assert suggest_close("", PARTNERS) is None
    assert suggest_close("ABSHER VOYAGE", []) is None


def test_initials_of_the_name() -> None:
    assert suggest_close("WG TOURISM", PARTNERS) == 3
    assert suggest_close("RT VOYAGES", PARTNERS) == 2  # "ET" is a small word, not an initial


def test_initials_need_three_words_in_the_name_and_a_key_of_five_characters() -> None:
    assert suggest_close("GT", PARTNERS) is None
    assert suggest_close("WGT", PARTNERS) is None  # three letters: too short to guess from
    assert suggest_close("WG TOURISM", [(3, "WORLD TOURISM")]) is None  # two words: not enough


def test_two_partners_at_the_same_distance_is_a_tie_and_gives_nothing() -> None:
    twins = [(1, "ABSHER VOYAGE"), (2, "ABSHER VOYAGES")]
    assert suggest_close("ABSHER VOYAGEX", twins) is None
    assert (
        suggest_close("WG TOURISM", [(1, "WORLD GATE TOURISM"), (2, "WIDE GLOBE TOURISM")]) is None
    )


def test_an_alias_counts_like_a_name_and_the_same_partner_twice_is_no_tie() -> None:
    candidates = [*PARTNERS, (1, "ABS VOYAGE")]
    assert suggest_close("ABSHR VOYAGE", candidates) == 1


def test_the_best_distance_wins_over_a_worse_one() -> None:
    near = [(1, "ABSHER VOYAGE"), (2, "ABSHER VOYAGEXYZ")]
    assert suggest_close("ABSHER VOYAGEX", near) == 1


def _paste(name: str, partners: list[tuple[int, str]], aliases: list[tuple[int, str]]):  # type: ignore[no-untyped-def]
    text = (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")
    return proposed_sales(parse_pnr_text(text), partners, aliases)


def test_an_approved_alias_matches_exactly() -> None:
    (sale,) = _paste("partner_no_rm", PARTNERS, [(2, "REND")])
    assert (sale.partner_id, sale.agency_match, sale.agency_text) == (
        2,
        "exact",
        "REND TOURISME ET VOYAGES",
    )
    (sale,) = _paste("partner_no_rm", [(1, "ABSHER VOYAGE")], [(1, "REND TOURISME ET VOYAGES")])
    assert (sale.partner_id, sale.agency_match) == (1, "exact")
    assert sale.warnings == ()


def test_the_match_and_the_suggestion_are_reported() -> None:
    (sale,) = _paste("partner_no_rm", [(7, "REND")], [])
    assert (sale.partner_id, sale.agency_match) == (7, "prefix")
    (sale,) = _paste("partner_no_rm", [(7, "RAND TOURISME ET VOYAGES")], [])
    assert (sale.partner_id, sale.agency_match, sale.suggested_partner_id) == (
        None,
        "none",
        7,
    )
    (sale,) = _paste("partner_no_rm", [(7, "ZZZ")], [])
    assert (sale.partner_id, sale.agency_match, sale.suggested_partner_id) == (None, "none", None)


def test_the_same_partner_by_name_and_by_alias_is_not_ambiguous() -> None:
    (sale,) = _paste("partner_no_rm", [(2, "REND TOURISME ET VOYAGES")], [(2, "REND")])
    assert (sale.partner_id, sale.agency_match) == (2, "exact")
    (sale,) = _paste(
        "partner_no_rm", [(2, "REND TOURISME ET VOYAGES SARL")], [(2, "REND TOURISME")]
    )
    assert sale.partner_id == 2  # prefix of both the name and the alias: still one partner


def test_a_direct_sale_by_the_ap_agency_is_an_exact_match() -> None:
    (sale,) = _paste("adults_with_infants", PARTNERS, [])[:1]
    assert (sale.ag_kind, sale.agency_match, sale.agency_text) == ("direct", "exact", "BRUGE")


@pytest.mark.parametrize(
    ("text", "candidates", "expected"),
    [
        ("ALPHA VOYAGES", [(1, "ATLAS VOYAGES")], None),
        ("ATLAS", [(1, "ALPHA VOYAGES")], None),
        ("SAHARA TOURS", [(2, "SAFARI TOURS")], None),  # two edits, an eleven-character key
        ("SAFARA TOURS", [(1, "SAHARA TOURS"), (2, "SAFARI TOURS")], None),  # a tie
        (
            "AFERICA TRAVEL AND TOURS",
            [(1, "AFRICA TRAVEL AND TOURS"), (2, "AMERICA TRAVEL AND TOURS")],
            None,
        ),
        ("GLOBA TRAVEL", [(1, "GLOBAL TRAVEL"), (2, "GLOBE TRAVEL")], None),
        ("GLOBE TRAVEL", [(1, "GLOBAL TRAVEL"), (2, "GLOBE TRAVEL")], 2),  # exact beats close
    ],
)
def test_sibling_names_never_give_a_wrong_suggestion(
    text: str, candidates: list[tuple[int, str]], expected: int | None
) -> None:
    assert suggest_close(text, candidates) == expected


def test_two_edits_only_from_twelve_characters() -> None:
    assert suggest_close("ABCDEFGHIJK", [(1, "ABCDEFGHIXY")]) is None  # eleven: one edit at most
    assert suggest_close("ABCDEFGHIJKL", [(1, "ABCDEFGHIJXY")]) == 1  # twelve: two edits


@pytest.mark.parametrize(
    ("a", "b", "distance"),
    [("ab", "ba", 1), ("xab", "xba", 1), ("abx", "bax", 1), ("abcd", "bacx", 2), ("ca", "abc", 3)],
)
def test_swaps_at_the_edges_and_swap_with_an_edit(a: str, b: str, distance: int) -> None:
    assert damerau_levenshtein(a, b) == distance
