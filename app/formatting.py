"""Text formatting shared by the CLI and the web pages."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

NARROW_NBSP = " "  # French thousands separator: it never breaks across lines
NOT_AVAILABLE = "—"  # shown instead of a figure that does not exist

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
    decimal comma ("1 118 857", "1 234,50").

    A value that is not a finite number (NaN, an infinity) shows as "—" rather than raising: a
    page must still render, and a dash says "no figure" where "NaN" would look like a bug. A
    negative amount that rounds to zero ("-0.001") prints without its sign, never as "-0,00".
    """
    if not value.is_finite():
        return NOT_AVAILABLE
    plain = money(value)
    sign = "-" if plain.startswith("-") else ""
    plain = plain.removeprefix("-")
    if not plain.strip("0."):
        sign = ""
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


def day_month_fr(value: date) -> str:
    """ "25 juin": the day and the month, no year (for a date close to today)."""
    return f"{value.day} {_MONTHS[value.month - 1]}"


def datetime_fr(value: datetime) -> str:
    """ "1 octobre 2026, 14:30": the date, then the 24-hour clock in UTC (a value without a time
    zone is taken as UTC, which is what the database stores)."""
    moment = value.astimezone(UTC) if value.tzinfo is not None else value
    return f"{date_fr(moment.date())}, {moment:%H:%M}"
