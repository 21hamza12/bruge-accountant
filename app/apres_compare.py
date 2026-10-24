"""The parallel-run comparison (phase C, doc 15): the app's stored TJQ APRES against hers.

Both sides are parsed workbooks (``parse_apres``: the 8 Amadeus columns plus P.B, AG, CLT, AP). The
rows are aligned by ticket: the DOC NUMBER normalised to 10 digits as everywhere else
(``comparison.normalise_doc``; a number that cannot be normalised aligns only with the very same
text). For the rows present on both sides, the cells of the 12 columns of the export are compared:

* money (TOTAL DOC, P.B) as a ``Decimal`` to the cent; an empty cell is not a zero;
* text (SEQ NO, A/L, PAX NAME, AS, TRNC, RLOC, AG, CLT, AP) after the white-space and case
  normalisation of the app (runs of white space are one space, upper and lower case are equal,
  spaces around the ``/`` of a name do not count); the ``'`` the export puts before a text that
  could be read as a formula is not part of the text. PAX NAME includes the ``(CHD)`` / ``(INF)``
  mark, so the padding of the mark does not matter but a child against an adult does.

Pure and bounded: it reads two already-parsed reports, writes nothing, and keeps at most
``MAX_KEPT`` differing cells and ``MAX_KEPT`` rows of each "only in" list (the counts are exact).
Values are display strings; the caller escapes them (the templates autoescape).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.comparison import normalise_doc
from app.export_apres import FORMULA_STARTS, HEADERS, LEADING_BLANKS
from app.parsing.amadeus import ParsedReport, ParsedTicket

COLUMNS: Final = tuple(HEADERS)
MAX_KEPT: Final = 500
_CENT: Final = Decimal("0.01")
_ADULT: Final = "ADT"
CUT_MARK: Final = "… (tronqué)"
_CUT_KEYS: Final = {"AG": "ag", "CLT": "clt", "AP": "ap"}
_AROUND_SLASH: Final = re.compile(r"\s*/\s*")


@dataclass(frozen=True)
class CellDiff:
    doc_number: str
    seq_no: str  # the app's SEQ NO of the row
    column: str
    hers: str  # "" for an empty cell
    app: str


@dataclass(frozen=True)
class RowRef:
    doc_number: str
    seq_no: str
    pax_name: str
    total_doc: str


@dataclass(frozen=True)
class Comparison:
    app_rows: int
    hers_rows: int
    matched: int  # rows present on both sides
    identical: int  # matched rows whose 11 other cells are all equal
    differing_cells: int
    differences: tuple[CellDiff, ...]  # at most MAX_KEPT, in the app's row order then column order
    only_hers: tuple[RowRef, ...]
    only_hers_count: int
    only_app: tuple[RowRef, ...]
    only_app_count: int
    duplicates_hers: int  # lines of hers whose ticket already appeared above (the first is kept)
    duplicates_app: int

    @property
    def same(self) -> bool:
        """Nothing differs: no differing cell, no row on one side only."""
        return not (self.differing_cells or self.only_hers_count or self.only_app_count)

    @property
    def differing_rows(self) -> int:
        return self.matched - self.identical

    @property
    def differences_cut(self) -> bool:
        return self.differing_cells > len(self.differences)


def _key(ticket: ParsedTicket) -> str:
    return normalise_doc(ticket.ticket_no, ticket.airline) or ticket.ticket_no


def _shown(raw: str | None) -> str:
    """The text of a cell as shown: white space collapsed, the export's formula guard removed."""
    text = raw or ""
    if text.startswith("'") and (
        text[1:2] in FORMULA_STARTS or text[1:].lstrip(LEADING_BLANKS)[:1] in FORMULA_STARTS
    ):
        text = text[1:]
    return " ".join(text.split())


def _fold(shown: str) -> str:
    return _AROUND_SLASH.sub("/", shown.casefold())


def _money(value: Decimal | None, raw: str | None = None) -> str:
    """The amount to the cent ("0.00" for a negative zero); when the cell was not an amount, her own
    text (so an unreadable cell is never shown as empty); "" for an empty cell."""
    if value is None:
        return _shown(raw)
    cents = value.quantize(_CENT)
    return format(cents if cents else Decimal("0.00"), "f")


def _name(ticket: ParsedTicket) -> str:
    name = _shown(ticket.passenger_name)
    return name if ticket.pax_type == _ADULT else f"{name} ({ticket.pax_type})"


# column title -> (value shown, is it money). Text is folded to compare, money is compared as shown
# (two decimals), which for a Decimal quantised to the cent is the exact value.
_CELLS: Final[tuple[tuple[str, Callable[[ParsedTicket], str], bool], ...]] = (
    ("SEQ NO", lambda t: _shown(t.seq_no), False),
    ("A/L", lambda t: _shown(t.airline), False),
    ("TOTAL DOC", lambda t: _money(t.cost, t.cost_raw), True),
    ("PAX NAME", _name, False),
    ("AS", lambda t: _shown(t.staff_sign), False),
    ("TRNC", lambda t: _shown(t.transaction_type), False),
    ("RLOC", lambda t: _shown(t.booking_ref), False),
    ("P.B", lambda t: _money(t.pb, t.pb_raw), True),
    ("AG", lambda t: _shown(t.ag), False),
    ("CLT", lambda t: _shown(t.clt), False),
    ("AP", lambda t: _shown(t.ap), False),
)


def _index(report: ParsedReport) -> tuple[dict[str, ParsedTicket], int]:
    """The tickets by aligned number (the first line of a number is kept) and the count of lines
    dropped because their number already appeared."""
    found: dict[str, ParsedTicket] = {}
    repeated = 0
    for ticket in report.tickets:
        key = _key(ticket)
        if key in found:
            repeated += 1
        else:
            found[key] = ticket
    return found, repeated


def _ref(key: str, ticket: ParsedTicket) -> RowRef:
    return RowRef(
        doc_number=key,
        seq_no=_shown(ticket.seq_no),
        pax_name=_name(ticket),
        total_doc=_money(ticket.cost, ticket.cost_raw),
    )


def _cut(rows: Iterable[RowRef]) -> tuple[RowRef, ...]:
    kept: list[RowRef] = []
    for row in rows:
        if len(kept) >= MAX_KEPT:
            break
        kept.append(row)
    return tuple(kept)


def compare(app: ParsedReport, hers: ParsedReport) -> Comparison:
    """Align the two reports by ticket and compare the cells of the matched rows."""
    mine, duplicates_app = _index(app)
    theirs, duplicates_hers = _index(hers)
    matched = identical = differing = 0
    cells: list[CellDiff] = []
    for key, ours in mine.items():
        other = theirs.get(key)
        if other is None:
            continue
        matched += 1
        row_differs = False
        for title, shown, money in _CELLS:
            left, right = shown(other), shown(ours)
            cut_key = _CUT_KEYS.get(title, "")
            cut_hers, cut_app = cut_key in other.cut, cut_key in ours.cut
            if cut_hers or cut_app:  # a text cut to its limit can never be shown equal
                left += CUT_MARK if cut_hers else ""
                right += CUT_MARK if cut_app else ""
            elif (left == right) if money else (_fold(left) == _fold(right)):
                continue
            row_differs = True
            differing += 1
            if len(cells) < MAX_KEPT:
                cells.append(
                    CellDiff(
                        doc_number=key,
                        seq_no=_shown(ours.seq_no),
                        column=title,
                        hers=left,
                        app=right,
                    )
                )
        identical += 0 if row_differs else 1
    only_app = [(k, t) for k, t in mine.items() if k not in theirs]
    only_hers = [(k, t) for k, t in theirs.items() if k not in mine]
    return Comparison(
        app_rows=len(mine),
        hers_rows=len(theirs),
        matched=matched,
        identical=identical,
        differing_cells=differing,
        differences=tuple(cells),
        only_hers=_cut(_ref(k, t) for k, t in only_hers),
        only_hers_count=len(only_hers),
        only_app=_cut(_ref(k, t) for k, t in only_app),
        only_app_count=len(only_app),
        duplicates_hers=duplicates_hers,
        duplicates_app=duplicates_app,
    )


__all__ = ["COLUMNS", "MAX_KEPT", "CellDiff", "Comparison", "RowRef", "compare"]
