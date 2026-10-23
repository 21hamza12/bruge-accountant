"""The comparison of the app report with the Amadeus report (B8 core). Pure: no database.

Lines are matched on DOC NUMBER. For every pair the compared fields are A/L, TOTAL DOC, PAX NAME,
pax type, AS, RLOC and TRNC (docs/design/05-daily-comparison.md section 3). The result lists what
matched, the flags, the cancellations accepted automatically, the tickets that moved from an
earlier day and the partner tickets that are not in this office's report.

Rules, all deliberate:

* **Nothing that should block is ever only a warning.** An unusable or repeated ticket number, a
  ticket that belongs to a sale of another day, a missing line: all are blocking flags.
  ``warnings`` holds informational notes only (today: an unknown sale kind, treated as direct).
* DOC NUMBER: ASCII digits only. A number is usable when it has 10 digits, or 13 digits whose
  first three are the ticket's own airline prefix (``495-3940281465`` equals ``3940281465``).
  Anything else (no digits, wrong length, full-width digits) is a ``BAD_NUMBER`` flag. A number
  that appears twice on one side is a ``BAD_NUMBER`` flag too; the first of the duplicates in a
  total order is still compared, so a result never depends on the order of the inputs.
* PAX NAME: case-insensitive; all whitespace (including no-break spaces) collapsed; spaces around
  ``/`` ignored. There is no truncation tolerance (the Excel export has full names) and no accent
  folding: ``ELEA`` and ``ÉLÉA`` differ. A name difference needs the agent only.
* AS: full sign, case-insensitive, stripped (``0004SV`` against ``0004SV``; ``SV`` differs).
* TOTAL DOC: exact Decimal equality, to the cent. This is stricter than doc 05's "to the unit" on
  purpose. An Amadeus cost that is missing (``None``) is a difference.
* A/L, TRNC, pax type, RLOC: stripped, case-insensitive.
* TRNC ``CANX`` only in Amadeus: accepted, listed in ``cancelled``. ``CANX`` on both sides is a
  match (the TJQ APRES export, B10, removes cancelled lines). An app ticket that is ``CANX`` with
  no Amadeus line is ``NOT_IN_AMADEUS`` (blocking). Any other TRNC (known or not) that is only in
  Amadeus is ``NOT_SENT``.
* Tickets of **partner** sales missing from the report go to ``not_in_report_partner`` and do not
  block, because they are issued in another office (docs/design/10-ticket-screens.md section 7).
  **B9 must require the accountant to acknowledge that list before closing the day.** A partner
  ticket that is in the report and differs still blocks.
* ``other_days_app`` holds app tickets found outside the day (``active_ticket_lookup``). **The
  caller must leave out tickets of closed days.** A report line whose ticket is there with an
  earlier day is a ``Move`` (and is compared as usual); with the same or a later day it is an
  ``ELSEWHERE`` flag (re-sending it would be refused as a duplicate).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from app.models import AG_DIRECT, AG_PARTNER, AmadeusLine
from app.sales_rules import TicketRow

__all__ = [
    "KIND_BAD_NUMBER",
    "KIND_DIFFERENT",
    "KIND_ELSEWHERE",
    "KIND_NOT_IN_AMADEUS",
    "KIND_NOT_SENT",
    "KNOWN_KINDS",
    "AmadeusTicket",
    "AppTicket",
    "ComparisonResult",
    "Flag",
    "Move",
    "amadeus_ticket_from_line",
    "app_ticket_from_row",
    "compare",
    "normalise_doc",
]

KIND_DIFFERENT: Final = "DIFFERENT"
KIND_NOT_SENT: Final = "NOT_SENT"
KIND_NOT_IN_AMADEUS: Final = "NOT_IN_AMADEUS"
KIND_BAD_NUMBER: Final = "BAD_NUMBER"
KIND_ELSEWHERE: Final = "ELSEWHERE"
KNOWN_KINDS: Final = frozenset(
    {KIND_DIFFERENT, KIND_NOT_SENT, KIND_NOT_IN_AMADEUS, KIND_BAD_NUMBER, KIND_ELSEWHERE}
)

TRNC_CANX: Final = "CANX"
DOC_DIGITS: Final = 10
# Fields whose correction moves money, the ticket itself, its attribution or its fare class: the
# accountant or manager validates (decision 18, widened after the B9 review: the sign decides whose
# sale it is and the passenger type decides the fare). A name, PNR or airline fix needs the agent
# only.
NEEDS_VALIDATION_FIELDS: Final = frozenset(
    {"total_doc", "trnc", "doc_number", "pax_type", "staff_sign"}
)

_NO_VALUE = ""


@dataclass(frozen=True)
class AppTicket:
    """A ticket of an active sale, as the app holds it."""

    doc_number: str
    airline: str
    total_doc: Decimal
    pax_name: str
    pax_type: str
    rloc: str
    trnc: str
    staff_sign: str
    sale_id: int
    ag_kind: str  # AG_DIRECT | AG_PARTNER
    report_day: date


@dataclass(frozen=True)
class AmadeusTicket:
    """A line of the Amadeus report."""

    doc_number: str
    airline: str
    total_doc: Decimal | None
    pax_name: str
    pax_type: str
    rloc: str
    trnc: str
    staff_sign: str  # as in the report, e.g. 0004SV
    seq_order: int


@dataclass(frozen=True)
class Flag:
    """A thing to settle. ``key`` is stable across re-runs so decisions can be kept when the
    values did not change. Values are display strings (``""`` = absent)."""

    kind: str
    doc_number: str  # normalised; the raw text for an unusable number
    field: str  # "" except for DIFFERENT and BAD_NUMBER
    app_value: str
    amadeus_value: str
    responsible_sign: str  # the app ticket's sign if there is one, else the Amadeus sign
    needs_validation: bool
    blocking: bool
    sale_id: int | None = None
    amadeus_seq: int | None = None
    amadeus_sign: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.doc_number, self.kind, self.field)


@dataclass(frozen=True)
class Move:
    """An Amadeus line of ``to_day`` that belongs to a sale of an earlier day."""

    doc_number: str
    sale_id: int
    from_day: date
    to_day: date
    amadeus_seq: int | None = None


@dataclass(frozen=True)
class ComparisonResult:
    matched: tuple[str, ...]  # doc numbers equal on every compared field, in Amadeus order
    flags: tuple[Flag, ...]
    cancelled: tuple[AmadeusTicket, ...]
    moves: tuple[Move, ...]
    not_in_report_partner: tuple[AppTicket, ...]
    warnings: tuple[str, ...] = ()
    matched_tickets: tuple[AmadeusTicket, ...] = ()  # the Amadeus lines of ``matched``

    @property
    def blocking_flags(self) -> tuple[Flag, ...]:
        return tuple(f for f in self.flags if f.blocking)


# --- builders ---------------------------------------------------------------------------------


def app_ticket_from_row(row: TicketRow, *, ag_kind: str, report_day: date) -> AppTicket:
    """An ``AppTicket`` from B4's ``TicketRow`` (it carries neither the sale kind nor the day)."""
    return AppTicket(
        doc_number=row.doc_number,
        airline=row.airline,
        total_doc=row.total_doc,
        pax_name=row.pax_name,
        pax_type=row.pax_type,
        rloc=row.rloc,
        trnc=row.trnc,
        staff_sign=row.staff_sign,
        sale_id=row.sale_id,
        ag_kind=ag_kind,
        report_day=report_day,
    )


def amadeus_ticket_from_line(line: AmadeusLine) -> AmadeusTicket:
    """An ``AmadeusTicket`` from a stored line of the report."""
    return AmadeusTicket(
        doc_number=line.doc_number,
        airline=line.airline,
        total_doc=line.total_doc,
        pax_name=line.pax_name,
        pax_type=line.pax_type,
        rloc=line.rloc,
        trnc=line.trnc,
        staff_sign=line.staff_sign,
        seq_order=line.seq_order,
    )


# --- normalisation ----------------------------------------------------------------------------

_ASCII_DIGITS = frozenset("0123456789")
_AROUND_SLASH = re.compile(r"\s*/\s*")


def normalise_doc(raw: str, airline: str = "") -> str:
    """The 10-digit ticket number of ``raw``, or ``""`` when it is unusable.

    ASCII digits only (any other digit, e.g. full-width, makes it unusable). Thirteen digits whose
    first three equal ``airline`` lose that prefix.
    """
    if any(ch.isdigit() and ch not in _ASCII_DIGITS for ch in raw):
        return ""
    digits = "".join(ch for ch in raw if ch in _ASCII_DIGITS)
    if len(digits) == DOC_DIGITS + 3 and airline.strip() and digits[:3] == airline.strip():
        digits = digits[3:]
    return digits if len(digits) == DOC_DIGITS else ""


def _name_key(raw: str) -> str:
    return _AROUND_SLASH.sub("/", " ".join(raw.split())).casefold()


def _money(value: Decimal | None) -> str:
    if value is None:
        return _NO_VALUE
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _ci(a: str, b: str) -> bool:
    return a.strip().upper() == b.strip().upper()


def _differences(app: AppTicket, amd: AmadeusTicket) -> list[tuple[str, str, str]]:
    """``(field, app display, amadeus display)`` for each compared field that differs."""
    checks: list[tuple[str, bool, str, str]] = [
        ("airline", _ci(app.airline, amd.airline), app.airline, amd.airline),
        (
            "total_doc",
            amd.total_doc is not None and amd.total_doc == app.total_doc,
            _money(app.total_doc),
            _money(amd.total_doc),
        ),
        (
            "pax_name",
            _name_key(app.pax_name) == _name_key(amd.pax_name),
            app.pax_name,
            amd.pax_name,
        ),
        ("pax_type", _ci(app.pax_type, amd.pax_type), app.pax_type, amd.pax_type),
        ("staff_sign", _ci(app.staff_sign, amd.staff_sign), app.staff_sign, amd.staff_sign),
        ("rloc", _ci(app.rloc, amd.rloc), app.rloc, amd.rloc),
        ("trnc", _ci(app.trnc, amd.trnc), app.trnc, amd.trnc),
    ]
    return [(name, a, b) for name, equal, a, b in checks if not equal]


# --- the comparison ---------------------------------------------------------------------------


def _app_order(t: AppTicket) -> tuple[object, ...]:
    return (
        t.sale_id,
        t.doc_number,
        t.airline,
        str(t.total_doc),
        t.pax_name,
        t.pax_type,
        t.rloc,
        t.trnc,
        t.staff_sign,
        t.ag_kind,
        t.report_day.toordinal(),
    )


def _amd_order(t: AmadeusTicket) -> tuple[object, ...]:
    return (
        t.seq_order,
        t.doc_number,
        t.airline,
        str(t.total_doc),
        t.pax_name,
        t.pax_type,
        t.rloc,
        t.trnc,
        t.staff_sign,
    )


@dataclass
class _Bad:
    """What is wrong with one ticket number, gathered from both sides into a single flag."""

    app: AppTicket | None = None
    amd: AmadeusTicket | None = None
    app_note: str = _NO_VALUE
    amd_note: str = _NO_VALUE


def _index[T](
    items: Sequence[T],
    order: Callable[[T], tuple[object, ...]],
    doc_of: Callable[[T], str],
    airline_of: Callable[[T], str],
    note: Callable[[_Bad, T, str], None],
    bad: dict[str, _Bad],
) -> dict[str, T]:
    """Items by usable doc number, taken in a total order. The first of a repeated number is kept;
    every unusable or repeated number is recorded in ``bad``."""
    found: dict[str, T] = {}
    for item in sorted(items, key=order):
        raw = doc_of(item).strip()
        doc = normalise_doc(raw, airline_of(item))
        if not doc:
            note(bad.setdefault(raw, _Bad()), item, "unusable")
        elif doc in found:
            note(bad.setdefault(doc, _Bad()), item, "duplicate")
        else:
            found[doc] = item
    return found


def _note_app(entry: _Bad, item: AppTicket, what: str) -> None:
    entry.app = entry.app or item
    entry.app_note = entry.app_note or what


def _note_amd(entry: _Bad, item: AmadeusTicket, what: str) -> None:
    entry.amd = entry.amd or item
    entry.amd_note = entry.amd_note or what


def compare(
    app: Sequence[AppTicket],
    amadeus: Sequence[AmadeusTicket],
    *,
    day: date,
    other_days_app: Mapping[str, AppTicket] | None = None,
) -> ComparisonResult:
    """Compare the app tickets of ``day`` with the Amadeus lines of ``day``.

    ``other_days_app`` holds app tickets found outside the day (via ``active_ticket_lookup``);
    the caller must leave out tickets of closed days. Never raises on well-typed input, and the
    result does not depend on the order of the inputs.
    """
    warnings: list[str] = []
    bad_app: dict[str, _Bad] = {}
    bad_amd: dict[str, _Bad] = {}
    app_by_doc = _index(
        app, _app_order, lambda t: t.doc_number, lambda t: t.airline, _note_app, bad_app
    )
    amd_by_doc = _index(
        amadeus, _amd_order, lambda t: t.doc_number, lambda t: t.airline, _note_amd, bad_amd
    )
    other_by_doc: dict[str, AppTicket] = {}
    for key, ticket in sorted((other_days_app or {}).items(), key=lambda kv: _app_order(kv[1])):
        doc = normalise_doc(key, ticket.airline)
        if doc:
            other_by_doc.setdefault(doc, ticket)

    flags: list[Flag] = []
    blocked = set(bad_app) | set(bad_amd)
    flags.extend(_bad_flag(raw, bad_app.get(raw), bad_amd.get(raw)) for raw in sorted(blocked))

    matched: list[AmadeusTicket] = []
    cancelled: list[AmadeusTicket] = []
    moves: list[Move] = []

    for doc, amd in amd_by_doc.items():
        mine = app_by_doc.get(doc)
        if mine is None:
            other = other_by_doc.get(doc)
            if other is not None and other.report_day < day:
                mine = other
                moves.append(Move(doc, other.sale_id, other.report_day, day, amd.seq_order))
            elif other is not None:
                flags.append(
                    Flag(
                        kind=KIND_ELSEWHERE,
                        doc_number=doc,
                        field="",
                        app_value=other.report_day.isoformat(),
                        amadeus_value=amd.trnc,
                        responsible_sign=other.staff_sign,
                        needs_validation=False,
                        blocking=True,
                        sale_id=other.sale_id,
                        amadeus_seq=amd.seq_order,
                        amadeus_sign=amd.staff_sign,
                    )
                )
                continue
        if mine is None:
            if amd.trnc.strip().upper() == TRNC_CANX:
                if doc not in blocked:
                    cancelled.append(amd)
                continue
            flags.append(
                Flag(
                    kind=KIND_NOT_SENT,
                    doc_number=doc,
                    field="",
                    app_value=_NO_VALUE,
                    amadeus_value=amd.trnc,
                    responsible_sign=amd.staff_sign,
                    needs_validation=False,
                    blocking=True,
                    amadeus_seq=amd.seq_order,
                    amadeus_sign=amd.staff_sign,
                )
            )
            continue
        diffs = _differences(mine, amd)
        if not diffs and doc not in blocked:
            matched.append(amd)
        for field, app_text, amd_text in diffs:
            flags.append(
                Flag(
                    kind=KIND_DIFFERENT,
                    doc_number=doc,
                    field=field,
                    app_value=app_text,
                    amadeus_value=amd_text,
                    responsible_sign=mine.staff_sign,
                    needs_validation=field in NEEDS_VALIDATION_FIELDS,
                    blocking=True,
                    sale_id=mine.sale_id,
                    amadeus_seq=amd.seq_order,
                    amadeus_sign=amd.staff_sign,
                )
            )

    partner_only: list[AppTicket] = []
    for doc, ticket in app_by_doc.items():
        if doc in amd_by_doc:
            continue
        if ticket.ag_kind == AG_PARTNER:
            partner_only.append(ticket)
            continue
        flags.append(
            Flag(
                kind=KIND_NOT_IN_AMADEUS,
                doc_number=doc,
                field="",
                app_value=ticket.trnc,
                amadeus_value=_NO_VALUE,
                responsible_sign=ticket.staff_sign,
                needs_validation=False,
                blocking=True,
                sale_id=ticket.sale_id,
            )
        )
    if any(t.ag_kind not in (AG_DIRECT, AG_PARTNER) for t in app_by_doc.values()):
        warnings.append("app: a sale kind is neither direct nor partner; treated as direct")

    flags.sort(key=_flag_order)
    matched.sort(key=_amd_order)
    return ComparisonResult(
        matched=tuple(normalise_doc(t.doc_number, t.airline) for t in matched),
        flags=tuple(flags),
        cancelled=tuple(sorted(cancelled, key=_amd_order)),
        moves=tuple(sorted(moves, key=lambda m: (m.doc_number, m.sale_id))),
        not_in_report_partner=tuple(sorted(partner_only, key=_app_order)),
        warnings=tuple(sorted(warnings)),
        matched_tickets=tuple(matched),
    )


def _bad_flag(raw: str, app: _Bad | None, amd: _Bad | None) -> Flag:
    a = app.app if app else None
    b = amd.amd if amd else None
    sign = a.staff_sign if a else (b.staff_sign if b else "")
    return Flag(
        kind=KIND_BAD_NUMBER,
        doc_number=raw,
        field="doc_number",
        app_value=app.app_note if app else _NO_VALUE,
        amadeus_value=amd.amd_note if amd else _NO_VALUE,
        responsible_sign=sign,
        needs_validation=True,
        blocking=True,
        sale_id=a.sale_id if a else None,
        amadeus_seq=b.seq_order if b else None,
        amadeus_sign=b.staff_sign if b else "",
    )


def _flag_order(flag: Flag) -> tuple[int, int, str, str, str, int]:
    """Amadeus order first, flags without a report line after, then doc number, field, kind."""
    seq = flag.amadeus_seq
    sale = flag.sale_id if flag.sale_id is not None else -1
    if seq is None:
        return (1, 0, flag.doc_number, flag.field, flag.kind, sale)
    return (0, seq, flag.doc_number, flag.field, flag.kind, sale)
