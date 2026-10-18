"""Database access. Every query is scoped by ``agency_id``."""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models import PRICE_STATUS_AWAITING, Agency, Ticket, Upload
from app.parsing.amadeus import ParsedReport, ParsedTicket
from app.tenancy import bind_tenant, bound_tenant, lookup_scope, unbind_tenant

UPSERT_CHUNK = 200

# Amadeus-owned columns refreshed on re-import. Deliberately absent: price_status (staff
# owned), first_upload_id and created_at (history), and report_date (a ticket belongs to the
# day it first appeared; a later report never moves it to another day).
_REFRESHED_COLUMNS = (
    "seq_no",
    "airline",
    "cost",
    "passenger_name",
    "pax_type",
    "staff_sign",
    "transaction_type",
    "booking_ref",
    "flags",
    "last_upload_id",
    "updated_at",
)


@dataclass(frozen=True)
class ImportResult:
    upload_id: int
    inserted: int
    updated: int


def _next_agency_id(session: Session) -> int | None:
    """Postgres: the next id from the sequence; SQLite: None (the insert picks the id).

    Under row-level security a new agency row is only accepted when its id equals the tenant
    setting, and ``INSERT .. RETURNING`` needs the row to be readable. So the id is taken first,
    the tenant is bound to it, and only then is the row written.
    """
    if session.get_bind().dialect.name != "postgresql":
        return None
    sequence = func.pg_get_serial_sequence("agencies", "id")
    return int(session.scalar(select(func.nextval(sequence))) or 0) or None


def create_agency(session: Session, slug: str, name: str, currency: str = "MRU") -> Agency:
    """Insert an agency and leave the session bound to it. Works under FORCEd row-level security."""
    previous = bound_tenant(session)
    agency_id = _next_agency_id(session)
    if agency_id is not None:
        bind_tenant(session, agency_id)  # before the INSERT: its WITH CHECK compares the id
    agency = Agency(slug=slug, name=name, currency=currency)
    if agency_id is not None:
        agency.id = agency_id
    session.add(agency)
    try:
        session.commit()
    except Exception:
        session.rollback()
        if previous is None:
            unbind_tenant(session)
        else:
            bind_tenant(session, previous)
        raise
    bind_tenant(session, agency.id)
    return agency


def get_agency_by_slug(session: Session, slug: str) -> Agency | None:
    """Find one agency by its exact slug, before any tenant is bound (narrow lookup policy)."""
    lookup_scope(session, "app.agency_slug", slug)
    return session.scalars(select(Agency).where(Agency.slug == slug)).one_or_none()


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _upsert_statement(dialect_name: str, rows: list[dict[str, Any]]) -> Any:
    """One atomic INSERT .. ON CONFLICT DO UPDATE for a chunk of rows."""
    if dialect_name == "postgresql":
        stmt: Any = postgresql.insert(Ticket).values(rows)
    elif dialect_name == "sqlite":
        stmt = sqlite.insert(Ticket).values(rows)
    else:
        raise NotImplementedError(f"upsert is not implemented for dialect {dialect_name!r}")
    return stmt.on_conflict_do_update(
        index_elements=["agency_id", "ticket_no"],
        set_={name: getattr(stmt.excluded, name) for name in _REFRESHED_COLUMNS},
    )


def _upsert_chunk(session: Session, rows: list[dict[str, Any]]) -> None:
    session.execute(_upsert_statement(session.get_bind().dialect.name, rows))


def _row(
    parsed: ParsedTicket, agency_id: int, report_date: date, upload_id: int, now: datetime
) -> dict[str, Any]:
    return {
        "agency_id": agency_id,
        "ticket_no": parsed.ticket_no,
        "first_upload_id": upload_id,
        "last_upload_id": upload_id,
        "report_date": report_date,
        "seq_no": parsed.seq_no,
        "airline": parsed.airline,
        "cost": parsed.cost,
        "passenger_name": parsed.passenger_name,
        "pax_type": parsed.pax_type,
        "staff_sign": parsed.staff_sign,
        "transaction_type": parsed.transaction_type,
        "booking_ref": parsed.booking_ref,
        "flags": list(parsed.flags),
        "price_status": PRICE_STATUS_AWAITING,
        "created_at": now,
        "updated_at": now,
    }


def _count_existing(session: Session, agency_id: int, ticket_nos: Sequence[str]) -> int:
    stmt = (
        select(func.count())
        .select_from(Ticket)
        .where(Ticket.agency_id == agency_id, Ticket.ticket_no.in_(ticket_nos))
    )
    return session.scalar(stmt) or 0


def import_report(
    session: Session,
    agency_id: int,
    parsed: ParsedReport,
    filename: str,
    sha256: str,
    uploaded_by_user_id: int | None = None,
) -> ImportResult:
    """Store an upload and upsert its tickets in a single transaction.

    The session is bound to ``agency_id`` first (row-level security). The commit at the end
    ends the transaction and its tenant setting; the session's ``after_begin`` listener applies
    the binding again to whatever the caller reads next.

    Each chunk is one atomic upsert, so concurrent imports of the same tickets cannot
    collide; rows go in ticket-number order so concurrent writers lock in the same order.
    ``inserted``/``updated`` come from a count taken in the same transaction (exact unless
    another import commits the same tickets in between).
    """
    try:
        bind_tenant(session, agency_id)
        if session.get(Agency, agency_id) is None:
            raise LookupError(f"unknown agency id {agency_id}")

        header = parsed.header
        unique = {t.ticket_no: t for t in sorted(parsed.tickets, key=lambda t: t.ticket_no)}
        upload = Upload(
            agency_id=agency_id,
            filename=filename,
            sha256=sha256,
            report_range_raw=header.report_range_raw,
            report_date=header.report_date,
            generated_on=header.generated_on,
            ticket_count=len(unique),
            warnings=list(parsed.warnings),
            uploaded_by_user_id=uploaded_by_user_id,
        )
        session.add(upload)
        session.flush()

        now = datetime.now(UTC)
        existing = 0
        for chunk in _chunks(list(unique.values()), UPSERT_CHUNK):
            existing += _count_existing(session, agency_id, [t.ticket_no for t in chunk])
            rows = [_row(t, agency_id, header.report_date, upload.id, now) for t in chunk]
            _upsert_chunk(session, rows)

        upload_id = upload.id
        upload.inserted_count = len(unique) - existing
        upload.updated_count = existing
        session.commit()
    except Exception:
        session.rollback()
        raise
    # Core upserts bypass the identity map; refresh anything already loaded.
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Ticket):
            session.expire(obj)
    return ImportResult(upload_id=upload_id, inserted=len(unique) - existing, updated=existing)


def tickets_awaiting_price(session: Session, agency_id: int, report_date: date) -> list[Ticket]:
    bind_tenant(session, agency_id)
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
