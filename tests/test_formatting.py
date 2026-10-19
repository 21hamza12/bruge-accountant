"""French money and date formatting shared by the CLI-free web pages."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.formatting import date_fr, datetime_fr, money, money_fr
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


@pytest.mark.parametrize("value", ["-0", "-0.00", "-0.001", "-0.004", "0.00", "-0E-10"])
def test_money_fr_never_prints_a_negative_zero(value: str) -> None:
    text = money_fr(Decimal(value))
    assert text in {"0", "0,00"}
    assert not text.startswith("-")


def test_money_fr_keeps_the_sign_of_a_real_negative_amount() -> None:
    assert money_fr(Decimal("-0.01")) == "-0,01"
    assert money_fr(Decimal("-0.006")) == "-0,01"  # rounds to a visible cent: the sign stays


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_money_fr_renders_a_dash_for_a_value_that_is_not_a_finite_number(value: str) -> None:
    """Money on a page must never raise or print "NaN": a corrupt value is shown as "—"."""
    assert money_fr(Decimal(value)) == "—"


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


def test_datetime_fr_is_the_date_then_the_time_in_24_hours() -> None:
    assert datetime_fr(datetime(2026, 10, 1, 9, 5, tzinfo=UTC)) == "1 octobre 2026, 09:05"
    assert datetime_fr(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)) == "31 décembre 2026, 23:59"
    assert datetime_fr(datetime(2026, 1, 15, 0, 0, tzinfo=UTC)) == "15 janvier 2026, 00:00"


def test_datetime_fr_shows_the_clock_of_the_value_in_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    assert datetime_fr(datetime(2026, 10, 1, 1, 30, tzinfo=plus_two)) == "30 septembre 2026, 23:30"
    assert (
        datetime_fr(datetime(2026, 10, 1, 9, 5)) == "1 octobre 2026, 09:05"
    )  # naive: taken as UTC


def test_datetime_fr_filter_is_available_in_templates() -> None:
    value = datetime(2026, 10, 1, 14, 30, tzinfo=UTC)
    assert render_string("{{ d|datetime_fr }}", d=value) == "1 octobre 2026, 14:30"
