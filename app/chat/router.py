"""From a message to a lookup: ``parse_message`` (pure) and ``route_message`` (the entry point).

``parse_message`` turns the text into an ``Intent``: a tool name and typed arguments, or no tool
at all. It is plain code over ``app.chat.text`` (accents and case ignored, extra words tolerated).
It never runs SQL and builds no SQL: the only things that leave it are a fixed tool name, a date,
a ten-digit number, a staff sign and, for the partner accounts (phase F4), a partner's name made of
the folded words of the message (letters and digits only), which the tool matches in Python
against the agency's partners and aliases.

Partner accounts: « solde de <partenaire> » (``partner_balance``), « qui doit de l'argent ? » or
« impayés » (``partners_owing``) and « relevé <partenaire> [<période>] » (``partner_statement``,
the period as « 1-15 octobre », « 16 au 31 oct 2026 », a date, or nothing for the latest closed
one).

``route_message`` runs the intent through the registry (``tools.run_tool``: permission, arguments,
handler), with the rights of the logged-in user.

THE SEAM FOR PART 2: a message no rule understands goes to ``unknown``, a function of
``(ChatContext, text)`` that returns a ``Reply``. Here it is ``polite_unknown`` (a French
"I did not understand" and the help of the role, outcome ``unknown``). Part 2 passes a function
that asks the model, which may call ``tools.run_tool`` with the same checks, and falls back to
``polite_unknown`` when the AI is off, over budget or unsure.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.chat import reply as r
from app.chat.reply import Reply
from app.chat.text import MONTHS, DateNotUnderstood, find_day, find_doc, find_sign, fold, words
from app.chat.tools import BAD_DAY_HINT, BAD_DOC, ChatContext, help_lines, run_tool

UnknownHandler = Callable[[ChatContext, str], Reply]

# ``[ \t]*`` and not ``\s*``: ``\s`` also matches the newline, so a text of many blank lines made
# the search quadratic (every line start re-scanned all the lines after it).
_RP_HEADER = re.compile(r"^[ \t]*RP/\S", re.MULTILINE)
_ELEMENT_LINE = re.compile(
    r"^ {0,2}\d{1,3}(?:\.[A-Z(]| +(?:FA|FE|FP|FO|AP|TK|RM|OSI|SSR) )", re.MULTILINE
)
# One typed line of a screen: a fare line (FA / FP), or a passenger NAME/FIRST with its title.
_FARE_LINE = re.compile(r"(?:^|\s)(?:FA|FP)\s+\S")
_PASSENGER = re.compile(r"\b[A-Z]{2,}/[A-Z]{2,} (?:MR|MRS|MS|MSTR|MISS|CHD|INF)\b")
MIN_ELEMENT_LINES = 2
MAX_SCAN = 20_000  # characters looked at: the paste screen refuses 16 KB anyway

CANCEL = frozenset({"annule", "annuler", "annulez", "supprime", "supprimer", "supprimez", "void"})
SHEET = frozenset({"feuille", "tjq", "export", "exporter", "excel", "xlsx"})
MISSING = frozenset({"manquant", "manquants", "manquante", "manquantes"})
FLAGS = frozenset(
    {"flag", "flags", "ecart", "ecarts", "anomalie", "anomalies", "alerte", "alertes"}
)
SALES = frozenset(
    {"billet", "billets", "ticket", "tickets", "vente", "ventes", "reservation", "reservations"}
)
HELP = frozenset({"aide", "aidez", "aider", "help", "menu"})
HELP_PHRASES = ("besoin d aide", "sais tu faire", "sais faire")
HELP_MAX_WORDS = 3  # "aide", "aidez-moi", "menu": a longer sentence is about something else
HELP_PHRASE_MAX_WORDS = 6
MINE = frozenset({"mes", "mon", "ma"})
# A long number next to one of these is an amount ("BADI a payé 600000"), not a ticket.
AMOUNT_WORDS = frozenset(
    {"paye", "payee", "payer", "paiement", "montant", "mru", "um", "ouguiya", "ouguiyas", "cash"}
    | {"mille"}
)
_LONG_NUMBER = re.compile(r"(?<![0-9])[0-9]{6,}(?![0-9])", re.ASCII)

# Partner accounts (F4).
BALANCE = frozenset({"solde", "soldes"})
STATEMENT = frozenset({"releve", "releves"})
OWE = frozenset({"doit", "doivent"})
UNPAID = frozenset({"impaye", "impayes", "impayee", "impayees"})
# Words before a partner's name that are not part of it ("solde de", "relevé du").
NAME_FILLER = frozenset(
    {"de", "du", "d", "des", "pour", "chez", "le", "la", "l", "les", "au", "a", "mon", "notre"}
)
# Words that end a partner's name: the period that follows it.
NAME_END = frozenset({"du", "au", "periode", "quinzaine", "hier", "aujourd", "aujourdhui"})
_HALF_MONTH = re.compile(
    r"(?<![0-9/])([0-9]{1,2})(?:er)?(?:\s*(?:-|au\b|a\b)?\s*[0-9]{1,2})?"
    r"\s+([a-z]{3,})(?:\s+([0-9]{4}))?(?![0-9/])"
)
MIN_MONTH_LETTERS = 3

HELP_TOOL = "help"
SHEET_TOOL = "day_sheet"
SALES_TOOL = "list_sales"
STATUS_TOOL = "ticket_status"
VOID_TOOL = "void_link"
BALANCE_TOOL = "partner_balance"
OWING_TOOL = "partners_owing"
STATEMENT_TOOL = "partner_statement"
UNKNOWN_TEXT = "Je n'ai pas compris votre demande."


@dataclass(frozen=True)
class Intent:
    """``tool`` is None when nothing matched; ``error`` is a French message for a request that
    named a tool but a date or number that cannot be read (shown instead of running the tool)."""

    tool: str | None
    args: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def looks_like_amadeus(text: str) -> bool:
    """A pasted Amadeus screen: an ``RP/`` line, or at least two element lines (``1.NAME``,
    `` 5 FA ...``). Only the first ``MAX_SCAN`` characters are looked at."""
    head = text[:MAX_SCAN]
    if _RP_HEADER.search(head):
        return True
    return len(_ELEMENT_LINE.findall(head)) >= MIN_ELEMENT_LINES


def looks_like_fragment(text: str) -> bool:
    """A piece of an Amadeus screen typed in or pasted in part: one element line (``1.DUPONT/
    JEAN MR``, `` 5 FA ...``), a fare line, or a passenger name with its title. Such a message is
    stored as the placeholder, not as typed (``store.stored_text``)."""
    head = text[:MAX_SCAN]
    return bool(
        looks_like_amadeus(head)
        or _ELEMENT_LINE.search(head)
        or _FARE_LINE.search(head)
        or _PASSENGER.search(head)
    )


def _day_or_error(text: str, today: date) -> tuple[date | None, str]:
    try:
        return find_day(text, today), ""
    except DateNotUnderstood:
        return None, BAD_DAY_HINT


_PARTICIPLES = frozenset(
    {"annulé"}
)  # "annule" folded is also the imperative: "annulé" is a question


def _cancels(text: str, present: set[str]) -> bool:
    """An imperative or infinitive cancel verb (annule, annuler, supprime...). The participle
    "annulé" (a question about a ticket: "est-il annulé ?") folds to the same letters as the
    imperative "annule", so the accent is looked at in the text as typed."""
    if not present & CANCEL:
        return False
    typed = re.findall(r"[^\W\d_]+", unicodedata.normalize("NFC", text).casefold())
    return any(fold(t) in CANCEL and t not in _PARTICIPLES for t in typed)


def _is_help(text: str, found: list[str]) -> bool:
    """A short message that asks for the menu ("aide", "?", "que sais-tu faire")."""
    if text.strip() == "?":
        return True
    if len(found) <= HELP_MAX_WORDS and set(found) & HELP:
        return True
    joined = " ".join(found)
    return len(found) <= HELP_PHRASE_MAX_WORDS and any(p in joined for p in HELP_PHRASES)


def _is_amount_context(present: set[str]) -> bool:
    return bool(present & AMOUNT_WORDS) or any(
        w.endswith("k") and w[:-1].isdigit() for w in present
    )


def _month(word: str) -> int | None:
    """The month a word names: a prefix of at least three letters of one month only."""
    if len(word) < MIN_MONTH_LETTERS:
        return None
    hits = [i for i, name in enumerate(MONTHS, start=1) if name.startswith(word)]
    return hits[0] if len(hits) == 1 else None


def _ends_name(word: str) -> bool:
    return word in NAME_END or any(c.isdigit() for c in word) or _month(word) is not None


def _partner_after(found: list[str], keywords: frozenset[str], *, until_period: bool) -> str:
    """The partner's name: the words after the first keyword, without the leading fillers ("de",
    "du"...), up to the period when ``until_period`` (a number, a month, "du", "periode"...).
    Folded words only."""
    start = next((i for i, w in enumerate(found) if w in keywords), len(found)) + 1
    name: list[str] = []
    for word in found[start:]:
        if not name and word in NAME_FILLER:
            continue
        if until_period and _ends_name(word):
            break
        name.append(word)
    return " ".join(name)


def _half_month_day(text: str, today: date) -> date | None:
    """The first day named by a half-month such as « 1-15 octobre », « 16 au 31 oct 2026 » or
    « 15 oct »: that day (the tool takes its half-month). Without a year, the latest such day
    that is not after today. None when the text names none; ``DateNotUnderstood`` for a day
    that does not exist."""
    for found in _HALF_MONTH.finditer(fold(text)):
        month = _month(found[2])
        if month is None:
            continue
        day = int(found[1])
        try:
            if found[3]:
                return date(int(found[3]), month, day)
            chosen = date(today.year, month, day)
            return chosen if chosen <= today else date(today.year - 1, month, day)
        except ValueError:
            raise DateNotUnderstood from None
    return None


def _statement(text: str, found: list[str], today: date) -> Intent:
    args: dict[str, Any] = {}
    name = _partner_after(found, STATEMENT, until_period=True)
    if name:
        args["partner"] = name
    try:
        day = _half_month_day(text, today) or find_day(text, today)
    except DateNotUnderstood:
        return Intent(STATEMENT_TOOL, args, BAD_DAY_HINT)
    if day is not None:
        args["day"] = day
    return Intent(STATEMENT_TOOL, args)


def _owing(found: list[str], present: set[str]) -> bool:
    """« qui doit de l'argent ? », « qui nous doit », « impayés », « partenaires en retard »."""
    if present & UNPAID:
        return True
    if present & OWE and present & {"argent", "nous"}:  # not « qui doit valider ? »
        return True
    return "retard" in present and bool(present & {"partenaire", "partenaires"})


def parse_message(text: str, today: date) -> Intent:
    """What the message asks for. Order matters: the more specific wins. A pasted screen first;
    then a ticket number (it decides the request on its own: a status, or a void when an
    imperative verb says so, whatever else the sentence says); then the short help request, and
    the lookups that name no ticket."""
    if looks_like_amadeus(text):
        return Intent("paste_sale", {"text": text})
    found = words(text)
    present = set(found)
    doc = find_doc(text)
    if doc:
        return Intent(VOID_TOOL if _cancels(text, present) else STATUS_TOOL, {"doc": doc})
    if _LONG_NUMBER.search(text) and not _is_amount_context(present):
        return Intent(VOID_TOOL if _cancels(text, present) else STATUS_TOOL, error=BAD_DOC)
    if _is_help(text, found):
        return Intent(HELP_TOOL)
    if _cancels(text, present):
        return Intent(VOID_TOOL)
    if present & STATEMENT:
        return _statement(text, found, today)
    if present & BALANCE:
        name = _partner_after(found, BALANCE, until_period=False)
        return Intent(BALANCE_TOOL, {"partner": name} if name else {})
    if _owing(found, present):
        return Intent(OWING_TOOL)
    if present & SHEET:
        day, error = _day_or_error(text, today)
        return Intent(SHEET_TOOL, {"day": day} if day else {}, error)
    if present & MISSING or (
        present & {"non", "pas"} and any(w.startswith("envoy") for w in found)
    ):
        return Intent("not_sent_tickets")
    if present & FLAGS or any(w.startswith("bloq") for w in found):
        return Intent("open_flags")
    if present & SALES:
        day, error = _day_or_error(text, today)
        args: dict[str, Any] = {}
        if day:
            args["day"] = day
        sign = find_sign(text)
        if sign:
            args["sign"] = sign
        if present & MINE:
            args["mine"] = True
        return Intent(SALES_TOOL, args, error)
    return Intent(None)


def polite_unknown(ctx: ChatContext, text: str) -> Reply:
    """Nothing understood: say so, politely, and show what can be asked. Never echoes ``text``."""
    return Reply(r.UNKNOWN, (r.say(UNKNOWN_TEXT), *help_lines(ctx.user)))


def route_message(ctx: ChatContext, text: str, unknown: UnknownHandler = polite_unknown) -> Reply:
    """Answer one message with the rights of ``ctx.user``. A message that names a request is run
    through the registry; one that names nothing goes to ``unknown`` (part 2: the AI)."""
    intent = parse_message(text, ctx.today)
    if intent.error:
        return r.refuse(r.INVALID, r.say(intent.error), tool=intent.tool)
    if intent.tool is None:
        return unknown(ctx, text)
    return run_tool(ctx, intent.tool, intent.args)
