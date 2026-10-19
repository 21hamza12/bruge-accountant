"""French money and date formatting shared by the CLI-free web pages."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.formatting import date_fr, money, money_fr
from app.web.templating import render_string

NNBSP = " "


def test_money_keeps_its_stable_plain_form() -> None:
    assert money(Decimal("746423.00")) == "746423"
    assert money(Decimal("1234.5")) == "1234.50"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), "0"),
        (Decimal("999"), "999"),
        (Decimal("1000"), f"1{NNBSP}000"),
        (Decimal("1118857.00"), f"1{NNBSP}118{NNBSP}857"),
        (Decimal("1234.5"), f"1{NNBSP}234,50"),
        (Decimal("12.05"), "12,05"),
        (Decimal("-1500"), f"-1{NNBSP}500"),
        (Decimal("123456789"), f"123{NNBSP}456{NNBSP}789"),
    ],
)
def test_money_fr_groups_with_a_narrow_no_break_space(value: Decimal, expected: str) -> None:
    assert money_fr(value) == expected


def test_money_fr_never_uses_a_plain_space_or_a_dot() -> None:
    text = money_fr(Decimal("1118857.25"))
    assert " " not in text
    assert "." not in text


def test_money_fr_filter_is_available_in_templates() -> None:
    assert render_string("{{ v|money_fr }}", v=Decimal("1118857")) == f"1{NNBSP}118{NNBSP}857"


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 1), "1 octobre 2026"),
        (date(2026, 1, 15), "15 janvier 2026"),
        (date(2026, 2, 28), "28 février 2026"),
        (date(2026, 8, 3), "3 août 2026"),
        (date(2026, 12, 31), "31 décembre 2026"),
    ],
)
def test_date_fr(day: date, expected: str) -> None:
    assert date_fr(day) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 10, 1), "jeudi 1 octobre 2026"),  # a Thursday
        (date(2026, 10, 7), "mercredi 7 octobre 2026"),
        (date(2026, 10, 18), "dimanche 18 octobre 2026"),
        (date(2026, 10, 19), "lundi 19 octobre 2026"),
        (date(2026, 10, 20), "mardi 20 octobre 2026"),
        (date(2026, 10, 23), "vendredi 23 octobre 2026"),
        (date(2026, 10, 24), "samedi 24 octobre 2026"),
    ],
)
def test_date_fr_with_the_weekday(day: date, expected: str) -> None:
    assert date_fr(day, weekday=True) == expected


def test_all_twelve_months_have_a_name() -> None:
    names = {date_fr(date(2026, m, 1)).split()[1] for m in range(1, 13)}
    assert len(names) == 12


def test_date_fr_filter_is_available_in_templates() -> None:
    assert render_string("{{ d|date_fr }}", d=date(2026, 10, 1)) == "1 octobre 2026"
    assert (
        render_string("{{ d|date_fr(weekday=True) }}", d=date(2026, 10, 1))
        == "jeudi 1 octobre 2026"
    )
