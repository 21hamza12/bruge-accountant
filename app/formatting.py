"""Text formatting shared by the CLI and the web pages."""

from __future__ import annotations

from decimal import Decimal


def money(value: Decimal) -> str:
    """Stable text form: whole amounts without decimals ("746423"), otherwise 2 ("1234.50")."""
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.quantize(Decimal("0.01")), "f")
