"""Database access. Every query is scoped by ``agency_id``."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PRICE_STATUS_AWAITING, Agency, Ticket, Upload
from app.parsing.amadeus import ParsedReport, ParsedTicket

LOOKUP_CHUNK = 500


@dataclass(frozen=True)
class ImportResult:
    upload_id: int
    inserted: int
    updated: int


def create_agency(session: Session, slug: str, name: str, currency: str = "MRU") -> Agency:
    agency = Agency(slug=slug, name=name, currency=currency)
    session.add(agency)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    return agency


def get_agency_by_slug(session: Session, slug: str) -> Agency | None:
    return session.scalars(select(Agency).where(Agency.slug == slug)).one_or_none()


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _apply(ticket: Ticket, parsed: ParsedTicket, report_date: date, upload_id: int) -> None:
    """Copy Amadeus-owned fields only; price_status is deliberately not touched."""
    ticket.report_date = report_date
    ticket.seq_no = parsed.seq_no
    ticket.airline = parsed.airline
    ticket.cost = parsed.cost
    ticket.passenger_name = parsed.passenger_name
    ticket.pax_type = parsed.pax_type
    ticket.staff_sign = parsed.staff_sign
    ticket.transaction_type = parsed.transaction_type
    ticket.booking_ref = parsed.booking_ref
    ticket.flags = list(parsed.flags)
    ticket.last_upload_id = upload_id


def import_report(
    session: Session,
    agency_id: int,
    parsed: ParsedReport,
    filename: str,
    sha256: str,
) -> ImportResult:
    """Store an upload and upsert its tickets in a single transaction."""
    try:
        if session.get(Agency, agency_id) is None:
            raise LookupError(f"unknown agency id {agency_id}")

        header = parsed.header
        unique: dict[str, ParsedTicket] = {}
        for item in parsed.tickets:
            unique.setdefault(item.ticket_no, item)

        upload = Upload(
            agency_id=agency_id,
            filename=filename,
            sha256=sha256,
            report_range_raw=header.report_range_raw,
            report_date=header.report_date,
            generated_on=header.generated_on,
            ticket_count=len(unique),
            warnings=list(parsed.warnings),
        )
        session.add(upload)
        session.flush()

        existing: dict[str, Ticket] = {}
        for chunk in _chunks(list(unique), LOOKUP_CHUNK):
            stmt = select(Ticket).where(Ticket.agency_id == agency_id, Ticket.ticket_no.in_(chunk))
            for row in session.scalars(stmt):
                existing[row.ticket_no] = row

        now = datetime.now(UTC)
        inserted = updated = 0
        for ticket_no, item in unique.items():
            current = existing.get(ticket_no)
            if current is None:
                current = Ticket(
                    agency_id=agency_id,
                    ticket_no=ticket_no,
                    first_upload_id=upload.id,
                    price_status=PRICE_STATUS_AWAITING,
                )
                _apply(current, item, header.report_date, upload.id)
                session.add(current)
                inserted += 1
            else:
                _apply(current, item, header.report_date, upload.id)
                current.updated_at = now
                updated += 1

        session.commit()
        return ImportResult(upload_id=upload.id, inserted=inserted, updated=updated)
    except BaseException:
        session.rollback()
        raise


def tickets_awaiting_price(session: Session, agency_id: int, report_date: date) -> list[Ticket]:
    stmt = (
        select(Ticket)
        .where(
            Ticket.agency_id == agency_id,
            Ticket.report_date == report_date,
            Ticket.price_status == PRICE_STATUS_AWAITING,
        )
        .order_by(Ticket.staff_sign, Ticket.booking_ref, Ticket.ticket_no)
    )
    return list(session.scalars(stmt))
