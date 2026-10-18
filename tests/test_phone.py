"""Phone normalization to E.164, default country Mauritania (+222)."""

from __future__ import annotations

from typing import Any

import pytest

from app.auth.phone import InvalidPhoneError, normalize_phone, phone_digit_forms

MR = "+22212345678"


@pytest.mark.parametrize(
    "raw",
    [
        "+222 12 34 56 78",
        "+22212345678",
        "00222 12 34 56 78",
        "0022212345678",
        "12345678",
        "12 34 56 78",
        "12-34-56-78",
        "12.34.56.78",
        "+222-12-34-56-78",
        "+222.12.34.56.78",
        "  +222 12 34 56 78  ",
        " 12 34 56 78",  # a non-breaking space from a copy-paste
        "+222 12-34.56 78",
        "00222-12-34-56-78",
    ],
)
def test_mauritanian_numbers(raw: str) -> None:
    assert normalize_phone(raw) == MR


def test_normalization_is_idempotent() -> None:
    assert normalize_phone(normalize_phone("12 34 56 78")) == MR


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+33 6 12 34 56 78", "+33612345678"),
        ("+1 202 555 0143", "+12025550143"),
        ("0033 6 12 34 56 78", "+33612345678"),
        ("+212 6 12 34 56 78", "+212612345678"),
        ("+12345678", "+12345678"),  # shortest accepted: 8 digits
        ("+123456789012345", "+123456789012345"),  # longest accepted: 15 digits
    ],
)
def test_other_countries_in_plus_form(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "1234567",  # 7 digits: too short for a local number
        "123456789",  # 9 digits without a country code: ambiguous
        "222 12 34 56 78",  # country code without + or 00: ambiguous
        "+222 1234567",  # Mauritanian numbers have 8 digits
        "+222 123456789",
        "+2221234567a",
        "12 34 56 7a",
        "+222 12 34 56 78 ext 5",
        "12 34 56 78 +",
        "12+34 56 78",
        "++22212345678",
        "+1234567",  # 7 digits: below the E.164 floor used here
        "+1234567890123456",  # 16 digits: above E.164
        "+0 123 456 789",  # a country code never starts with 0
        "00",
        "+",
        "(222) 12 34 56 78",
        "+222 12 34 56 78\n",  # control characters are not separators
        "١٢٣٤٥٦٧٨",  # Arabic-Indic digits
        "１２３４５６７８",  # full-width digits
        "+222 12 34 56 78; DROP TABLE users",
    ],
)
def test_rejects_everything_else(raw: str) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


@pytest.mark.parametrize("raw", [None, 12345678, b"12345678", ["12345678"]])
def test_rejects_non_strings(raw: Any) -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone(raw)


def test_error_is_a_value_error_with_a_french_message() -> None:
    with pytest.raises(ValueError, match="numéro") as info:
        normalize_phone("123")
    assert isinstance(info.value, InvalidPhoneError)
    assert "123" not in str(info.value)  # never echo what was typed (it may be a password)


def test_very_long_input_is_rejected_quickly() -> None:
    with pytest.raises(InvalidPhoneError):
        normalize_phone("1" * 10_000)
    with pytest.raises(InvalidPhoneError):
        normalize_phone(" " * 10_000 + "12345678")


def test_digit_forms_cover_the_ways_a_phone_can_be_typed() -> None:
    assert phone_digit_forms(MR) == {"22212345678", "0022212345678", "12345678"}
    assert phone_digit_forms("+33612345678") == {"33612345678", "0033612345678"}
