"""SQLAlchemy models. Every business row carries ``agency_id`` (multi-tenant).

There are deliberately no selling-price columns: prices arrive in Phase 3 as an
append-only history table, and ``price_status`` is never touched by imports.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PRICE_STATUS_AWAITING = "awaiting_price"


def _now() -> datetime:
    return datetime.now(UTC)


class Agency(Base):
    __tablename__ = "agencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="MRU")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Upload(Base):
    __tablename__ = "uploads"

    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    report_range_raw: Mapped[str] = mapped_column(String(32))
    report_date: Mapped[date] = mapped_column(Date)
    generated_on: Mapped[date] = mapped_column(Date)
    ticket_count: Mapped[int]
    warnings: Mapped[list[str]] = mapped_column(JSON)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("agency_id", "ticket_no", name="uq_tickets_agency_ticket_no"),
        Index("ix_tickets_agency_report_date_staff", "agency_id", "report_date", "staff_sign"),
        Index("ix_tickets_agency_booking_ref", "agency_id", "booking_ref"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id"))
    first_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    last_upload_id: Mapped[int] = mapped_column(ForeignKey("uploads.id"))
    report_date: Mapped[date] = mapped_column(Date)
    ticket_no: Mapped[str] = mapped_column(String(32))
    seq_no: Mapped[str] = mapped_column(String(32))
    airline: Mapped[str] = mapped_column(String(8))
    cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    passenger_name: Mapped[str] = mapped_column(String(200))
    pax_type: Mapped[str] = mapped_column(String(3))
    staff_sign: Mapped[str] = mapped_column(String(32))
    transaction_type: Mapped[str] = mapped_column(String(16))
    booking_ref: Mapped[str] = mapped_column(String(16))
    flags: Mapped[list[str]] = mapped_column(JSON)
    price_status: Mapped[str] = mapped_column(String(32), default=PRICE_STATUS_AWAITING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
