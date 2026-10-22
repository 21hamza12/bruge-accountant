"""Amounts of money: what people type, parsed into ``Decimal`` (never ``float``).

``parse_amount`` accepts digits with optional group separators (any space, including the
no-break and narrow no-break spaces French number formats use) and ``,`` or ``.`` as the decimal
separator. It refuses negative numbers, signs, exponents, non-ASCII digits, a missing integer or
fraction part (".5", ",5" and "1," are refused, write 0,5 and 1), more than two decimals, and
anything of 10**12 or more (the column is NUMERIC(14,2)). ``1,500`` is not read as fifteen
hundred nor as 1.5: it is ambiguous, so the person is told how to write it. Errors are
``MoneyError`` with a French message that never echoes the input.

Amounts read from an Amadeus file are parsed separately (app/parsing/amadeus.py) on purpose: that
format has its own rules, so do not unify the two.
"""

from __future__ import annotations

import re
from decimal import Decimal

MAX_AMOUNT = Decimal(10) ** 12  # exclusive
CENT = Decimal("0.01")
MAX_INPUT_LENGTH = 64  # after spaces are removed
MAX_RAW_LENGTH = MAX_INPUT_LENGTH * 8  # before: a long run of spaces is still refused cheaply

MESSAGE_REQUIRED = "Saisissez un montant."
MESSAGE_FORMAT = (
    "Ce montant n'est pas valide. Saisissez des chiffres, par exemple 1 500 ou 1 500,50."
)
MESSAGE_NEGATIVE = "Le montant ne peut pas être négatif."
MESSAGE_DECIMALS = "Le montant ne peut avoir que 2 décimales au maximum."
MESSAGE_TOO_LARGE = "Ce montant est trop grand."
MESSAGE_THOUSANDS = "Écrivez 1500 sans séparateur de milliers, ou 1 500 avec une espace."

_NUMBER = re.compile(r"(?P<int>[0-9]+)(?:[.,](?P<frac>[0-9]+))?", re.ASCII)
_DIGITS = re.compile(r"[0-9]+", re.ASCII)


class MoneyError(ValueError):
    """The amount is not acceptable; ``str(error)`` is a French message."""


def _check_range(amount: Decimal) -> Decimal:
    """Sign and size first, rounding last: ``quantize`` raises on a huge exponent."""
    if amount < 0:
        raise MoneyError(MESSAGE_NEGATIVE)
    if amount >= MAX_AMOUNT:
        raise MoneyError(MESSAGE_TOO_LARGE)
    return amount.quantize(CENT)


def parse_amount(text: str) -> Decimal:
    """The amount typed in ``text``, with exactly two decimals; ``MoneyError`` otherwise."""
    if not isinstance(text, str) or len(text) > MAX_RAW_LENGTH:
        raise MoneyError(MESSAGE_FORMAT)
    compact = "".join(text.split())  # str.split() drops NBSP and narrow NBSP too
    if not compact:
        raise MoneyError(MESSAGE_REQUIRED)
    if len(compact) > MAX_INPUT_LENGTH:
        raise MoneyError(MESSAGE_TOO_LARGE if _DIGITS.fullmatch(compact) else MESSAGE_FORMAT)
    if compact.startswith("-"):
        if _NUMBER.fullmatch(compact[1:]):
            raise MoneyError(MESSAGE_NEGATIVE)
        raise MoneyError(MESSAGE_FORMAT)
    match = _NUMBER.fullmatch(compact)
    if match is None:
        raise MoneyError(MESSAGE_FORMAT)
    fraction = match.group("frac") or ""
    if len(fraction) == 3:  # "1,500": ask, do not guess
        raise MoneyError(MESSAGE_THOUSANDS)
    if len(fraction) > 2:
        raise MoneyError(MESSAGE_DECIMALS)
    return _check_range(Decimal(f"{match.group('int')}.{fraction or '0'}"))


def validate_amount(value: Decimal | int) -> Decimal:
    """A ``Decimal`` (or ``int``) amount checked the same way; floats, bools and text refused."""
    if isinstance(value, bool) or not isinstance(value, Decimal | int):
        raise MoneyError(MESSAGE_FORMAT)
    amount = Decimal(value)
    if not amount.is_finite():
        raise MoneyError(MESSAGE_FORMAT)
    checked = _check_range(amount)  # raises for negative or huge before any rounding
    if amount != checked:
        raise MoneyError(MESSAGE_DECIMALS)
    return checked


def to_amount(raw: Decimal | int | str) -> Decimal:
    """``parse_amount`` for text typed by a person, ``validate_amount`` for a Decimal or int."""
    if isinstance(raw, str):
        return parse_amount(raw)
    return validate_amount(raw)
