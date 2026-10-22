"""The TJQ APRES: the accountant's workbook of one day, built from the Amadeus report and the app.

Layout (docs/design/05-daily-comparison.md section 5): rows 1 to 3 hold ``Agy no``, ``Report date
range`` and ``Current date``; row 5 the headers ``SEQ NO, A/L, DOC NUMBER, TOTAL DOC, PAX NAME, AS,
TRNC, RLOC, P.B, AG, CLT, AP``; data from row 6, one line per ticket of the compared Amadeus line
set, CANX lines removed, in SEQ order; a totals row under the table (after one blank row).

* ``apres_rows`` is the data: the Amadeus line set of the day (the newest upload, through
  ``repository.lines_upload_for``) with, per ticket, the P.B / AG / CLT / AP of the app sale that
  holds it. P.B follows decision 21 (``pb_per_line``: the full price on the sale's first ticket, 0
  on the others) and the lines of a multi-ticket sale carry a marker ("Vente #12 · 2 billets"). A
  line with no active app sale has empty P.B / AG / CLT / AP and is marked (B8 wiring / B9 turn it
  into a flag). Manager and accountant only (``permissions.can_export_day``).
* ``build_apres_xlsx`` is pure: report in, bytes out. Amounts are numbers (2 decimals), nothing is a
  formula: every text cell is cleaned of characters XML cannot hold, gets a leading ``'`` when it
  starts with ``= + - @``, tab or CR, and is forced to a string cell.
* ``corrections`` (B9 hook): ``{(doc number, column title): (original, approved, who)}``. Empty
  until the approvals exist; when filled, the cell shows the approved value, highlighted, with a
  comment giving the Amadeus original and who approved. TODO(B9): ``corrections_for`` reads them
  from the approved flags, and the totals are recomputed from the approved amounts.
* P.B never vanishes: a sale's full P.B goes on the first exported line of that sale (first in SEQ
  order among its non-CANX lines present in the report), 0 on its other exported lines; a sale none
  of whose tickets is exported puts nothing (the comparison flags it). Each sale ticket is consumed
  once: a second report line with the same number is a "doublon" (marked, empty P.B / AG
  / CLT / AP).
* Bounds: at most ``MAX_LINES`` lines, ``MAX_VERSIONS`` stored versions per day and
  ``MAX_BYTES`` per workbook (also a CHECK in migration 0012); beyond them closing is refused with
  a French message.
* A day closed without any Amadeus upload stores no export (the page says so). B8 wiring refuses
  uploads for closed days, so the export path stays consistent: after reopen + upload + close,
  version 1 is stored.
* Storage: ``close_day_with_export`` closes the day and stores the next ``day_exports`` version in
  the same transaction (nothing when there is no Amadeus upload). TODO(B9): close preconditions go
  in front of ``days.close_day`` there.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Final, cast

import openpyxl
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE, Cell
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, days, permissions, repository, sales
from app.comparison import TRNC_CANX, normalise_doc
from app.models import AmadeusLine, DayExport, ReportDay, Upload, User
from app.sales_rules import SaleView

HEADERS: Final = (
    "SEQ NO", "A/L", "DOC NUMBER", "TOTAL DOC", "PAX NAME", "AS", "TRNC", "RLOC",
    "P.B", "AG", "CLT", "AP",
)  # fmt: skip
FIRST_ROW: Final = 6
HEADER_ROW: Final = 5
XLSX_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MONEY_FORMAT: Final = "#,##0.00"
FORMULA_STARTS: Final = frozenset("=+-@\t\r\n\x0b\x0c\xa0")
LEADING_BLANKS: Final = " \t\r\n\x0b\x0c\xa0"
PHONE_E164: Final = re.compile(r"\+[0-9]{8,15}")
MAX_LINES: Final = 50_000  # the parser's limit
MAX_VERSIONS: Final = 50
MAX_BYTES: Final = 5_000_000
TOO_MANY_LINES: Final = "Le rapport est trop long pour être exporté."
TOO_MANY_VERSIONS: Final = "Trop de versions du TJQ APRES ont déjà été enregistrées pour ce jour."
TOO_BIG: Final = "Le TJQ APRES est trop volumineux pour être enregistré."
DUPLICATE_NOTE: Final = "Doublon : ce numéro figure déjà plus haut dans le rapport."
MONTHS: Final = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
UNMATCHED_NOTE: Final = "Aucune vente dans l'application pour ce billet."
NO_REPORT: Final = "Aucun rapport Amadeus pour cette journée."

Corrections = Mapping[tuple[str, str], tuple[str, str, str]]
NO_CORRECTIONS: Final[Corrections] = {}

_COL_SEQ, _COL_DOC, _COL_TOTAL, _COL_PAX, _COL_PB, _COL_AG = 1, 3, 4, 5, 9, 10
_COL_CLT, _COL_AP = 11, 12
_FILL_GROUP = PatternFill("solid", fgColor="FFDDEBF7")
_FILL_UNMATCHED = PatternFill("solid", fgColor="FFFCE4D6")
_FILL_CORRECTED = PatternFill("solid", fgColor="FFFFFF00")
_FILL_HEAD = PatternFill("solid", fgColor="FFD9D9D9")


@dataclass(frozen=True)
class ApresLine:
    """One line of the table: the 8 Amadeus columns, then P.B / AG / CLT / AP of its app sale."""

    seq_no: str
    airline: str
    doc_number: str
    total_doc: Decimal | None
    pax_name: str
    staff_sign: str
    trnc: str
    rloc: str
    pb: Decimal | None = None  # None: no app sale holds the ticket
    ag: str = ""
    clt: str = ""
    ap: str = ""
    sale_id: int | None = None
    ticket_count: int = 0  # tickets of the sale
    duplicate: bool = False  # the number already appeared on an earlier line

    @property
    def marker(self) -> str:
        """ "Vente #12 · 2 billets" for a line of a multi-ticket sale, else empty."""
        if self.sale_id is None or self.ticket_count < 2:
            return ""
        return f"Vente #{self.sale_id} · {self.ticket_count} billets"


@dataclass(frozen=True)
class ApresReport:
    day: date
    agy_no: str
    range_raw: str
    current_date: str
    lines: tuple[ApresLine, ...]
    ticket_count: int
    total_doc: Decimal
    total_pb: Decimal
    total_margin: Decimal  # sum of P.B minus sum of TOTAL DOC
    unmatched: int  # lines without an app sale


class ExportError(days.DayError):
    """The export cannot be built or stored (a bound is hit); ``str(error)`` is French."""


def without_canx(lines: Iterable[ApresLine]) -> list[ApresLine]:
    """The lines whose TRNC (stripped, upper case) is not CANX."""
    return [ln for ln in lines if ln.trnc.strip().upper() != TRNC_CANX]


def make_report(
    *, day: date, agy_no: str, range_raw: str, current_date: str, lines: Sequence[ApresLine]
) -> ApresReport:
    zero = Decimal(0)
    total_doc = sum((ln.total_doc or zero for ln in lines), zero)
    total_pb = sum((ln.pb or zero for ln in lines), zero)
    return ApresReport(
        day=day,
        agy_no=agy_no,
        range_raw=range_raw,
        current_date=current_date,
        lines=tuple(lines),
        ticket_count=len(lines),
        total_doc=total_doc,
        total_pb=total_pb,
        total_margin=total_pb - total_doc,
        unmatched=sum(1 for ln in lines if ln.sale_id is None),
    )


def amadeus_date(value: date) -> str:
    """``21-OCT-2026``, in English month abbreviations whatever the locale."""
    return f"{value.day:02d}-{MONTHS[value.month - 1]}-{value.year}"


def export_filename(day: date, *, draft: bool = False) -> str:
    """The download name, built from the date only (never from user input)."""
    return f"TJQ_APRES_{day.isoformat()}{'_BROUILLON' if draft else ''}.xlsx"


# --- the data -----------------------------------------------------------------------------------


def _latest_upload(db: Session, agency_id: int, day: date) -> Upload | None:
    return db.scalars(
        select(Upload)
        .where(Upload.agency_id == agency_id, Upload.report_date == day)
        .order_by(Upload.id.desc())
        .limit(1)
    ).first()


def _report_name(amadeus: AmadeusLine) -> str:
    """PAX NAME as the report prints it: the parser moved a trailing ``(CHD)`` / ``(INF)`` into
    ``pax_type``, so it is put back."""
    if amadeus.pax_type == "ADT":
        return amadeus.pax_name
    return f"{amadeus.pax_name} ({amadeus.pax_type})"


def _build_lines(amadeus: Sequence[AmadeusLine], views: Sequence[SaleView]) -> list[ApresLine]:
    """The lines (CANX already removed) with their sale's data. A sale ticket is used once; the
    sale's P.B sits on the first exported line of the sale and 0 on its other exported lines."""
    by_view = {v.id: v for v in views}
    by_doc = {row.doc_number: by_view[row.sale_id] for row in sales.ticket_rows(views)}
    consumed: set[str] = set()
    priced: set[int] = set()
    lines: list[ApresLine] = []
    for item in amadeus:
        doc = normalise_doc(item.doc_number, item.airline)
        view = by_doc.get(doc)
        pb: Decimal | None = None
        duplicate = False
        if view is not None and doc in consumed:
            view, duplicate = None, True
        elif view is not None:
            consumed.add(doc)
            pb = Decimal(0) if view.id in priced else view.pb
            priced.add(view.id)
        lines.append(
            ApresLine(
                seq_no=item.seq_no,
                airline=item.airline,
                doc_number=item.doc_number,
                total_doc=item.total_doc,
                pax_name=_report_name(item),
                staff_sign=item.staff_sign,
                trnc=item.trnc,
                rloc=item.rloc,
                pb=pb,
                ag=view.ag_label if view else "",
                clt=(view.clt or "") if view else "",
                ap=(view.ap or "") if view else "",
                sale_id=view.id if view else None,
                ticket_count=len(view.tickets) if view else 0,
                duplicate=duplicate,
            )
        )
    expected = sum((v.pb for v in views if v.id in priced), Decimal(0))
    if sum((ln.pb or Decimal(0) for ln in lines), Decimal(0)) != expected:
        raise AssertionError("the exported P.B does not add up to the sales' P.B")
    return lines


def apres_rows(db: Session, actor: User, report_day: date) -> ApresReport | None:
    """The TJQ APRES of ``report_day`` for the actor's agency, or None when no Amadeus report was
    uploaded for it. Raises ``PermissionError`` for a role that may not export and
    :class:`ExportError` for a report beyond ``MAX_LINES``."""
    if not permissions.can_export_day(actor):
        raise PermissionError("export of the TJQ APRES is for manager and accountant")
    agency_id = actor.agency_id
    upload = _latest_upload(db, agency_id, report_day)
    if upload is None:
        return None
    holder = repository.lines_upload_for(db, agency_id, upload.id)
    if holder is None:  # an upload from before the line snapshot: nothing to export
        return None
    amadeus = [
        a
        for a in db.scalars(
            select(AmadeusLine)
            .where(AmadeusLine.agency_id == agency_id, AmadeusLine.upload_id == holder)
            .order_by(AmadeusLine.seq_order)
        )
        if a.trnc.strip().upper() != TRNC_CANX
    ]
    if len(amadeus) > MAX_LINES:
        raise ExportError(TOO_MANY_LINES)
    views = sales.sales_for_day_for(db, actor, report_day)
    return make_report(
        day=report_day,
        agy_no="",  # the upload does not store the agency number
        range_raw=upload.report_range_raw,
        current_date=amadeus_date(upload.generated_on),
        lines=_build_lines(amadeus, views),
    )


# --- the workbook -------------------------------------------------------------------------------


def neutralise(text: str) -> str:
    """Text made safe for a spreadsheet cell: characters XML cannot hold are dropped, and a text
    whose start (blanks ignored) is ``= + - @``, tab, CR, LF, VT, FF or a no-break space gets a
    leading ``'``: it can never be read as a formula. A strict international phone number
    (``+`` and 8 to 15 digits) stays as it is."""
    clean = ILLEGAL_CHARACTERS_RE.sub("", text)
    if PHONE_E164.fullmatch(clean):
        return clean
    if clean[:1] in FORMULA_STARTS or clean.lstrip(LEADING_BLANKS)[:1] in FORMULA_STARTS:
        return "'" + clean
    return clean


def _text(ws: Worksheet, row: int, col: int, value: str, *, always: bool = False) -> None:
    clean = neutralise(value)
    if not clean and not always:
        return
    cell = ws.cell(row=row, column=col)
    cell.value = clean
    cell.data_type = "s"  # a string cell, whatever the text looks like


def _money(ws: Worksheet, row: int, col: int, value: Decimal) -> None:
    cell = ws.cell(row=row, column=col)
    cell.value = value
    cell.number_format = MONEY_FORMAT


def _note(ws: Worksheet, row: int, col: int, text: str) -> None:
    comment = Comment(ILLEGAL_CHARACTERS_RE.sub("", text), "TJQ APRES")
    comment.width, comment.height = 260, 80
    cast(Cell, ws.cell(row=row, column=col)).comment = comment


def _fill_row(ws: Worksheet, row: int, fill: PatternFill) -> None:
    for col in range(1, len(HEADERS) + 1):
        ws.cell(row=row, column=col).fill = fill


def _amount(raw: str) -> Decimal | None:
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _corrected(ws: Worksheet, row: int, col: int, change: tuple[str, str, str]) -> None:
    original, approved, who = change
    cell = ws.cell(row=row, column=col)
    if col == _COL_TOTAL and (number := _amount(approved)) is not None:
        _money(ws, row, col, number)
    else:
        _text(ws, row, col, approved, always=True)
    cell.fill = _FILL_CORRECTED
    _note(ws, row, col, f"Valeur Amadeus d'origine : {original}\nApprouvé par : {who}")


def build_apres_xlsx(report: ApresReport, *, corrections: Corrections = NO_CORRECTIONS) -> bytes:
    """The workbook of ``report`` as bytes. ``corrections`` is the B9 hook (see the module text)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None  # noqa: S101 - a new workbook always has an active sheet
    ws.title = "TJQ APRES"
    ws["A1"], ws["C1"], ws["C3"] = "Agy no", "Report date range", "Current date"
    _text(ws, 1, 2, report.agy_no)
    _text(ws, 1, 4, report.range_raw)
    _text(ws, 3, 4, report.current_date)
    for col, title in enumerate(HEADERS, start=1):
        head = ws.cell(row=HEADER_ROW, column=col, value=title)
        head.font, head.fill = Font(bold=True), _FILL_HEAD
        head.alignment = Alignment(horizontal="center")
    for n, line in enumerate(report.lines):
        row = FIRST_ROW + n
        _text(ws, row, _COL_SEQ, line.seq_no)
        _text(ws, row, 2, line.airline)
        _text(ws, row, _COL_DOC, line.doc_number)
        if line.total_doc is not None:
            _money(ws, row, _COL_TOTAL, line.total_doc)
        _text(ws, row, _COL_PAX, line.pax_name)
        _text(ws, row, 6, line.staff_sign)
        _text(ws, row, 7, line.trnc)
        _text(ws, row, 8, line.rloc)
        if line.pb is not None:
            _money(ws, row, _COL_PB, line.pb)
        _text(ws, row, _COL_AG, line.ag)
        _text(ws, row, _COL_CLT, line.clt)
        _text(ws, row, _COL_AP, line.ap)
        if line.sale_id is None:
            _fill_row(ws, row, _FILL_UNMATCHED)
            _note(ws, row, _COL_PB, DUPLICATE_NOTE if line.duplicate else UNMATCHED_NOTE)
        elif line.marker:
            _fill_row(ws, row, _FILL_GROUP)
            _note(ws, row, _COL_SEQ, line.marker)
        for col, title in enumerate(HEADERS, start=1):
            change = corrections.get((line.doc_number, title))
            if change is not None:
                _corrected(ws, row, col, change)
    total_row = FIRST_ROW + len(report.lines) + 1
    ws.cell(row=total_row, column=1, value="TOTAUX").font = Font(bold=True)
    ws.cell(row=total_row, column=_COL_DOC, value=report.ticket_count)
    _money(ws, total_row, _COL_TOTAL, report.total_doc)
    _money(ws, total_row, _COL_PB, report.total_pb)
    ws.cell(row=total_row, column=_COL_AG, value="P.B − TOTAL DOC")
    _money(ws, total_row, _COL_CLT, report.total_margin)
    for col in range(1, len(HEADERS) + 1):
        ws.cell(row=total_row, column=col).font = Font(bold=True)
    ws.freeze_panes = f"A{FIRST_ROW}"
    for letter, width in zip(
        "ABCDEFGHIJKL", (10, 6, 14, 14, 30, 10, 8, 10, 14, 16, 24, 16), strict=True
    ):
        ws.column_dimensions[letter].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# --- storage ------------------------------------------------------------------------------------


def corrections_for(db: Session, agency_id: int, report_day_id: int) -> Corrections:
    """The approved corrections of the day. TODO(B9): read them from the approved flags."""
    return NO_CORRECTIONS


def stored_versions(db: Session, agency_id: int, report_day_id: int) -> list[int]:
    """The version numbers stored for a day, oldest first."""
    return list(
        db.scalars(
            select(DayExport.version)
            .where(DayExport.agency_id == agency_id, DayExport.report_day_id == report_day_id)
            .order_by(DayExport.version)
        )
    )


def export_history(db: Session, agency_id: int, report_day_id: int) -> list[tuple[int, datetime]]:
    """``(version, created_at)`` of a day's stored exports, oldest first (never the bytes)."""
    rows = db.execute(
        select(DayExport.version, DayExport.created_at)
        .where(DayExport.agency_id == agency_id, DayExport.report_day_id == report_day_id)
        .order_by(DayExport.version)
    )
    return [(int(v), at) for v, at in rows]


def get_export(
    db: Session, agency_id: int, report_day_id: int, version: int | None
) -> DayExport | None:
    """A stored export of the day: version ``version``, or the latest when None."""
    query = select(DayExport).where(
        DayExport.agency_id == agency_id, DayExport.report_day_id == report_day_id
    )
    query = query.where(DayExport.version == version) if version is not None else query
    return db.scalars(query.order_by(DayExport.version.desc()).limit(1)).first()


def _last_version(db: Session, agency_id: int, report_day_id: int) -> int:
    last = db.scalar(
        select(func.max(DayExport.version)).where(
            DayExport.agency_id == agency_id, DayExport.report_day_id == report_day_id
        )
    )
    return last or 0


def store_next_version(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> DayExport | None:
    """Build the day's workbook and append it as the next version; None (nothing stored) when no
    Amadeus report exists for the day. The caller commits."""
    report = apres_rows(db, actor, report_day.day)
    if report is None:
        return None
    last = _last_version(db, actor.agency_id, report_day.id)
    if last >= MAX_VERSIONS:
        raise ExportError(TOO_MANY_VERSIONS)
    content = build_apres_xlsx(
        report, corrections=corrections_for(db, actor.agency_id, report_day.id)
    )
    if len(content) > MAX_BYTES:
        raise ExportError(TOO_BIG)
    row = DayExport(
        agency_id=actor.agency_id,
        report_day_id=report_day.id,
        version=last + 1,
        content=content,
        sha256=sha256(content).hexdigest(),
        created_by_user_id=actor.id,
    )
    if now is not None:
        row.created_at = now
    db.add(row)
    db.flush()
    audit.record(
        db,
        audit.DAY_EXPORT_STORED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"report_day_id": report_day.id, "version": row.version},
        now=now,
    )
    return row


def close_day_with_export(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> DayExport | None:
    """Close the day and store its TJQ APRES in the same transaction (the caller commits).

    TODO(B9): the close preconditions (no open flag, ...) go here, before ``days.close_day``."""
    if _last_version(db, actor.agency_id, report_day.id) >= MAX_VERSIONS:
        raise ExportError(TOO_MANY_VERSIONS)  # refused before anything changes
    days.close_day(db, actor, report_day, now=now, ip=ip)
    return store_next_version(db, actor, report_day, now=now, ip=ip)


__all__ = [
    "HEADERS",
    "NO_CORRECTIONS",
    "NO_REPORT",
    "XLSX_MEDIA_TYPE",
    "ApresLine",
    "MAX_BYTES",
    "MAX_LINES",
    "MAX_VERSIONS",
    "ApresReport",
    "ExportError",
    "Corrections",
    "amadeus_date",
    "apres_rows",
    "build_apres_xlsx",
    "close_day_with_export",
    "corrections_for",
    "export_history",
    "export_filename",
    "get_export",
    "make_report",
    "neutralise",
    "store_next_version",
    "stored_versions",
    "without_canx",
]
