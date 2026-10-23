"""app/chat/text.py: reading French free text (accents, case, dates, tickets, signs, amounts)."""

from __future__ import annotations

from datetime import date

import pytest

from app.chat.text import (
    DateNotUnderstood,
    find_day,
    find_doc,
    find_sign,
    fold,
    words,
)

TODAY = date(2026, 10, 22)


def test_fold_removes_accents_case_apostrophes_and_punctuation() -> None:
    assert fold("  Qu’est-ce qui BLOQUE ?! ") == "qu est-ce qui bloque"
    assert fold("Billets non envoyés\n\tà côté") == "billets non envoyes a cote"
    assert fold("L'écran d'aujourd'hui") == "l ecran d aujourd hui"
    assert fold("") == ""


def test_words_drop_the_dots_and_dashes_around_a_word() -> None:
    assert words("Mes billets d'hier.") == ["mes", "billets", "d", "hier"]
    assert words("19/09 - feuille") == ["19/09", "feuille"]


# --- days ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mes billets d'aujourd'hui", TODAY),
        ("aujourdhui", TODAY),
        ("feuille d'hier", date(2026, 10, 21)),
        ("HIER", date(2026, 10, 21)),
        ("avant-hier", date(2026, 10, 20)),
        ("avant hier", date(2026, 10, 20)),
        ("feuille du 19 septembre", date(2026, 9, 19)),
        ("feuille du 19 Septembre 2025", date(2025, 9, 19)),
        ("le 1er octobre", date(2026, 10, 1)),
        ("19 sept", date(2026, 9, 19)),
        ("19 sep", date(2026, 9, 19)),
        ("3 janv", date(2026, 1, 3)),
        ("14 juillet", date(2026, 7, 14)),
        ("14 juil", date(2026, 7, 14)),
        ("5 juin", date(2026, 6, 5)),
        ("8 aout", date(2026, 8, 8)),
        ("8 août", date(2026, 8, 8)),
        ("12 décembre", date(2025, 12, 12)),  # not after today: last year's
        ("19/09", date(2026, 9, 19)),
        ("19.09", date(2026, 9, 19)),
        ("19-09", date(2026, 9, 19)),
        ("9/9", date(2026, 9, 9)),
        ("19/09/2026", date(2026, 9, 19)),
        ("19/09/25", date(2025, 9, 19)),
        ("2026-09-19", date(2026, 9, 19)),
        ("25/12", date(2025, 12, 25)),
        ("22/10", TODAY),
    ],
)
def test_find_day(text: str, expected: date) -> None:
    assert find_day(text, TODAY) == expected


@pytest.mark.parametrize(
    "text", ["", "mes billets", "billet 3940299439", "juin", "19", "le 19 jui", "12 trucs"]
)
def test_find_day_is_none_when_there_is_no_date(text: str) -> None:
    assert find_day(text, TODAY) is None


@pytest.mark.parametrize(
    "text", ["31/02", "32/01", "0/5", "30 février", "2026-13-01", "29/02/2026"]
)
def test_find_day_refuses_a_date_that_does_not_exist(text: str) -> None:
    with pytest.raises(DateNotUnderstood):
        find_day(text, TODAY)


def test_a_leap_day_is_understood_in_a_leap_year() -> None:
    assert find_day("29/02/2028", TODAY) == date(2028, 2, 29)
    with pytest.raises(DateNotUnderstood):  # no 29 February in 2026
        find_day("29/02/2026", TODAY)


def test_a_leap_day_without_a_year_is_the_latest_one_not_after_today() -> None:
    assert find_day("29/02", TODAY) == date(2024, 2, 29)
    assert find_day("29 février", TODAY) == date(2024, 2, 29)
    assert find_day("29/02", date(2028, 2, 29)) == date(2028, 2, 29)
    assert find_day("29/02", date(2028, 2, 28)) == date(2024, 2, 29)
    with pytest.raises(DateNotUnderstood):  # no 30 February in any year
        find_day("30/02", TODAY)


def test_a_year_first_date_is_understood() -> None:
    assert find_day("2026/09/19", TODAY) == date(2026, 9, 19)
    assert find_day("feuille du 2026/9/5", TODAY) == date(2026, 9, 5)
    with pytest.raises(DateNotUnderstood):
        find_day("2026/13/19", TODAY)


@pytest.mark.parametrize(
    "text", ["lundi", "billets de Mardi", "feuille de vendredi dernier", "samedi", "demain"]
)
def test_a_weekday_is_a_date_the_reader_cannot_use(text: str) -> None:
    with pytest.raises(DateNotUnderstood):
        find_day(text, TODAY)


@pytest.mark.parametrize("text", ["1/2/3", "19/09/2026/5", "2026/09"])
def test_a_slashed_token_that_is_not_a_date_is_refused_not_ignored(text: str) -> None:
    with pytest.raises(DateNotUnderstood):
        find_day(text, TODAY)


@pytest.mark.parametrize(
    "text", ["ventes 4.5k", "ventes 4.5", "12.50k", "45.50", "billets 12.5 mille", "32.13", "1.2"]
)
def test_an_amount_is_not_a_date(text: str) -> None:
    assert find_day(text, TODAY) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("19.09", date(2026, 9, 19)),
        ("19.09.26", date(2026, 9, 19)),
        ("le 19.09.", date(2026, 9, 19)),
    ],
)
def test_a_dotted_date_is_day_dot_month(text: str, expected: date) -> None:
    assert find_day(text, TODAY) == expected


# --- ticket numbers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("billet 3940299439", "3940299439"),
        ("3940299439", "3940299439"),
        ("billet n°3940299439 svp", "3940299439"),
        ("4953940299439", "3940299439"),  # airline code + number
        ("495-3940299439", "3940299439"),
        ("495 3940299439", "3940299439"),
        ("495/3940299439", "3940299439"),
        ("annule 3940299439 et 3940299440", "3940299439"),  # the first one
    ],
)
def test_find_doc(text: str, expected: str) -> None:
    assert find_doc(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "billet", "394029943", "39402994390", "394029943901", "39402994391234", "٣٩٤٠٢٩٩٤٣٩"],
)
def test_find_doc_needs_ten_or_thirteen_ascii_digits(text: str) -> None:
    assert find_doc(text) is None


# --- signs --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("billets de 0004sv", "0004SV"),
        ("billets de 0004SV du 19/09", "0004SV"),
        ("signe sv", "SV"),
        ("agent ab12", "AB12"),
        ("code 0006ac", "0006AC"),
    ],
)
def test_find_sign(text: str, expected: str) -> None:
    assert find_sign(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "mes billets",
        "billets de hier",
        "signe",
        "billet 3940299439",
        "agent hier",
        "code 12eme",
        "le 12eme billet",
        "signe demain",
        "agent les",
    ],
)
def test_find_sign_is_none_without_a_sign(text: str) -> None:
    assert find_sign(text) is None
