"""One path from uploaded bytes to stored tickets, used by the web upload and the CLI.

``ingest_report`` checks the bytes (size, not empty, zip magic), parses them with the parser it is
given (the in-process parser by default, the sandbox in the web app), and stores the result in one
transaction with ``import_report``. The filename is display metadata only: it is sanitized here and
never used as a path.

Tenant rule: the session must be unbound or already bound to ``agency_id``. A session bound to
another agency is refused (``TenantBindingError``); nothing is re-pointed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from app.parsing.amadeus import MAX_FILE_BYTES, ParsedReport, ParseError, parse_report
from app.repository import import_report, upload_ticket_dates

ZIP_MAGIC = b"PK\x03\x04"
FALLBACK_FILENAME = "upload.xlsx"
MAX_FILENAME_CHARS = 200
SUFFIX = ".xlsx"


class EmptyFileError(ParseError):
    """The upload has no bytes."""

    def __init__(self) -> None:
        super().__init__("the file is empty")


class NotAnXlsxError(ParseError):
    """The bytes are not a zip archive, so they cannot be an .xlsx workbook."""

    def __init__(self) -> None:
        super().__init__("not a valid .xlsx workbook")


class FileTooLargeError(ParseError):
    """The upload is bigger than the parser's limit."""

    def __init__(self) -> None:
        super().__init__("file is larger than the 10 MB limit")


@dataclass(frozen=True)
class IngestResult:
    upload_id: int
    inserted: int
    updated: int
    report_date: date
    warnings: tuple[str, ...]
    filename: str  # as stored (sanitized)
    sha256: str
    # The days the tickets of this upload actually belong to, with their counts. Tickets that were
    # already stored keep their original day, so this can differ from ``report_date``.
    ticket_dates: tuple[tuple[date, int], ...] = ()

    @property
    def dates_differ(self) -> bool:
        """Some ticket of this upload belongs to a day other than ``report_date``."""
        return any(day != self.report_date for day, _ in self.ticket_dates)


def sanitize_filename(raw: str | None) -> str:
    """A safe display name: basename (split on ``/`` and backslash), NFC, no control, format
    (bidi, zero-width) or separator characters, at most 200 characters, ``.xlsx`` suffix
    (any case); otherwise ``upload.xlsx``. Never used as a path."""
    if not raw:
        return FALLBACK_FILENAME
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    # Categories C* (control, format, surrogate, private, unassigned) and the line/paragraph
    # separators Zl, Zp.
    name = "".join(
        ch
        for ch in name
        if unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) not in ("Zl", "Zp")
    )
    name = name.strip()
    if not name.lower().endswith(SUFFIX) or not name[: -len(SUFFIX)].strip():
        return FALLBACK_FILENAME
    stem, suffix = name[: -len(SUFFIX)], name[-len(SUFFIX) :]
    stem = stem[: MAX_FILENAME_CHARS - len(SUFFIX)]
    return unicodedata.normalize("NFC", stem + suffix)


def check_bytes(data: bytes) -> None:
    """Cheap checks before any parsing; each raises a ``ParseError`` subclass."""
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLargeError
    if not data:
        raise EmptyFileError
    if not data.startswith(ZIP_MAGIC):
        raise NotAnXlsxError


def ingest_report(
    session: Session,
    agency_id: int,
    data: bytes,
    filename: str | None,
    *,
    parse: Callable[[bytes], ParsedReport] = parse_report,
    report_date_override: date | None = None,
    uploaded_by: int | None = None,
) -> IngestResult:
    """Validate, parse and store one report for ``agency_id``; commits on success.

    Raises ``ParseError`` (or a subclass) for bad input, before anything is written.
    """
    check_bytes(data)
    parsed = parse(data)
    if report_date_override is not None:
        parsed = dataclasses.replace(
            parsed, header=dataclasses.replace(parsed.header, report_date=report_date_override)
        )
    stored_name = sanitize_filename(filename)
    sha256 = hashlib.sha256(data).hexdigest()
    result = import_report(session, agency_id, parsed, stored_name, sha256, uploaded_by)
    ticket_dates = tuple(upload_ticket_dates(session, agency_id, result.upload_id))
    return IngestResult(
        upload_id=result.upload_id,
        inserted=result.inserted,
        updated=result.updated,
        report_date=parsed.header.report_date,
        warnings=parsed.warnings,
        filename=stored_name,
        sha256=sha256,
        ticket_dates=ticket_dates,
    )
