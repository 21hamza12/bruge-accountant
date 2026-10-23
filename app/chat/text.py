"""Reading French free text for the chat: no model, no network, no state.

``fold`` lower-cases and removes accents and punctuation, so every rule is written once, in plain
ASCII. Everything returned is a typed value (a ``date``, a 10-digit string, a ``Decimal``) built
from digits and a fixed vocabulary, so nothing the person typed is ever copied into a reply.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from app.accounts import AccountError, normalize_staff_sign
from app.sales_rules import ticket_number

_APOSTROPHES = re.compile(r"[’‘'`´]")
_KEPT = re.compile(r"[^a-z0-9/.\-\s]")
_SPACES = re.compile(r"\s+")

MONTHS = (
    "janvier",
    "fevrier",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "aout",
    "septembre",
    "octobre",
    "novembre",
    "decembre",
)
MIN_MONTH_LETTERS = 3


class DateNotUnderstood(ValueError):
    """The text names a date that does not exist (31/02) or is out of range."""


def fold(text: str) -> str:
    """Lower case, no accents, apostrophes as spaces, anything but letters, digits and ``/ . -``
    as a space, single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
    plain = _APOSTROPHES.sub(" ", plain)
    return _SPACES.sub(" ", _KEPT.sub(" ", plain)).strip()


def words(text: str) -> list[str]:
    """The folded words; a hyphen separates words (``aidez-moi``) and ``. /`` around a word are
    dropped (``hier.`` is ``hier``)."""
    return [w for w in (t.strip("./") for t in fold(text).replace("-", " ").split()) if w]


# --- ticket numbers ---------------------------------------------------------------------------

_DOC = re.compile(r"(?<!\d)(\d{3}[ /-]?)?(\d{10})(?!\d)", re.ASCII)


def find_doc(text: str) -> str | None:
    """The first ticket number in ``text`` as the application stores it: ten digits. A thirteen
    digit number (airline code and number, ``495-3940281465``) loses its code, through the same
    rule as the sale form (``sales_rules.ticket_number``)."""
    for match in _DOC.finditer(text):
        prefix = (match.group(1) or "").strip(" /-")
        number, error = ticket_number(prefix + match.group(2), prefix or None)
        if error is None and number is not None:
            return number
    return None


# --- staff signs ------------------------------------------------------------------------------

_SIGN_WORDS = frozenset({"signe", "sign", "agent", "code"})
_SIGN_SHAPE = re.compile(r"\d{2,4}[a-z]{2,3}")
_AFTER_KEYWORD = re.compile(r"[a-z]{2,3}|(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{2,8}")
_ORDINAL = re.compile(r"[1-9]\d?(?:er|ere|e|eme|ieme|nd|nde)")  # "12eme" is not a sign
_NOT_SIGNS = frozenset(
    {"les", "des", "mes", "mon", "ton", "son", "une", "que", "qui", "pas", "par", "sur", "ici"}
    | {"hier", "demain", "avant"}
)


def find_sign(text: str) -> str | None:
    """A staff sign named in ``text``: a word shaped like ``0004SV``, or, after "signe", "agent" or
    "code", a word shaped like a sign (two or three letters, or letters and digits: not "hier",
    not "12eme"). Normalised like the rest of the application, else None."""
    found = words(text)
    for index, word in enumerate(found):
        if word in _NOT_SIGNS or _ORDINAL.fullmatch(word):
            continue
        after = found[index - 1] if index else ""
        named = after in _SIGN_WORDS and _AFTER_KEYWORD.fullmatch(word) is not None
        if _SIGN_SHAPE.fullmatch(word) or named:
            try:
                return normalize_staff_sign(word)
            except AccountError:
                continue
    return None


# --- dates ------------------------------------------------------------------------------------

_NUMERIC_DAY = re.compile(
    r"(?<![\d/.-])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}|\d{2}))?(?![\d/]|\s?(?:k|mille)\b)"
)
_DOTTED_DAY = re.compile(
    r"(?<![\d/.-])(\d{2})\.(\d{2})(?:\.(\d{4}|\d{2}))?(?![\d/]|\s?(?:k|mille)\b)"
)
_YEAR_FIRST = re.compile(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_SLASHED = re.compile(r"\d+(?:/\d+)+")
_WEEKDAYS = frozenset(
    {"lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche", "demain"}
)
_DAY_WORD = re.compile(r"(\d{1,2})(?:er)?")
MAX_YEARS_BACK = 8  # the longest gap between two 29 Februaries (2096 to 2104)


def _month_of(word: str) -> int | None:
    """The month a word names, by a unique prefix of at least three letters (``sept``, ``janv``);
    ``jui`` names two months, so it names none."""
    if len(word) < MIN_MONTH_LETTERS:
        return None
    hits = [i for i, name in enumerate(MONTHS, start=1) if name.startswith(word)]
    return hits[0] if len(hits) == 1 else None


def _build(year: int, month: int, day: int, today: date, *, year_given: bool) -> date:
    if year_given:
        try:
            return date(year, month, day)
        except ValueError:
            raise DateNotUnderstood from None
    for candidate in range(today.year, today.year - MAX_YEARS_BACK - 1, -1):  # "25/12" in
        try:  # October is last year's, and 29 February is the latest one that exists
            chosen = date(candidate, month, day)
        except ValueError:
            continue
        if chosen <= today:
            return chosen
    raise DateNotUnderstood


def _year(raw: str | None, today: date) -> tuple[int, bool]:
    if not raw:
        return today.year, False
    value = int(raw)
    return (value + 2000 if value < 100 else value), True


def _dotted(folded: str, today: date) -> date | None:
    """``19.09`` / ``19.09.26``: day.month with two digits each and in range, else not a date
    (``4.5k`` and ``12.50`` are amounts)."""
    for match in _DOTTED_DAY.finditer(folded):
        if 1 <= int(match[1]) <= 31 and 1 <= int(match[2]) <= 12:
            year, given = _year(match[3], today)
            return _build(year, int(match[2]), int(match[1]), today, year_given=given)
    return None


def find_day(text: str, today: date) -> date | None:
    """The day ``text`` names, or None when it names none: today ("aujourd'hui"), yesterday
    ("hier", "avant-hier"), ``2026-09-19`` / ``2026/09/19``, ``19/09`` (``19/09/2026``,
    ``19.09.26``), or a day and a month name (``19 septembre``, ``1er oct``, ``19 sept 2025``).
    Without a year, the most recent such day that is not after today. Raises
    ``DateNotUnderstood`` for a date that does not exist, for a slashed number that is not a
    date, and for a day the reader does not handle (a weekday, "demain"): a date is never
    guessed, and never silently replaced by today."""
    folded = fold(text)
    first = _YEAR_FIRST.search(folded)
    if first:
        return _build(int(first[1]), int(first[2]), int(first[3]), today, year_given=True)
    numeric = _NUMERIC_DAY.search(folded)
    if numeric:
        year, given = _year(numeric[3], today)
        return _build(year, int(numeric[2]), int(numeric[1]), today, year_given=given)
    dotted = _dotted(folded, today)
    if dotted:
        return dotted
    if _SLASHED.search(folded):
        raise DateNotUnderstood
    found = words(text)
    for index, word in enumerate(found):
        if word in _WEEKDAYS:
            raise DateNotUnderstood
        if word == "hier":
            return today - timedelta(days=2 if found[index - 1 : index] == ["avant"] else 1)
        if word in ("aujourd", "aujourdhui"):
            return today
        day_word = _DAY_WORD.fullmatch(word)
        month = _month_of(found[index + 1]) if day_word and index + 1 < len(found) else None
        if day_word and month is not None:
            raw_year = found[index + 2] if index + 2 < len(found) else ""
            year, given = _year(raw_year if re.fullmatch(r"\d{4}", raw_year) else None, today)
            return _build(year, month, int(day_word[1]), today, year_given=given)
    return None
