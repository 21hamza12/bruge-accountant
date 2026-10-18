"""Resource limits and hostile-input handling of the Amadeus parser."""

from __future__ import annotations

import io
import re
import time
import zipfile

import openpyxl
import pytest

from app.parsing import amadeus
from app.parsing.amadeus import ParseError, parse_report
from tests.builders import add_zip_entries, build_workbook, patch_sheet_xml, ticket_row


def test_stray_cell_at_last_excel_row_is_fast_and_ignored() -> None:
    rows = [ticket_row(doc=f"39400000{i:02d}") for i in range(3)]
    data = build_workbook(rows, stray={"Z1048576": "stray"})
    start = time.perf_counter()
    result = parse_report(data)
    elapsed = time.perf_counter() - start
    assert elapsed < 3, f"took {elapsed:.1f}s"
    assert [t.ticket_no for t in result.tickets] == ["3940000000", "3940000001", "3940000002"]
    assert result.warnings == ()


def test_stray_cell_inside_amadeus_columns_far_down_is_a_warning() -> None:
    data = build_workbook([ticket_row()], stray={"A200000": "x"})
    start = time.perf_counter()
    result = parse_report(data)
    assert time.perf_counter() - start < 3
    assert len(result.tickets) == 1
    assert len(result.warnings) == 1
    assert "row 200000" in result.warnings[0]


def test_wrong_declared_dimension_does_not_truncate_rows() -> None:
    rows = [ticket_row(doc=f"39400000{i:02d}") for i in range(5)]
    original = build_workbook(rows)
    sheet = zipfile.ZipFile(io.BytesIO(original)).read("xl/worksheets/sheet1.xml").decode()
    assert '<dimension ref="A1:H10"' in sheet
    data = patch_sheet_xml(
        original,
        lambda xml: re.sub(r'<dimension ref="[^"]*"\s*/>', '<dimension ref="A1:H6"/>', xml),
    )
    assert b'ref="A1:H6"' in zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    assert len(parse_report(data).tickets) == 5


def test_too_many_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(amadeus, "MAX_DATA_ROWS", 5)
    rows = [ticket_row(doc=f"39400000{i:02d}") for i in range(6)]
    with pytest.raises(ParseError, match="too many rows"):
        parse_report(build_workbook(rows))
    assert len(parse_report(build_workbook(rows[:5])).tickets) == 5


def test_zip_with_too_many_entries() -> None:
    base = build_workbook([ticket_row()])
    extra = {f"junk/{i}.txt": b"x" for i in range(amadeus.MAX_ZIP_ENTRIES + 1)}
    with pytest.raises(ParseError, match="too many entries"):
        parse_report(add_zip_entries(base, extra))


def test_zip_entry_declaring_huge_size() -> None:
    base = build_workbook([ticket_row()])
    bomb = add_zip_entries(base, {"junk/big.bin": b"\0" * (amadeus.MAX_ENTRY_BYTES + 1)})
    assert len(bomb) < amadeus.MAX_FILE_BYTES
    with pytest.raises(ParseError, match="unreasonable size"):
        parse_report(bomb)


def test_zip_total_uncompressed_size_too_large() -> None:
    base = build_workbook([ticket_row()])
    chunk = b"\0" * (amadeus.MAX_ENTRY_BYTES // 2)
    entries = {f"junk/{i}.bin": chunk for i in range(4)}
    assert len(chunk) * 4 > amadeus.MAX_UNCOMPRESSED_BYTES
    with pytest.raises(ParseError, match="unreasonable size"):
        parse_report(add_zip_entries(base, entries))


def test_limits_have_sane_values() -> None:
    assert amadeus.MAX_UNCOMPRESSED_BYTES <= 30 * 1024 * 1024
    assert amadeus.MAX_ZIP_ENTRIES <= 1000
    assert amadeus.MAX_DATA_ROWS <= 50_000


_INTERNAL = '<!DOCTYPE x [<!ENTITY a "boom">]>'
_EXTERNAL = '<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]>'


@pytest.mark.parametrize("doctype", [_INTERNAL, _EXTERNAL], ids=["internal", "external"])
def test_sheet_xml_with_entity_declaration_is_rejected(doctype: str) -> None:
    def inject(xml: str) -> str:
        head, _, rest = xml.partition("?>")
        return f"{head}?>{doctype}{rest}"

    data = patch_sheet_xml(build_workbook([ticket_row()]), inject)
    assert doctype.encode() in zipfile.ZipFile(io.BytesIO(data)).read("xl/worksheets/sheet1.xml")
    with pytest.raises(ParseError):
        parse_report(data)


def test_refuses_to_run_without_defusedxml_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    import openpyxl.xml

    monkeypatch.setattr(openpyxl.xml, "DEFUSEDXML", False)
    with pytest.raises(RuntimeError, match="defusedxml"):
        parse_report(build_workbook([ticket_row()]))


def test_workbook_with_only_a_chartsheet_is_rejected() -> None:
    wb = openpyxl.Workbook()
    wb.remove(wb.worksheets[0])
    wb.create_chartsheet()
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(ParseError):
        parse_report(buf.getvalue())
