"""Parser for the Amadeus daily sales report (.xlsx).

Only the eight Amadeus columns are read, located by header name. Anything else in
the sheet (hand-typed columns, stray formulas) has no effect on the result.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, BinaryIO, Literal

import openpyxl

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
HEADER_SCAN_ROWS = 20

PaxType = Literal["ADT", "CHD", "INF"]

FLAG_MISSING_COST = "missing_cost"
FLAG_UNKNOWN_TRNC = "unknown_trnc"

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"],
        start=1,
    )
}
_RANGE_RE = re.compile(r"^(\d{1,2})([A-Za-z]{3})$")
_CURRENT_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_PAX_SUFFIX_RE = re.compile(r"\s+\((INF|CHD)\)$", re.IGNORECASE)

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


def parse_report(source: Path | bytes | BinaryIO) -> ParsedReport:
    data = _read_bytes(source)
    ws = _open_sheet(data)
    rows = [
        tuple(row) for row in ws.iter_rows(min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True)
    ]
    header_row, columns = _locate_columns(rows)
    header = _parse_header(rows[: header_row - 1])

    warnings: list[str] = []
    tickets: list[ParsedTicket] = []
    seen: set[str] = set()
    width = max(columns.values()) + 1
    data_rows = ws.iter_rows(min_row=header_row + 1, max_col=width, values_only=True)
    for row_no, row in enumerate(data_rows, start=header_row + 1):
        cells = {key: (row[idx] if idx < len(row) else None) for key, idx in columns.items()}
        if all(_is_blank(v) for v in cells.values()):
            continue
        ticket = _build_ticket(cells, row_no, warnings)
        if ticket is None:
            continue
        if ticket.ticket_no in seen:
            warnings.append(
                f"row {row_no}: duplicate DOC NUMBER {ticket.ticket_no} ignored (first one kept)"
            )
            continue
        seen.add(ticket.ticket_no)
        tickets.append(ticket)
    return ParsedReport(header=header, tickets=tuple(tickets), warnings=tuple(warnings))


# --- loading ------------------------------------------------------------------------


def _read_bytes(source: Path | bytes | BinaryIO) -> bytes:
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


def _open_sheet(data: bytes) -> Any:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if sum(i.file_size for i in zf.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise ParseError("file expands to an unreasonable size")
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.worksheets[0]
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError("not a valid .xlsx workbook") from exc
    return ws


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
        raise ParseError(f"'Report date range' must look like 01OCT, got {raw!r}")
    day, month = int(m.group(1)), _MONTHS[m.group(2).upper()]

    generated = _parse_current(current_value)
    year = generated.year - 1 if month > generated.month else generated.year
    try:
        report_date = date(year, month, day)
    except ValueError as exc:
        raise ParseError(f"'Report date range' {raw!r} is not a valid date in {year}") from exc
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
    raise ParseError(f"'Current date' must look like 02-OCT-2026, got {raw!r}")


# --- rows ---------------------------------------------------------------------------


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: object, field: str, row_no: int, warnings: list[str]) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value).strip()
    warnings.append(f"row {row_no}: {field} was a number, converted to text {text!r}")
    return text


def _cost(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, int):
            result = Decimal(value)
        elif isinstance(value, float):
            result = Decimal(repr(value))
        elif isinstance(value, str):
            result = Decimal(value.replace(",", "").replace(" ", ""))
        else:
            return None
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _build_ticket(cells: dict[str, Any], row_no: int, warnings: list[str]) -> ParsedTicket | None:
    ticket_no = _text(cells["ticket_no"], "DOC NUMBER", row_no, warnings)
    if not ticket_no:
        warnings.append(f"row {row_no}: blank DOC NUMBER, row skipped")
        return None

    name = _text(cells["passenger_name"], "PAX NAME", row_no, warnings)
    pax_type: PaxType = "ADT"
    suffix = _PAX_SUFFIX_RE.search(name)
    if suffix:
        pax_type = "INF" if suffix.group(1).upper() == "INF" else "CHD"
        name = name[: suffix.start()].rstrip()

    cost = _cost(cells["cost"])
    trnc = _text(cells["transaction_type"], "TRNC", row_no, warnings).upper()
    flags: list[str] = []
    if cost is None:
        flags.append(FLAG_MISSING_COST)
    if trnc != "TKTT":
        flags.append(FLAG_UNKNOWN_TRNC)

    return ParsedTicket(
        seq_no=_text(cells["seq_no"], "SEQ NO", row_no, warnings),
        airline=_text(cells["airline"], "A/L", row_no, warnings),
        ticket_no=ticket_no,
        cost=cost,
        passenger_name=name,
        pax_type=pax_type,
        staff_sign=_text(cells["staff_sign"], "AS", row_no, warnings),
        transaction_type=trnc,
        booking_ref=_text(cells["booking_ref"], "RLOC", row_no, warnings),
        flags=tuple(flags),
        source_row=row_no,
    )
