"""app/money.py: amounts typed by people, parsed into Decimal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.money import MAX_AMOUNT, MoneyError, parse_amount, to_amount, validate_amount

NBSP = "\u00a0"
NNBSP = "\u202f"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0", "0.00"),
        ("500", "500.00"),
        ("1 500", "1500.00"),
        ("1 500,50", "1500.50"),
        (f"1{NBSP}500", "1500.00"),
        (f"1{NNBSP}500,5", "1500.50"),
        ("  300  ", "300.00"),
        ("12.5", "12.50"),
        ("12,5", "12.50"),
        ("0,01", "0.01"),
        ("1 118 857", "1118857.00"),
        ("999999999999,99", "999999999999.99"),
    ],
)
def test_valid_amounts(text: str, expected: str) -> None:
    result = parse_amount(text)
    assert result == Decimal(expected)
    assert result.as_tuple().exponent == -2


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("", "montant"),
        ("   ", "montant"),
        ("-5", "négatif"),
        ("-0", "négatif"),
        ("-1 500,50", "négatif"),
        ("1,234", "séparateur de milliers"),
        ("1.234", "séparateur de milliers"),
        ("1,500", "1500 sans séparateur"),
        ("1,2345", "décimales"),
        (".5", "montant"),
        ("1,5,5", "montant"),
        ("1,500.50", "montant"),
        ("abc", "montant"),
        ("12 MRU", "montant"),
        ("1e3", "montant"),
        ("NaN", "montant"),
        ("Infinity", "montant"),
        ("٣٠٠", "montant"),  # non-ASCII digits
        ("1,", "montant"),
        (",5", "montant"),
        ("+5", "montant"),
        ("0x10", "montant"),
        ("1_000", "montant"),
        ("1 000 000 000 000", "trop grand"),
        ("9" * 40, "trop grand"),
        ("9" * 500, "montant"),
    ],
)
def test_refused_amounts_have_a_french_message(text: str, word: str) -> None:
    with pytest.raises(MoneyError) as info:
        parse_amount(text)
    assert word in str(info.value)


def test_the_boundary_is_ten_to_the_twelfth_exclusive() -> None:
    assert Decimal(10) ** 12 == MAX_AMOUNT
    assert parse_amount("999999999999.99") < MAX_AMOUNT
    with pytest.raises(MoneyError):
        parse_amount("1000000000000")


@pytest.mark.parametrize("value", [1.5, 0.0, None, 12, b"12", ["1"], True])
def test_parse_amount_rejects_anything_but_text(value: object) -> None:
    with pytest.raises(MoneyError):
        parse_amount(value)  # type: ignore[arg-type]


def test_money_error_is_a_value_error() -> None:
    assert issubclass(MoneyError, ValueError)


def test_validate_amount_accepts_decimal_and_int_and_refuses_float() -> None:
    assert validate_amount(Decimal("300")) == Decimal("300.00")
    assert validate_amount(Decimal("0")) == Decimal("0.00")
    assert validate_amount(7) == Decimal("7.00")
    for bad in (
        1.5,
        Decimal("-1"),
        Decimal("1.234"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal(10) ** 12,
        True,
        "5",
    ):
        with pytest.raises(MoneyError):
            validate_amount(bad)  # type: ignore[arg-type]


def test_validate_amount_allows_trailing_zero_decimals() -> None:
    assert validate_amount(Decimal("1.500")) == Decimal("1.50")


@pytest.mark.parametrize(
    ("value", "word"),
    [
        (Decimal("1E+30"), "trop grand"),
        (Decimal("1E+999999"), "trop grand"),
        (Decimal("-1E+999999"), "négatif"),
        (Decimal("-1E+30"), "négatif"),
        (Decimal("1E-999999"), "décimales"),
    ],
)
def test_validate_amount_never_raises_anything_but_money_error(value: Decimal, word: str) -> None:
    with pytest.raises(MoneyError, match=word):
        validate_amount(value)


def test_to_amount_dispatches_on_the_type() -> None:
    assert to_amount("1 500,5") == Decimal("1500.50")
    assert to_amount(Decimal("7")) == Decimal("7.00")
    assert to_amount(7) == Decimal("7.00")
    for bad in (1.5, None, b"1"):
        with pytest.raises(MoneyError):
            to_amount(bad)  # type: ignore[arg-type]
    with pytest.raises(MoneyError, match="trop grand"):
        to_amount(Decimal("1E+30"))


def test_a_long_run_of_spaces_and_a_long_digit_string_are_refused() -> None:
    with pytest.raises(MoneyError):
        parse_amount(" " * 10_000)
    with pytest.raises(MoneyError, match="trop grand"):
        parse_amount("1" * 100)
    with pytest.raises(MoneyError, match="montant"):
        parse_amount("٣" * 100)  # non-ASCII digits are not digits here
