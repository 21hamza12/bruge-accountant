"""When the cheap transcript of one image is not good enough and the stronger model reads it again.

``fallback_reason`` looks at ONE image's transcript and the reading of it (``parse_pnr_text``) and
returns the first of these reasons, or ``None`` when the read is fine. Every page of an Amadeus
booking starts with an ``RP/`` line, so each image is judged alone:

``truncated``
    the answer was cut at the output limit (``Transcription.truncated``): the end of the screen,
    often the tickets, is missing even if what is there reads well.
``no_rp``
    the reader found no RP line (no booking reference): the header was lost or the model did not
    transcribe a screen at all.
``no_ticket``
    the screen shows ticketing elements (``FA``, ``FB``, ``FV``, ``FP``, ``FE`` or ``FO`` lines)
    but not one FA ticket line was read: the tickets were lost. A first page that only has the
    names, segments and contacts (a layover booking) has none of those elements and is NOT a
    reason: its tickets are on the next page.
``no_cost``
    a ticket was read without its amount (unreadable), and not because the currency is not MRU
    (that is the screen's truth, a stronger model would read the same).
``garbled``
    the reader says an FA line could not be read; or the model wrote ``?`` (the prompt's mark for
    a character it could not read) in a line that carries money or ticket data (``FA``, ``FB``,
    ``FV``, ``FP``, ``FE``, ``FO``, ``TK``) or next to a digit. A ``?`` in a name or a free text
    is not a reason: a stronger model cannot change what the agent corrects while reading back.

Cheap and deterministic, and no money question is ever decided by it: the agent reads everything
back before confirming, the fallback only improves what is proposed.
"""

from __future__ import annotations

import re

from app.parsing.pnr import PnrReading

TRUNCATED = "truncated"
NO_RP = "no_rp"
NO_TICKET = "no_ticket"
NO_COST = "no_cost"
GARBLED = "garbled"

_TICKETING_ELEMENT_RE = re.compile(
    r"^\s*\d{1,3}\s+(?:FA|FB|FV|FP|FE|FO)\b", re.MULTILINE | re.ASCII
)
_MARKED_LINE_RE = re.compile(
    r"^\s*\d{1,3}\s+(?:FA|FB|FV|FP|FE|FO|TK)\b[^\n]*\?|\d\?|\?\d", re.MULTILINE | re.ASCII
)
_FA_UNREADABLE = "Une ligne FA (billet) n'a pas pu être lue"
_CURRENCY_WARNING = "devise"


def fallback_reason(text: str, reading: PnrReading, *, truncated: bool = False) -> str | None:
    if truncated:
        return TRUNCATED
    if reading.rloc is None:
        return NO_RP
    if not reading.tickets and _TICKETING_ELEMENT_RE.search(text):
        return NO_TICKET
    warnings = reading.warnings
    # a currency other than MRU is a warning of its own on that very ticket: a second read would
    # say the same. Any other ticket without a cost is unreadable.
    unexplained = [
        t
        for t in reading.tickets
        if t.cost is None
        and not any(w.startswith(f"Billet {t.doc_number} : {_CURRENCY_WARNING}") for w in warnings)
    ]
    if unexplained:
        return NO_COST
    if any(w.startswith(_FA_UNREADABLE) for w in warnings) or _MARKED_LINE_RE.search(text):
        return GARBLED
    return None
