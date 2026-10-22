"""Reasons typed by a person (why a sale was edited, a day reopened, a fee overridden).

Whitespace is collapsed and control characters removed (``clean_text``). A reason, when present,
has ``MIN_REASON_LENGTH`` (5) to ``MAX_REASON_LENGTH`` (500) characters; the same lower bound is a
CHECK constraint on the tables that store reasons.
"""

from __future__ import annotations

from app.models import MIN_REASON_LENGTH
from app.parsing.amadeus import clean_text

MAX_REASON_LENGTH = 500
REASON_TOO_SHORT = f"Indiquez le motif ({MIN_REASON_LENGTH} caractères au moins)."
REASON_TOO_LONG = f"Le motif ne peut pas dépasser {MAX_REASON_LENGTH} caractères."


class ReasonError(ValueError):
    """A reason that is missing, too short or too long; ``str(error)`` says which."""


def clean_reason(raw: str | None) -> str | None:
    """The reason with whitespace collapsed, or None when empty or missing. A non-empty reason
    must be 5 to 500 characters long, else ReasonError."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ReasonError(REASON_TOO_SHORT)
    text = " ".join(clean_text(raw).split())
    if not text:
        return None
    if len(text) < MIN_REASON_LENGTH:
        raise ReasonError(REASON_TOO_SHORT)
    if len(text) > MAX_REASON_LENGTH:
        raise ReasonError(REASON_TOO_LONG)
    return text


def required_reason(raw: str | None) -> str:
    """``clean_reason`` for a reason that must be given."""
    text = clean_reason(raw)
    if text is None:
        raise ReasonError(REASON_TOO_SHORT)
    return text
