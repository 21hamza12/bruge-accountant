"""IATA numeric ticket prefixes to airline names."""

from __future__ import annotations

import pytest

from app.airlines import AIRLINES, airline_name
from app.web.templating import render_string

KNOWN = {
    "147": "Royal Air Maroc",
    "157": "Qatar Airways",
    "199": "Tunisair",
    "235": "Turkish Airlines",
    "057": "Air France",
    "074": "KLM",
    "125": "British Airways",
    "071": "Ethiopian Airlines",
    "065": "Saudia",
    "176": "Emirates",
    "607": "Etihad Airways",
    "077": "EgyptAir",
    "124": "Air Algérie",
    "082": "Brussels Airlines",
    "220": "Lufthansa",
    "047": "TAP Air Portugal",
    "075": "Iberia",
    "495": "Mauritania Airlines",  # owner, 27 Oct 2026 (decision 16)
}


@pytest.mark.parametrize(("code", "name"), sorted(KNOWN.items()))
def test_known_prefixes(code: str, name: str) -> None:
    assert airline_name(code) == name


def test_the_table_holds_exactly_the_agreed_prefixes() -> None:
    assert dict(AIRLINES) == KNOWN


def test_an_unknown_code_shows_the_code() -> None:
    assert airline_name("999") == "Cie 999"


def test_whitespace_around_a_code_is_ignored() -> None:
    assert airline_name(" 147 ") == "Royal Air Maroc"


def test_an_empty_code_has_a_readable_label() -> None:
    assert airline_name("") == "Cie inconnue"


def test_airline_filter_in_templates_escapes_unknown_codes() -> None:
    assert render_string("{{ c|airline }}", c="147") == "Royal Air Maroc"
    assert render_string("{{ c|airline }}", c="<b>") == "Cie &lt;b&gt;"
