"""Helpers that build small Amadeus-shaped workbooks for tests."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import openpyxl

AMADEUS_HEADERS = ["SEQ NO", "A/L", "DOC NUMBER", "TOTAL DOC", "PAX NAME", "AS", "TRNC", "RLOC"]


def ticket_row(
    seq: Any = "000001",
    airline: Any = "199",
    doc: Any = "3940000001",
    cost: Any = 1000,
    name: Any = "DOE/JOHN",
    staff: Any = "0004SV",
    trnc: Any = "TKTT",
    rloc: Any = "AAAAAA",
) -> list[Any]:
    return [seq, airline, doc, cost, name, staff, trnc, rloc]


def build_workbook(
    rows: list[list[Any]],
    *,
    range_raw: str | None = "01OCT",
    current: str | None = "02-OCT-2026",
    headers: list[str] | None = None,
    header_row: int = 5,
    hand_typed: bool = False,
) -> bytes:
    """Create an .xlsx (as bytes) shaped like an Amadeus daily sales export."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws["A1"] = "Agy no"
    ws["B1"] = ""
    ws["C1"] = "Report date range"
    if range_raw is not None:
        ws["D1"] = range_raw
    ws["A2"] = "Office"
    ws["A3"] = "Agent"
    ws["C3"] = "Current date"
    if current is not None:
        ws["D3"] = current
    for col, title in enumerate(AMADEUS_HEADERS if headers is None else headers, start=1):
        ws.cell(row=header_row, column=col, value=title)
    if hand_typed:
        for col, title in enumerate(["P.B", "AG", "CLT", "AP"], start=9):
            ws.cell(row=header_row, column=col, value=title)
    for offset, values in enumerate(rows, start=1):
        for col, value in enumerate(values, start=1):
            if value is not None:
                ws.cell(row=header_row + offset, column=col, value=value)
        if hand_typed:
            ws.cell(row=header_row + offset, column=9, value=999)
            ws.cell(row=header_row + offset, column=10, value="JUNK")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def set_cached_formula(xlsx: bytes, cell: str, formula: str, cached: int | None) -> bytes:
    """Rewrite sheet XML so ``cell`` holds ``<f>formula</f>`` plus an optional cached ``<v>``.

    openpyxl cannot write cached formula results, so we patch the XML directly.
    """
    src = zipfile.ZipFile(io.BytesIO(xlsx))
    out = io.BytesIO()
    pattern = re.compile(rf'<c r="{cell}"[^>]*?(/>|>.*?</c>)', re.DOTALL)
    value_xml = "" if cached is None else f"<v>{cached}</v>"
    replacement = f'<c r="{cell}"><f>{formula}</f>{value_xml}</c>'
    patched = False
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                text, count = pattern.subn(replacement, data.decode("utf-8"), count=1)
                patched = count == 1
                data = text.encode("utf-8")
            dst.writestr(item, data)
    assert patched, f"cell {cell} not found in sheet XML"
    return out.getvalue()
