"""Reader for an Amadeus PNR display pasted as text (docs/design/10-ticket-screens.md).

``parse_pnr_text`` is pure and never raises on bad input: what cannot be read becomes a short
French warning (never echoing names or phone numbers). The result is only a *reading*: the agent
confirms every value in the entry form's read-back (phase B5/B6).

Rules:

* Limits: at most ``MAX_TEXT_BYTES`` bytes and ``MAX_LINES`` lines, else nothing is read. Every
  line goes through ``clean_text`` (control and bidi characters removed); the indentation is
  measured first.
* Screens: each pasted screen starts with an ``RP/...`` line. Screens of the first PNR are merged,
  elements deduplicated by their number (first wins); screens of another PNR are ignored (warning).
* Elements start with a number in the first three columns (`` 9 FA ...``). An indented line (three
  spaces or more) continues the element above and is joined **without** a space, because Amadeus
  breaks lines at a fixed width, even in the middle of a number; so does a line indented three
  spaces or more that looks like an element number. With ``transcript=True`` (text a model read off
  an image, whose indentation may be lost) any other line (not an element number, ``RP/``, header
  or prompt) continues the element too, unless it is prose (a lowercase word of three letters or
  more). Markdown code fences end the element. The ``*`` line and its
  continuation are consumed and ignored. Prompt lines (``>``, ``)>``, ``>|``) end an element.
* Passengers: ``1.SURNAME/FIRST(CHD)`` and ``(INFSURNAME/FIRST)``. An infant is a separate
  ``INF`` passenger placed right after its adult; its ``number`` is the **adult's number** (that is
  what ``FA INF ... /P1`` refers to) and ``adult_number`` links it.
* RM prices: see ``parse_rm`` (per passenger type, ambiguous means ``None`` and a warning).
* Money is ``Decimal`` read with a strict regex; a currency other than MRU is a warning.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal, NamedTuple

from app.parsing.amadeus import clean_text

MAX_TEXT_BYTES = 16 * 1024
MAX_LINES = 400
MAX_HINT_CHARS = 100
MAX_NAME_CHARS = 100
MAX_RM_AMOUNT = Decimal(10) ** 9
MAX_COST = Decimal(10) ** 8  # a ticket or a non-refundable amount above this is read as garbage
LOW_RM_PRICE = Decimal(1000)  # below this an adult/child RM price is probably a typo
MAX_BODY_CHARS = 300  # an AP/FP/FA/FE element is never longer; the rest is cut
MAX_RM_CHARS = 3000
SUSPECT_LEAD = "=+-@\t\r"  # a spreadsheet would read such a name as a formula

PaxType = Literal["ADT", "CHD", "INF"]
CENT = Decimal("0.01")

W_NO_TEXT = "Aucun texte à lire."
W_TOO_LONG = "Texte trop long (16 Ko et 400 lignes au plus) : rien n'a été lu."
W_NO_RP = "Ligne RP introuvable : collez l'écran complet de la réservation."
W_SEVERAL = "plusieurs réservations collées : seule la première est lue."
W_RM = "Ligne RM non comprise : saisissez le prix à la main."
W_RM_LOW = "prix RM inhabituellement bas : vérifier"
W_SUSPECT = "caractère suspect retiré"
W_RM_INFANT = "Prix bébé absent de la ligne RM : saisissez-le à la main."

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}

_ELEMENT_RE = re.compile(r"(\d{1,3}) +(\S.*)", re.ASCII)
_LOWER_WORD_RE = re.compile(r"[a-z]{3,}", re.ASCII)
_PAX_LINE_RE = re.compile(r"\d{1,2}\.\S", re.ASCII)
_PAX_SPLIT_RE = re.compile(r"(?:^|\s)(\d{1,2})\.(?=\S)", re.ASCII)
_PAX_CHD_RE = re.compile(r"\((?:CHD|C\d{1,2})\b")
_PAX_INF_RE = re.compile(r"\(INF([^)]*)\)")
_DATE_RE = re.compile(r"(\d{2})([A-Z]{3})(\d{2})", re.ASCII)
_RP_SIGN_RE = re.compile(r"[A-Z0-9]{2}/[A-Z0-9]{2,3}", re.ASCII)
_RP_DATE_RE = re.compile(r"(\d{2}[A-Z]{3}\d{2})/\d{4}Z", re.ASCII)
_RP_RLOC_RE = re.compile(r"[A-Z0-9]{5,8}", re.ASCII)
_FA_RE = re.compile(
    r"FA\s+(?:(PAX|INF|CHD)\s+)?(\d{3})-(\d{10})/(ET|DT)([A-Z0-9]{2})/([A-Z]{3})([^/]*)"
    r"/(\d{2}[A-Z]{3}\d{2})/([A-Z0-9]+)/(\d{8})((?:/[^/]*)*)",
    re.ASCII,
)
_SEGMENTS_RE = re.compile(r"S[0-9,\-]+", re.ASCII)
_PASSENGER_RE = re.compile(r"P(\d{1,2})", re.ASCII)
_AMOUNT_RE = re.compile(r"\d{1,12}(?:\.\d{1,2})?", re.ASCII)
_PASSENGER_RANGE_RE = re.compile(r"P\d{1,2}[-,].*", re.ASCII)
_BIRTH_DATE_RE = re.compile(r"/\d{2}[A-Z]{3}\d{2}$", re.ASCII)
_FE_NONREF_RE = re.compile(r"([A-Z]{3})([^/\s]*)\s+NONREF", re.ASCII)
_FE_PREFIX_RE = re.compile(r"FE\s+(?:(?:PAX|INF|CHD)\s+)?", re.ASCII)
_FE_SUFFIX_RE = re.compile(r"(?:S[0-9,\-]+|P\d{1,2})", re.ASCII)
_FO_RE = re.compile(r"(\d{3})-(\d{10})", re.ASCII)
_AP_CITY_RE = re.compile(r"[A-Z]{3}\s+(?=[+\d])", re.ASCII)
_AP_PHONE_RE = re.compile(r"\+?[0-9][0-9 /\-]*", re.ASCII)
_FP_RE = re.compile(r"NONREF\s*(.*)", re.IGNORECASE)
# "51 300" and "5 400" only: a lead group of 3 digits ("900 450") is two prices.
_RM_THOUSANDS_RE = re.compile(r"(?<![\d.])(\d{1,2}) (\d{3})(?![\d.])", re.ASCII)
_RM_ADJACENT_RE = re.compile(r"\d\s+\d", re.ASCII)
_RM_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[A-Z]+|[^\sA-Z0-9]", re.ASCII)
_RM_NUMBER_RE = re.compile(r"\d+(?:\.\d{1,2})?", re.ASCII)

# MRU, and UM (shorthand for the ouguiya). Not MRO: the old ouguiya is worth 10 times less.
_RM_CURRENCIES = frozenset({"MRU", "UM"})
_RM_LABELS = {
    "ADT": "ADT",
    "ADLT": "ADT",
    "ADULT": "ADT",
    "ADULTE": "ADT",
    "ADULTES": "ADT",
    "AD": "ADT",
    "CHD": "CHD",
    "CH": "CHD",
    "CHILD": "CHD",
    "ENF": "CHD",
    "ENFANT": "CHD",
    "INF": "INF",
    "INFANT": "INF",
    "BB": "INF",
    "BEBE": "INF",
}


# --- result -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Passenger:
    """``number`` is the PNR number; an infant carries its adult's number (``adult_number``)."""

    number: int
    name: str  # SURNAME/FIRST
    pax_type: PaxType
    adult_number: int | None


@dataclass(frozen=True)
class TicketLine:
    doc_number: str  # 10 digits
    airline: str  # 3-digit prefix, e.g. 495
    carrier: str  # 2 characters, e.g. L6
    emd: bool
    cost: Decimal | None  # None when the amount or the currency (not MRU) could not be read
    issue_date: date | None
    issuing_office: str
    iata: str
    segments: str
    passenger: int | None  # the P number; for an infant ticket, the adult's number
    is_infant: bool


@dataclass(frozen=True)
class RmPrices:
    """Prices of an RM line. ``default`` is a price with no type (it covers adults and children)."""

    adt: Decimal | None
    chd: Decimal | None
    inf: Decimal | None
    default: Decimal | None

    def for_type(self, pax_type: str) -> Decimal | None:
        """ADT: its price, else the default. CHD: its price, else the default (the single price
        with no type), never the labelled adult price. INF: its own price only."""
        if pax_type == "ADT":
            return self.adt if self.adt is not None else self.default
        if pax_type == "CHD":
            return self.chd if self.chd is not None else self.default
        if pax_type == "INF":
            return self.inf
        return None


@dataclass(frozen=True)
class PnrReading:
    rloc: str | None
    office: str | None
    agent_sign: str | None
    created: date | None
    passengers: tuple[Passenger, ...]
    tickets: tuple[TicketLine, ...]
    rm_raw: str  # the RM lines (without "RM "), joined with newlines
    rm_prices: RmPrices | None
    ap_agency: str | None
    ap_phones: tuple[str, ...]
    fp_hint: str | None  # a hint only
    exchanges: tuple[str, ...]  # 10-digit numbers of the original tickets (FO)
    nonrefundable_amounts: tuple[Decimal, ...]  # FE ``MRUxxxx.xx NONREF``: information only
    endorsements: tuple[str, ...]  # the other FE texts (restrictions printed on the ticket)
    warnings: tuple[str, ...]


def _empty(warning: str) -> PnrReading:
    return PnrReading(
        None, None, None, None, (), (), "", None, None, (), None, (), (), (), (warning,)
    )


# --- screens ------------------------------------------------------------------------------------


@dataclass
class _Screen:
    rp: str | None = None
    elements: dict[int, list[str]] = field(default_factory=dict)
    pax: dict[int, str] = field(default_factory=dict)


def strip_accents(value: str) -> str:
    """``value`` with accents removed (NFKD, combining marks dropped): e-acute becomes e."""
    return "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def _indent_and_text(line: str) -> tuple[int, str]:
    cleaned = clean_text("|" + line)[1:]  # the sentinel keeps clean_text from eating the indent
    text = cleaned.lstrip(" ")
    return len(cleaned) - len(text), text


def _split_screens(lines: list[str], *, transcript: bool = False) -> list[_Screen]:
    screens = [_Screen()]
    current: list[str] | None = None
    separator = ""
    for raw in lines:
        indent, text = _indent_and_text(raw)
        screen = screens[-1]
        if not text or text.startswith((">", ")>", "---", "```", "~~~")):
            current = None  # blank, prompt, header or Markdown fence: ends the element
        elif text.startswith("RP/") and indent < 3:
            screens.append(_Screen(rp=text))
            current = None
        elif indent <= 2 and _PAX_LINE_RE.match(text):
            for number, chunk in _pax_chunks(text):
                screen.pax.setdefault(number, chunk)
            current = []  # a wrapped name is not supported; swallow its continuation
        elif indent <= 2 and (m := _ELEMENT_RE.fullmatch(text)):
            number = int(m.group(1))
            if number in screen.elements:
                current = []
            else:
                current = [m.group(2)]
                screen.elements[number] = current
            separator = ""
        elif indent <= 2 and text.startswith("*"):
            current = []
            separator = " "
        elif current is not None and (indent >= 3 or (transcript and not _is_prose(text))):
            # Indented as Amadeus wraps (3 or more), a line always continues the element above,
            # lowercase e-mails and remarks included. Only in a transcript (the indentation may be
            # lost) does a line at column 0-2 continue it too, unless it is prose.
            current.append(separator + text)
        else:
            current = None
    return screens


def _is_prose(text: str) -> bool:
    """A line of ordinary sentences (a model's "Here is the text:"), not terminal output: Amadeus
    prints capitals, so a lowercase word of three letters or more is not part of a screen."""
    return _LOWER_WORD_RE.search(text) is not None


def _pax_chunks(text: str) -> list[tuple[int, str]]:
    found = list(_PAX_SPLIT_RE.finditer(text))
    chunks = []
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        chunks.append((int(m.group(1)), text[m.end() : end].strip()))
    return chunks


# --- small parsers ------------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    m = _DATE_RE.fullmatch(value)
    month = _MONTHS.get(m.group(2)) if m else None
    if m is None or month is None:
        return None
    try:
        return date(2000 + int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None


def _parse_cost(value: str) -> Decimal | None:
    if not _AMOUNT_RE.fullmatch(value):
        return None
    amount = Decimal(value)
    return amount.quantize(CENT) if amount <= MAX_COST else None


def _collapse(value: str) -> str:
    return " ".join(value.split())


class _Rp(NamedTuple):
    office: str | None
    sign: str | None
    created: date | None
    rloc: str | None


def _parse_rp(rp: str) -> _Rp:
    parts = rp.split()
    head = parts[0].split("/")
    office = head[1] if len(head) > 1 and head[1] else None
    sign = created = rloc = None
    for token in parts[1:]:
        if sign is None and _RP_SIGN_RE.fullmatch(token):
            sign = token.split("/")[0]
        elif created is None and (m := _RP_DATE_RE.fullmatch(token)):
            created = _parse_date(m.group(1))
    if len(parts) > 1 and _RP_RLOC_RE.fullmatch(parts[-1]):
        rloc = parts[-1]
    return _Rp(office, sign, created, rloc)


def _safe(value: str, warnings: list[str]) -> str:
    """``value`` without leading formula characters (``= + - @``); a warning if any was cut."""
    clean = value.lstrip(SUSPECT_LEAD + " ")
    if clean != value:
        warnings.append(W_SUSPECT)
    return clean


def _parse_passengers(pax: dict[int, str], warnings: list[str]) -> tuple[Passenger, ...]:
    out: list[Passenger] = []
    for number in sorted(pax):
        chunk = pax[number]
        name = _safe(_collapse(chunk.split("(", 1)[0]), warnings)[:MAX_NAME_CHARS]
        if not name:
            warnings.append("Un passager n'a pas de nom lisible.")
            continue
        pax_type: PaxType = "CHD" if _PAX_CHD_RE.search(chunk) else "ADT"
        out.append(Passenger(number, name, pax_type, None))
        for inf in _PAX_INF_RE.finditer(chunk):
            raw = _BIRTH_DATE_RE.sub("", _collapse(inf.group(1)))
            inf_name = _safe(raw, warnings)[:MAX_NAME_CHARS] or name.split("/")[0] + "/INF"
            out.append(Passenger(number, inf_name, "INF", number))
    return tuple(out)


def _parse_fa(
    text: str, passengers: tuple[Passenger, ...], warnings: list[str]
) -> TicketLine | None:
    m = _FA_RE.fullmatch(text)
    if m is None:
        warnings.append("Une ligne FA (billet) n'a pas pu être lue.")
        return None
    kind, prefix, doc, et, carrier, currency, amount, issued, office, iata, rest = m.groups()
    cost: Decimal | None = None
    if currency != "MRU":
        warnings.append(f"Billet {doc} : devise {currency} au lieu de MRU, coût à saisir.")
    else:
        cost = _parse_cost(amount)
        if cost is None:
            warnings.append(f"Billet {doc} : montant illisible, coût à saisir.")
    segments = ""
    passenger: int | None = None
    several = False
    for part in rest.split("/")[1:]:
        if _SEGMENTS_RE.fullmatch(part):
            segments = part
        elif p := _PASSENGER_RE.fullmatch(part):
            passenger = int(p.group(1))
        elif _PASSENGER_RANGE_RE.fullmatch(part):
            several = True
    is_infant = kind == "INF"
    if several:  # P1-2: more than one passenger, never guessed
        passenger = None
        warnings.append(f"Billet {doc} : plusieurs passagers : à vérifier.")
    elif passenger is None:
        candidates = {p.number for p in passengers if (p.pax_type == "INF") == is_infant}
        if len(candidates) == 1:
            passenger = candidates.pop()
        else:
            warnings.append(f"Billet {doc} : passager non identifié.")
    return TicketLine(
        doc_number=doc,
        airline=prefix,
        carrier=carrier,
        emd=et == "DT",
        cost=cost,
        issue_date=_parse_date(issued),
        issuing_office=office,
        iata=iata,
        segments=segments,
        passenger=passenger,
        is_infant=is_infant,
    )


def parse_rm(raw: str) -> RmPrices | None:
    """The prices of the RM text, or ``None`` when it is empty, ambiguous or not understood.

    Forms: ``5400 MRU`` (one price, no type); numbers followed by their label (``5400 MRU ADLT
    900 MRU BB``) or labels followed by their number (``ADT = 5400 , CHD = 4400``). At most one
    number may have no label; a type may appear once. A space inside a number is thousands only
    after one or two digits (``51 300``, ``5 400``); two numbers side by side are ambiguous. A
    comma is a separator. Amounts are above 0 and at most ``MAX_RM_AMOUNT``.
    """
    text = strip_accents(raw).upper()
    text = _RM_THOUSANDS_RE.sub(r"\1\2", text)
    if _RM_ADJACENT_RE.search(text):  # two numbers side by side: thousands or two prices?
        return None
    text = re.sub(r"[=:,;/]", " ", text)
    tokens = [t for t in _RM_TOKEN_RE.findall(text) if t not in _RM_CURRENCIES]
    if not tokens:
        return None
    items: list[tuple[str | None, Decimal]] = []
    i = 0
    if tokens[0][0].isdigit():
        while i < len(tokens):
            amount = _rm_amount(tokens[i])
            if amount is None:
                return None
            i += 1
            label = None
            if i < len(tokens) and tokens[i] in _RM_LABELS:
                label = _RM_LABELS[tokens[i]]
                i += 1
            items.append((label, amount))
    else:
        while i < len(tokens):
            if tokens[i] not in _RM_LABELS or i + 1 >= len(tokens):
                return None
            amount = _rm_amount(tokens[i + 1])
            if amount is None:
                return None
            items.append((_RM_LABELS[tokens[i]], amount))
            i += 2
    by_label: dict[str | None, Decimal] = {}
    for label, amount in items:
        if label in by_label:
            return None
        by_label[label] = amount
    return RmPrices(
        adt=by_label.get("ADT"),
        chd=by_label.get("CHD"),
        inf=by_label.get("INF"),
        default=by_label.get(None),
    )


def _rm_amount(token: str) -> Decimal | None:
    if not _RM_NUMBER_RE.fullmatch(token):
        return None
    amount = Decimal(token)
    return amount if 0 < amount <= MAX_RM_AMOUNT else None


def _agency_of(text: str) -> str | None:
    """The name in ``... - NAME - A``, found from the tail (linear, no backtracking)."""
    head = text.rstrip()
    if not head.endswith("A"):
        return None
    head = head[:-1].rstrip()
    if len(head) < 2 or not head.endswith("-") or not head[-2].isspace():
        return None
    head = head[:-1].rstrip()
    start = head.find(" - ")
    name = head[start + 3 :].strip() if start >= 0 else ""
    return name if any(c.isalnum() for c in name) else None


def _parse_fe(body: str, nonrefundable: list[Decimal], endorsements: list[str]) -> None:
    """FE is the endorsement element: restrictions printed on the ticket. ``MRU8245.00 NONREF``
    says 8 245 MRU of the ticket cannot be refunded (information, never a price)."""
    text = _FE_PREFIX_RE.sub("", body, count=1)
    parts = text.split("/")
    while len(parts) > 1 and _FE_SUFFIX_RE.fullmatch(parts[-1]):
        parts.pop()
    text = "/".join(parts).strip()
    if not text:
        return
    if m := _FE_NONREF_RE.fullmatch(text):
        amount = _parse_cost(m.group(2)) if m.group(1) == "MRU" else None
        if amount is not None and amount not in nonrefundable:
            nonrefundable.append(amount)
    elif text[:MAX_HINT_CHARS] not in endorsements:
        endorsements.append(text[:MAX_HINT_CHARS])


def _parse_ap(text: str) -> tuple[str | None, str | None]:
    """(agency name, phone) of an AP line; at most one of them is set."""
    if name := _agency_of(text):
        return name[:MAX_HINT_CHARS], None
    body = text[2:].strip()
    body = _AP_CITY_RE.sub("", body, count=1) if _AP_CITY_RE.match(body) else body
    if _AP_PHONE_RE.fullmatch(body) and sum(c.isdigit() for c in body) >= 6:
        return None, _collapse(body)
    return None, None


# --- the reader ---------------------------------------------------------------------------------


def parse_pnr_text(text: str, *, transcript: bool = False) -> PnrReading:
    """Read pasted PNR text. Never raises; problems are warnings in the result.

    ``transcript`` is True for text a model read off an image, whose indentation may be lost: a
    line that is not an element, header or prompt then continues the element above whatever its
    indentation. For typed or pasted text it is False and only an indented line continues."""
    if not isinstance(text, str):
        return _empty(W_NO_TEXT)
    if len(text) > MAX_TEXT_BYTES or len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return _empty(W_TOO_LONG)
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        return _empty(W_TOO_LONG)
    if not text.strip():
        return _empty(W_NO_TEXT)

    warnings: list[str] = []
    screens = _split_screens(lines, transcript=transcript)
    with_rp = [s for s in screens if s.rp]
    office = sign = rloc = None
    created: date | None = None
    if with_rp:
        office, sign, created, rloc = _parse_rp(with_rp[0].rp or "")
        chosen = [with_rp[0]]
        for s in with_rp[1:]:
            other = _parse_rp(s.rp or "").rloc
            if rloc is not None and other is not None and other != rloc:
                if W_SEVERAL not in warnings:
                    warnings.append(W_SEVERAL)
            else:
                chosen.append(s)
    else:
        chosen = [screens[0]]
    if rloc is None:
        warnings.append(W_NO_RP)

    elements: dict[int, str] = {}
    pax: dict[int, str] = {}
    for s in chosen:
        for number, parts in s.elements.items():
            joined = "".join(parts)
            if number not in elements:
                elements[number] = joined
            elif _collapse(elements[number]) != _collapse(joined):
                warnings.append(f"élément {number} différent entre deux écrans : vérifier")
        for number, chunk in s.pax.items():
            if number not in pax:
                pax[number] = chunk
            elif _collapse(pax[number]) != _collapse(chunk):
                warnings.append(f"passager {number} différent entre deux écrans : vérifier")

    passengers = _parse_passengers(pax, warnings)
    tickets: list[TicketLine] = []
    rm_lines: list[str] = []
    ap_agency: str | None = None
    ap_phones: list[str] = []
    fp_hint: str | None = None
    exchanges: list[str] = []
    nonrefundable: list[Decimal] = []
    endorsements: list[str] = []
    seen_docs: set[str] = set()
    for number in sorted(elements):
        body = _collapse(elements[number])
        code = body.split(" ", 1)[0]
        body = body[: MAX_RM_CHARS if code == "RM" else MAX_BODY_CHARS]
        if code == "FA":
            ticket = _parse_fa(body, passengers, warnings)
            if ticket and ticket.doc_number not in seen_docs:
                seen_docs.add(ticket.doc_number)
                tickets.append(ticket)
        elif code == "RM":
            if rest := body[2:].strip():
                rm_lines.append(rest)
        elif code == "AP":
            agency, phone = _parse_ap(body)
            if agency and ap_agency is None:
                ap_agency = _safe(agency, warnings) or None
            elif phone:
                ap_phones.append(phone)
        elif code == "FP":
            if fp_hint is None and (m := _FP_RE.search(body)):
                fp_hint = _safe(_collapse(m.group(1)), warnings)[:MAX_HINT_CHARS] or None
        elif code == "FE":
            _parse_fe(body, nonrefundable, endorsements)
        elif code == "FO" and (m := _FO_RE.search(body)) and m.group(2) not in exchanges:
            exchanges.append(m.group(2))

    rm_raw = "\n".join(rm_lines)
    rm_prices = parse_rm(" ".join(rm_lines)) if rm_lines else None
    if rm_lines and rm_prices is None:
        warnings.append(W_RM)
    if rm_prices is not None and any(
        p is not None and p < LOW_RM_PRICE
        for p in (rm_prices.adt, rm_prices.chd, rm_prices.default)
    ):
        warnings.append(W_RM_LOW)
    if (
        rm_prices is not None
        and rm_prices.inf is None
        and any(p.pax_type == "INF" for p in passengers)
    ):
        warnings.append(W_RM_INFANT)

    return PnrReading(
        rloc=rloc,
        office=office,
        agent_sign=sign,
        created=created,
        passengers=passengers,
        tickets=tuple(tickets),
        rm_raw=rm_raw,
        rm_prices=rm_prices,
        ap_agency=ap_agency,
        ap_phones=tuple(ap_phones),
        fp_hint=fp_hint,
        exchanges=tuple(exchanges),
        nonrefundable_amounts=tuple(nonrefundable),
        endorsements=tuple(endorsements),
        warnings=tuple(dict.fromkeys(warnings)),
    )
