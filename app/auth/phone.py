"""Phone numbers in E.164 (``+`` and 8 to 15 digits), with Mauritania (+222) as the default.

Accepted input, after trimming spaces: optional ``+`` or ``00``, then digits, with spaces,
dashes and dots as separators.

* ``+222 xx xx xx xx`` / ``00222 xx xx xx xx``: exactly 8 digits after the country code.
* A bare 8-digit number is a Mauritanian local number.
* ``+`` (or ``00``) with another country code: only the generic E.164 length (8 to 15 digits,
  not starting with 0) is checked. No mobile prefixes are hard-coded.

Everything else is refused; in particular a country code without ``+`` or ``00`` is ambiguous.
The error message is French (it is shown to users) and never echoes the input.
"""

from __future__ import annotations

import re

DEFAULT_COUNTRY_CODE = "222"
LOCAL_NUMBER_LENGTH = 8
MIN_DIGITS = 8
MAX_DIGITS = 15
# Anything longer cannot be a phone number; refusing early bounds the work done on hostile input.
MAX_INPUT_LENGTH = 64

_SEPARATOR_RE = re.compile(r"[ \-. ]")
_ASCII_DIGITS = re.compile(r"[0-9]+")

MESSAGE = (
    "Ce numéro de téléphone n'est pas valide. "
    "Saisissez 8 chiffres (par exemple 12 34 56 78) ou un numéro international (+33 6 ...)."
)


class InvalidPhoneError(ValueError):
    """The text is not a phone number we accept."""

    def __init__(self) -> None:
        super().__init__(MESSAGE)


def normalize_phone(raw: str) -> str:
    """Return the E.164 form of ``raw`` or raise :class:`InvalidPhoneError`."""
    if not isinstance(raw, str) or len(raw) > MAX_INPUT_LENGTH:
        raise InvalidPhoneError
    text = raw.strip("  ")
    international = text.startswith("+")
    if international:
        text = text[1:]
    compact = _SEPARATOR_RE.sub("", text)
    if not _ASCII_DIGITS.fullmatch(compact):
        raise InvalidPhoneError
    if not international and len(compact) == LOCAL_NUMBER_LENGTH:
        return f"+{DEFAULT_COUNTRY_CODE}{compact}"
    if not international and compact.startswith("00"):
        international = True
        compact = compact[2:]
    if not international:
        raise InvalidPhoneError
    if compact.startswith(DEFAULT_COUNTRY_CODE):
        if len(compact) != len(DEFAULT_COUNTRY_CODE) + LOCAL_NUMBER_LENGTH:
            raise InvalidPhoneError
    elif not MIN_DIGITS <= len(compact) <= MAX_DIGITS or compact.startswith("0"):
        raise InvalidPhoneError
    return f"+{compact}"


def phone_digit_forms(phone: str) -> set[str]:
    """Digit-only spellings of an E.164 number: with country code, with ``00``, local (+222)."""
    digits = phone.lstrip("+")
    forms = {digits, f"00{digits}"}
    if digits.startswith(DEFAULT_COUNTRY_CODE) and len(digits) == 11:
        forms.add(digits[len(DEFAULT_COUNTRY_CODE) :])
    return forms


def mask_phone(phone: str) -> str:
    """``+22212345612`` -> ``+222******12``; logs and audit events never hold a full number."""
    head, tail = 4, 2
    if len(phone) <= head + tail:
        return "*" * len(phone)
    return phone[:head] + "*" * (len(phone) - head - tail) + phone[-tail:]
