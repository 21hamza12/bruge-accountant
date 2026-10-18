"""SQLAlchemy models. Every business row carries ``agency_id`` (multi-tenant).

There are deliberately no selling-price columns: prices arrive in Phase 3 as an
append-only history table, and ``price_status`` is never touched by imports.

Tenant safety is enforced by the schema as well as the queries: tickets reference
uploads through composite ``(agency_id, upload_id)`` foreign keys, so a ticket can never
point at another agency's upload.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, BigIntType, JSONType, UTCDateTime

PRICE_STATUS_AWAITING = "awaiting_price"


def _now() -> datetime:
    return datetime.now(UTC)


class Agency(Base):
    __tablename__ = "agencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="MRU")
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


class Upload(Base):
    __tablename__ = "uploads"
    __table_args__ = (
        # Target of the tickets' composite foreign keys.
        UniqueConstraint("agency_id", "id", name="uq_uploads_agency_id_id"),
        CheckConstraint("ticket_count >= 0", name="ticket_count_nonneg"),
        Index("ix_uploads_agency_sha256", "agency_id", "sha256"),
        Index("ix_uploads_agency_report_date", "agency_id", "report_date"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    filename: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    report_range_raw: Mapped[str] = mapped_column(String(32))
    report_date: Mapped[date] = mapped_column(Date)
    generated_on: Mapped[date] = mapped_column(Date)
    ticket_count: Mapped[int]
    warnings: Mapped[list[str]] = mapped_column(JSONType)
    uploaded_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "first_upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name="fk_tickets_agency_first_upload",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "last_upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name="fk_tickets_agency_last_upload",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("agency_id", "ticket_no", name="uq_tickets_agency_ticket_no"),
        # Named so Phase 3 can replace it with the full list of statuses.
        CheckConstraint("price_status IN ('awaiting_price')", name="price_status"),
        CheckConstraint("pax_type IN ('ADT', 'CHD', 'INF')", name="pax_type"),
        Index("ix_tickets_agency_report_date_staff", "agency_id", "report_date", "staff_sign"),
        Index("ix_tickets_agency_booking_ref", "agency_id", "booking_ref"),
        Index("ix_tickets_agency_first_upload", "agency_id", "first_upload_id"),
        Index("ix_tickets_agency_last_upload", "agency_id", "last_upload_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    first_upload_id: Mapped[int] = mapped_column(BigIntType)
    last_upload_id: Mapped[int] = mapped_column(BigIntType)
    report_date: Mapped[date] = mapped_column(Date)
    ticket_no: Mapped[str] = mapped_column(String(32))
    seq_no: Mapped[str] = mapped_column(String(32))
    airline: Mapped[str] = mapped_column(String(8))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    passenger_name: Mapped[str] = mapped_column(Text)
    pax_type: Mapped[str] = mapped_column(String(3))
    staff_sign: Mapped[str] = mapped_column(String(32))
    transaction_type: Mapped[str] = mapped_column(String(16))
    booking_ref: Mapped[str] = mapped_column(String(16))
    flags: Mapped[list[str]] = mapped_column(JSONType)
    price_status: Mapped[str] = mapped_column(String(32), default=PRICE_STATUS_AWAITING)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now, server_default=func.now()
    )
