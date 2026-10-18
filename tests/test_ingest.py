"""Step 24: the shared ingest path (web and CLI) and the filename sanitizer."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.formatting import money
from app.ingest import (
    EmptyFileError,
    FileTooLargeError,
    IngestResult,
    NotAnXlsxError,
    ingest_report,
    sanitize_filename,
)
from app.models import Ticket, Upload
from app.parsing.amadeus import MAX_FILE_BYTES, ParsedReport, ParseError, parse_report
from app.tenancy import TenantBindingError, bind_tenant
from tests.web_support import Seeded, WebDb, owner_session, seed

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"


# --- sanitize_filename --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1OCT.xlsx", "1OCT.xlsx"),
        ("REPORT.XLSX", "REPORT.XLSX"),
        ("../../etc/passwd.xlsx", "passwd.xlsx"),
        ("C:\\Users\\bob\\1OCT.xlsx", "1OCT.xlsx"),
        ("a/b\\c/d.xlsx", "d.xlsx"),
        ("dir/", "upload.xlsx"),
        ("", "upload.xlsx"),
        (None, "upload.xlsx"),
        ("notes.txt", "upload.xlsx"),
        ("report.xls", "upload.xlsx"),
        ("report.xlsx.exe", "upload.xlsx"),
        (".xlsx", "upload.xlsx"),
        ("   .xlsx", "upload.xlsx"),
        ("evil\u202egnp.xlsx", "evilgnp.xlsx"),  # right-to-left override removed
        ("zero\u200bwidth\ufeff.xlsx", "zerowidth.xlsx"),
        ("tab\tand\nnewline\x00nul.xlsx", "tabandnewlinenul.xlsx"),
        ("line\u2028sep.xlsx", "linesep.xlsx"),
        ("lone\ud800surrogate.xlsx", "lonesurrogate.xlsx"),
        ("e\u0301t\u00e9.xlsx", "\u00e9t\u00e9.xlsx"),  # NFC
        ("  spaced name .xlsx", "spaced name .xlsx"),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str) -> None:
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_caps_the_length_and_keeps_the_suffix() -> None:
    result = sanitize_filename("a" * 500 + ".xlsx")
    assert len(result) == 200
    assert result.endswith(".xlsx")
    assert result.startswith("aaa")


def test_sanitize_filename_keeps_html_characters_as_plain_text() -> None:
    # Escaping is the template's job; the name is never used as a path.
    assert sanitize_filename('<script>"x".xlsx') == '<script>"x".xlsx'


# --- money --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("746423"), "746423"),
        (Decimal("746423.00"), "746423"),
        (Decimal("1234.5"), "1234.50"),
        (Decimal("0"), "0"),
        (Decimal("0.05"), "0.05"),
    ],
)
def test_money(value: Decimal, text: str) -> None:
    assert money(value) == text


# --- ingest_report ------------------------------------------------------------------------------


@pytest.fixture
def seeded(web_db: WebDb) -> Seeded:
    return seed(web_db.owner_url)


def _data() -> bytes:
    return FIXTURE.read_bytes()


def _counts(web_db: WebDb, agency_id: int) -> tuple[int, int]:
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, agency_id)
        tickets = (
            s.scalar(select(func.count()).select_from(Ticket).where(Ticket.agency_id == agency_id))
            or 0
        )
        uploads = (
            s.scalar(select(func.count()).select_from(Upload).where(Upload.agency_id == agency_id))
            or 0
        )
    return tickets, uploads


def test_ingest_stores_the_fixture_and_reports_the_counts(web_db: WebDb, seeded: Seeded) -> None:
    data = _data()
    with owner_session(web_db.owner_url) as s:
        result = ingest_report(
            s, seeded.agency_a, data, "dir/1OCT.xlsx", uploaded_by=seeded.manager_a
        )
    assert (result.inserted, result.updated) == (48, 0)
    assert result.filename == "1OCT.xlsx"
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.report_date.isoformat() == "2026-10-01"
    assert _counts(web_db, seeded.agency_a) == (48, 1)
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, seeded.agency_a)
        upload = s.scalars(select(Upload)).one()
        assert upload.id == result.upload_id
        assert upload.uploaded_by_user_id == seeded.manager_a
        assert upload.filename == "1OCT.xlsx"
    # another agency saw none of it
    assert _counts(web_db, seeded.agency_b) == (0, 0)


def test_reingest_updates_and_inserts_nothing(web_db: WebDb, seeded: Seeded) -> None:
    for expected in ((48, 0), (0, 48)):
        with owner_session(web_db.owner_url) as s:
            result = ingest_report(s, seeded.agency_a, _data(), "1OCT.xlsx")
        assert (result.inserted, result.updated) == expected
    assert _counts(web_db, seeded.agency_a) == (48, 2)


def test_report_date_override(web_db: WebDb, seeded: Seeded) -> None:
    with owner_session(web_db.owner_url) as s:
        result = ingest_report(
            s, seeded.agency_a, _data(), "1OCT.xlsx", report_date_override=date(2026, 10, 5)
        )
    assert result.report_date == date(2026, 10, 5)


def test_a_session_bound_to_another_agency_is_refused(web_db: WebDb, seeded: Seeded) -> None:
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, seeded.agency_b)
        with pytest.raises(TenantBindingError):
            ingest_report(s, seeded.agency_a, _data(), "1OCT.xlsx")
    assert _counts(web_db, seeded.agency_a) == (0, 0)
    assert _counts(web_db, seeded.agency_b) == (0, 0)


def test_a_session_bound_to_the_same_agency_works(web_db: WebDb, seeded: Seeded) -> None:
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, seeded.agency_a)
        result = ingest_report(s, seeded.agency_a, _data(), "1OCT.xlsx")
    assert result.inserted == 48


def test_the_parser_is_injectable(web_db: WebDb, seeded: Seeded) -> None:
    calls: list[int] = []

    def fake(data: bytes) -> ParsedReport:
        calls.append(len(data))
        return parse_report(data)

    with owner_session(web_db.owner_url) as s:
        ingest_report(s, seeded.agency_a, _data(), "1OCT.xlsx", parse=fake)
    assert calls == [len(_data())]


def test_a_parse_error_stores_nothing(web_db: WebDb, seeded: Seeded) -> None:
    def broken(data: bytes) -> ParsedReport:
        raise ParseError("bad sheet")

    with owner_session(web_db.owner_url) as s, pytest.raises(ParseError, match="bad sheet"):
        ingest_report(s, seeded.agency_a, _data(), "1OCT.xlsx", parse=broken)
    assert _counts(web_db, seeded.agency_a) == (0, 0)


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (b"", EmptyFileError),
        (b"not an excel file", NotAnXlsxError),
        (b"PK\x03", NotAnXlsxError),
        (b"%PDF-1.7" + b"0" * 100, NotAnXlsxError),
        (b"PK\x03\x04" + b"0" * (MAX_FILE_BYTES), FileTooLargeError),
    ],
    ids=["empty", "text", "short-prefix", "pdf", "too-big"],
)
def test_input_checks_run_before_the_parser(
    web_db: WebDb, seeded: Seeded, data: bytes, error: type[ParseError]
) -> None:
    def must_not_run(_: bytes) -> ParsedReport:
        raise AssertionError("the parser must not be reached")

    with owner_session(web_db.owner_url) as s, pytest.raises(error):
        ingest_report(s, seeded.agency_a, data, "x.xlsx", parse=must_not_run)


def test_the_checks_are_parse_errors_with_safe_messages() -> None:
    for error in (EmptyFileError, NotAnXlsxError, FileTooLargeError):
        assert issubclass(error, ParseError)


def test_the_result_is_immutable() -> None:
    result = IngestResult(1, 2, 3, date(2026, 10, 1), (), "a.xlsx", "0" * 64)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.inserted = 5  # type: ignore[misc]


def test_ingest_reports_the_days_the_tickets_belong_to(web_db: WebDb) -> None:
    seeded: Seeded = seed(web_db.owner_url)
    data = FIXTURE.read_bytes()
    with owner_session(web_db.owner_url) as s:
        first = ingest_report(s, seeded.agency_a, data, "1OCT.xlsx")
        again = ingest_report(
            s, seeded.agency_a, data, "1OCT.xlsx", report_date_override=date(2026, 10, 5)
        )
    assert first.ticket_dates == ((date(2026, 10, 1), 48),)
    assert not first.dates_differ
    # the re-upload says 5 October, but the 48 tickets already existed and keep 1 October
    assert again.report_date == date(2026, 10, 5)
    assert again.ticket_dates == ((date(2026, 10, 1), 48),)
    assert again.dates_differ
