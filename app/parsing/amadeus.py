"""Parser for the Amadeus daily sales report (.xlsx).

Only the eight Amadeus columns are read, located by header name. Anything else in
the sheet (hand-typed columns, stray formulas) has no effect on the result.

The workbook is streamed (``read_only``) so a tiny file that declares a cell on row
1,048,576 cannot make us materialise a million empty rows, and the archive is checked
for zip bombs before openpyxl sees it. XML goes through defusedxml (no DTDs/entities).
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, BinaryIO, Literal

import openpyxl
import openpyxl.xml
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024
MAX_ENTRY_BYTES = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 1000
MAX_DATA_ROWS = 50_000  # non-blank rows below the header
MAX_COLUMNS = 30  # only this many leading columns are ever read
HEADER_SCAN_ROWS = 20
MAX_WARNINGS = 100
WARNING_VALUE_CHARS = 40

PaxType = Literal["ADT", "CHD", "INF"]

FLAG_MISSING_COST = "missing_cost"
FLAG_UNKNOWN_TRNC = "unknown_trnc"
FLAG_MISSING_BOOKING_REF = "missing_booking_ref"
FLAG_MISSING_STAFF_SIGN = "missing_staff_sign"
FLAG_DUPLICATE_IN_FILE = "duplicate_in_file"
FLAG_FIELD_TOO_LONG = "field_too_long"
FLAG_COST_ROUNDED = "cost_rounded"

# Column lengths of ``app.models.Ticket``; longer values are truncated and flagged.
FIELD_LIMITS = {
    "seq_no": 32,
    "airline": 8,
    "staff_sign": 32,
    "transaction_type": 16,
    "booking_ref": 16,
}
COST_LIMIT = Decimal(10) ** 12  # Numeric(14, 2)
_CENT = Decimal("0.01")

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_RANGE_RE = re.compile(r"^(\d{1,2})([A-Za-z]{3})$")
_CURRENT_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_PAX_SUFFIX_RE = re.compile(r"\s*\((INF|CHD)\)$", re.IGNORECASE)
_PAX_MARKER_RE = re.compile(r"\((INF|CHD)", re.IGNORECASE)
_TICKET_NO_RE = re.compile(r"\d{8,15}", re.ASCII)

# header name -> internal key
_REQUIRED = {
    "SEQ NO": "seq_no",
    "A/L": "airline",
    "DOC NUMBER": "ticket_no",
    "TOTAL DOC": "cost",
    "PAX NAME": "passenger_name",
    "AS": "staff_sign",
    "TRNC": "transaction_type",
    "RLOC": "booking_ref",
}


class ParseError(Exception):
    """The file is not a readable Amadeus daily sales report."""


@dataclass(frozen=True)
class ReportHeader:
    report_range_raw: str
    report_date: date
    generated_on: date


@dataclass(frozen=True)
class ParsedTicket:
    seq_no: str
    airline: str
    ticket_no: str
    cost: Decimal | None
    passenger_name: str
    pax_type: PaxType
    staff_sign: str
    transaction_type: str
    booking_ref: str
    flags: tuple[str, ...]
    source_row: int


@dataclass(frozen=True)
class ParsedReport:
    header: ReportHeader
    tickets: tuple[ParsedTicket, ...]
    warnings: tuple[str, ...]


class _Warnings:
    """Keeps the first ``MAX_WARNINGS`` messages and counts the rest."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._dropped = 0

    def add(self, message: str) -> None:
        if len(self._items) < MAX_WARNINGS:
            self._items.append(message)
        else:
            self._dropped += 1

    def result(self) -> tuple[str, ...]:
        if self._dropped:
            return (*self._items, f"... and {self._dropped} more warnings")
        return tuple(self._items)


def _short(value: object) -> str:
    """repr of a cell value, cut to ``WARNING_VALUE_CHARS`` so warnings stay small."""
    text = repr(value)
    if len(text) <= WARNING_VALUE_CHARS:
        return text
    return text[: WARNING_VALUE_CHARS - 3] + "..."


def parse_report(source: Path | bytes | bytearray | BinaryIO) -> ParsedReport:
    data = _read_bytes(source)
    with _open_sheet(data) as ws:
        rows = _iter_rows(ws)
        head = list(itertools.islice(rows, HEADER_SCAN_ROWS))
        header_row, columns = _locate_columns(head)
        header = _parse_header(head[: header_row - 1])

        warnings = _Warnings()
        tickets: list[ParsedTicket] = []
        index_by_no: dict[str, int] = {}
        non_blank = 0
        blank_row: tuple[Any, ...] | None = None
        # Rows after the header that were already pulled while scanning for it come first.
        data_rows = itertools.chain(head[header_row:], rows)
        for row_no, row in enumerate(data_rows, start=header_row + 1):
            if row is blank_row:  # openpyxl yields one shared tuple for every missing row
                continue
            cells = {key: (row[idx] if idx < len(row) else None) for key, idx in columns.items()}
            if all(_is_blank(v) for v in cells.values()):
                blank_row = row
                continue
            non_blank += 1
            if non_blank > MAX_DATA_ROWS:
                raise ParseError(f"too many rows (more than {MAX_DATA_ROWS} data rows)")
            ticket = _build_ticket(cells, row_no, warnings)
            if ticket is None:
                continue
            first = index_by_no.get(ticket.ticket_no)
            if first is None:
                index_by_no[ticket.ticket_no] = len(tickets)
                tickets.append(ticket)
                continue
            warnings.add(
                f"row {row_no}: duplicate DOC NUMBER {ticket.ticket_no} ignored (first one kept)"
            )
            kept = tickets[first]
            if FLAG_DUPLICATE_IN_FILE not in kept.flags:
                flags = tuple(sorted({*kept.flags, FLAG_DUPLICATE_IN_FILE}))
                tickets[first] = dataclasses.replace(kept, flags=flags)
    return ParsedReport(header=header, tickets=tuple(tickets), warnings=warnings.result())


# --- loading ------------------------------------------------------------------------


def _read_bytes(source: Path | bytes | bytearray | BinaryIO) -> bytes:
    too_big = ParseError("file is larger than the 10 MB limit")
    if isinstance(source, Path):
        try:
            if source.stat().st_size > MAX_FILE_BYTES:
                raise too_big
            return source.read_bytes()
        except FileNotFoundError as exc:
            raise ParseError(f"file not found: {source}") from exc
        except OSError as exc:
            raise ParseError(f"cannot read file: {exc}") from exc
    if isinstance(source, bytes | bytearray):
        data = bytes(source)
    else:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise too_big
    return data


def _check_archive(data: bytes) -> None:
    """Reject zip bombs from the central directory, before anything is decompressed."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise ParseError("not a valid .xlsx workbook") from exc
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ParseError(f"workbook has too many entries (limit {MAX_ZIP_ENTRIES})")
    sizes = [i.file_size for i in infos]
    if any(size > MAX_ENTRY_BYTES for size in sizes) or sum(sizes) > MAX_UNCOMPRESSED_BYTES:
        raise ParseError("file expands to an unreasonable size")


@contextmanager
def _open_sheet(data: bytes) -> Iterator[ReadOnlyWorksheet]:
    if not openpyxl.xml.DEFUSEDXML:
        raise RuntimeError("defusedxml is required (is OPENPYXL_DEFUSEDXML disabled?)")
    _check_archive(data)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ParseError("not a valid .xlsx workbook") from exc
    try:
        sheets = wb.worksheets
        ws = sheets[0] if sheets else None
        if not isinstance(ws, ReadOnlyWorksheet):
            raise ParseError("workbook has no worksheet")
        # Declared dimensions are untrusted: too small drops rows, too big wastes time.
        ws.reset_dimensions()
        yield ws
    finally:
        wb.close()


def _iter_rows(ws: ReadOnlyWorksheet) -> Iterator[tuple[Any, ...]]:
    try:
        yield from ws.iter_rows(min_row=1, max_col=MAX_COLUMNS, values_only=True)
    except Exception as exc:
        raise ParseError("worksheet is malformed or uses forbidden XML") from exc


# --- header block -------------------------------------------------------------------


def _norm(value: object) -> str:
    return " ".join(str(value).upper().split()) if value is not None else ""


def _locate_columns(rows: list[tuple[Any, ...]]) -> tuple[int, dict[str, int]]:
    for number, row in enumerate(rows, start=1):
        if any(_norm(v) == "DOC NUMBER" for v in row):
            columns: dict[str, int] = {}
            for idx, value in enumerate(row):
                key = _REQUIRED.get(_norm(value))
                if key is not None and key not in columns:
                    columns[key] = idx
            missing = [h for h, k in _REQUIRED.items() if k not in columns]
            if missing:
                raise ParseError(f"missing required column(s): {', '.join(missing)}")
            return number, columns
    raise ParseError(
        f"header row not found (no 'DOC NUMBER' column in the first {HEADER_SCAN_ROWS} rows)"
    )


def _label_value(rows: list[tuple[Any, ...]], label: str) -> Any:
    for row in rows:
        for idx, value in enumerate(row):
            if _norm(value) == label.upper() and idx + 1 < len(row):
                return row[idx + 1]
    return None


def _parse_header(rows: list[tuple[Any, ...]]) -> ReportHeader:
    range_value = _label_value(rows, "Report date range")
    current_value = _label_value(rows, "Current date")

    raw = str(range_value).strip() if range_value is not None else ""
    m = _RANGE_RE.match(raw)
    if not m or m.group(2).upper() not in _MONTHS:
        raise ParseError(f"'Report date range' must look like 01OCT, got {_short(raw)}")
    day, month = int(m.group(1)), _MONTHS[m.group(2).upper()]

    generated = _parse_current(current_value)
    year = generated.year - 1 if month > generated.month else generated.year
    try:
        report_date = date(year, month, day)
    except ValueError as exc:
        raise ParseError(
            f"'Report date range' {_short(raw)} is not a valid date in {year}"
        ) from exc
    return ReportHeader(report_range_raw=raw, report_date=report_date, generated_on=generated)


def _parse_current(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip() if value is not None else ""
    m = _CURRENT_RE.match(raw)
    if m and m.group(2).upper() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)))
        except ValueError:
            pass
    raise ParseError(f"'Current date' must look like 02-OCT-2026, got {_short(raw)}")


# --- rows ---------------------------------------------------------------------------


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: object, field: str, row_no: int, warnings: _Warnings) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value).strip()
    warnings.add(f"row {row_no}: {field} was a number, converted to text {_short(text)}")
    return text


def _cost(value: object) -> tuple[Decimal | None, bool]:
    """Return ``(cost, was_rounded)``; cost is None when absent or not storable."""
    if value is None or isinstance(value, bool):
        return None, False
    try:
        if isinstance(value, int):
            parsed = Decimal(value)
        elif isinstance(value, float):
            parsed = Decimal(repr(value))
        elif isinstance(value, str):
            parsed = Decimal(value.replace(",", "").replace(" ", ""))
        else:
            return None, False
        if not parsed.is_finite() or abs(parsed) >= COST_LIMIT:
            return None, False
        rounded = parsed.quantize(_CENT, rounding=ROUND_HALF_UP)
    except ArithmeticError:
        return None, False
    return rounded, rounded != parsed


def _limited(
    text: str, key: str, label: str, row_no: int, flags: set[str], warnings: _Warnings
) -> str:
    limit = FIELD_LIMITS[key]
    if len(text) <= limit:
        return text
    flags.add(FLAG_FIELD_TOO_LONG)
    warnings.add(f"row {row_no}: {label} longer than {limit} characters, truncated")
    return text[:limit]


def _build_ticket(cells: dict[str, Any], row_no: int, warnings: _Warnings) -> ParsedTicket | None:
    ticket_no = _text(cells["ticket_no"], "DOC NUMBER", row_no, warnings)
    if not ticket_no:
        warnings.add(f"row {row_no}: blank DOC NUMBER, row skipped")
        return None
    if not _TICKET_NO_RE.fullmatch(ticket_no):
        warnings.add(
            f"row {row_no}: DOC NUMBER {_short(ticket_no)} is not 8-15 digits, row skipped"
        )
        return None

    flags: set[str] = set()
    name = _text(cells["passenger_name"], "PAX NAME", row_no, warnings)
    pax_type: PaxType = "ADT"
    suffix = _PAX_SUFFIX_RE.search(name)
    if suffix:
        pax_type = "INF" if suffix.group(1).upper() == "INF" else "CHD"
        name = name[: suffix.start()].rstrip()
    elif _PAX_MARKER_RE.search(name):
        warnings.add(
            f"row {row_no}: pax type marker in {_short(name)} not recognised, treated as adult"
        )

    cost, rounded = _cost(cells["cost"])
    if cost is None:
        flags.add(FLAG_MISSING_COST)
    if rounded:
        flags.add(FLAG_COST_ROUNDED)

    def field(key: str, label: str) -> str:
        return _limited(
            _text(cells[key], label, row_no, warnings), key, label, row_no, flags, warnings
        )

    seq_no = field("seq_no", "SEQ NO")
    airline = field("airline", "A/L")
    staff_sign = field("staff_sign", "AS")
    trnc = field("transaction_type", "TRNC").upper()
    booking_ref = field("booking_ref", "RLOC")
    if trnc != "TKTT":
        flags.add(FLAG_UNKNOWN_TRNC)
    if not booking_ref:
        flags.add(FLAG_MISSING_BOOKING_REF)
    if not staff_sign:
        flags.add(FLAG_MISSING_STAFF_SIGN)

    return ParsedTicket(
        seq_no=seq_no,
        airline=airline,
        ticket_no=ticket_no,
        cost=cost,
        passenger_name=name,
        pax_type=pax_type,
        staff_sign=staff_sign,
        transaction_type=trnc,
        booking_ref=booking_ref,
        flags=tuple(sorted(flags)),
        source_row=row_no,
    )
