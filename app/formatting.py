"""Text formatting shared by the CLI and the web pages."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

NARROW_NBSP = " "  # French thousands separator: it never breaks across lines

_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def money(value: Decimal) -> str:
    """Stable text form: whole amounts without decimals ("746423"), otherwise 2 ("1234.50")."""
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.quantize(Decimal("0.01")), "f")


def money_fr(value: Decimal) -> str:
    """French display form: groups of three digits joined by a narrow no-break space and a
    decimal comma ("1 118 857", "1 234,50")."""
    plain = money(value)
    sign = "-" if plain.startswith("-") else ""
    plain = plain.removeprefix("-")
    whole, _, fraction = plain.partition(".")
    groups: list[str] = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    text = NARROW_NBSP.join(groups)
    if fraction:
        text = f"{text},{fraction}"
    return f"{sign}{text}"


def date_fr(value: date, *, weekday: bool = False) -> str:
    """ "1 octobre 2026", or "jeudi 1 octobre 2026" with ``weekday``. No locale needed."""
    text = f"{value.day} {_MONTHS[value.month - 1]} {value.year}"
    return f"{_WEEKDAYS[value.weekday()]} {text}" if weekday else text
