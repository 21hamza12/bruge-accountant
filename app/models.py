"""SQLAlchemy models. Every business row carries ``agency_id`` (multi-tenant).

There are deliberately no selling-price columns: prices arrive in Phase 3 as an
append-only history table, and ``price_status`` is never touched by imports.

Tenant safety is enforced by the schema as well as the queries: tickets reference
uploads through composite ``(agency_id, upload_id)`` foreign keys, so a ticket can never
point at another agency's upload.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, BigIntType, JSONType, UTCDateTime

PRICE_STATUS_AWAITING = "awaiting_price"
PHONE_E164_CHECK = (
    "substr(phone, 1, 1) = '+' AND length(phone) BETWEEN 9 AND 16 "
    "AND substr(phone, 2, 1) <> '0' AND rtrim(substr(phone, 2), '0123456789') = ''"
)


def _now() -> datetime:
    return datetime.now(UTC)


class Agency(Base):
    __tablename__ = "agencies"
    # Migration 0004. The slug_lookup policy compares with a setting that reads '' on a reused
    # connection, so an empty slug must never exist.
    __table_args__ = (CheckConstraint("slug <> ''", name="slug_nonempty"),)

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
        # A NULL count (not recorded yet) passes these checks; only a negative number fails.
        CheckConstraint("inserted_count >= 0", name="inserted_count_nonneg"),
        CheckConstraint("updated_count >= 0", name="updated_count_nonneg"),
        ForeignKeyConstraint(
            ["agency_id", "uploaded_by_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_uploads_agency_uploaded_by_user",
            ondelete="RESTRICT",
        ),
        Index("ix_uploads_agency_sha256", "agency_id", "sha256"),
        Index("ix_uploads_agency_report_date", "agency_id", "report_date"),
        Index("ix_uploads_agency_uploaded_by", "agency_id", "uploaded_by_user_id"),
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
    # Added by migration 0002; import_report records them (the uploader once there are logins).
    inserted_count: Mapped[int | None] = mapped_column(Integer)
    updated_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by_user_id: Mapped[int | None] = mapped_column(BigIntType)


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


ROLE_MANAGER = "manager"
ROLE_STAFF = "staff"


class User(Base):
    """A login. The phone (E.164) is unique across the whole platform (login has no agency)."""

    __tablename__ = "users"
    __table_args__ = (
        # Target of the composite foreign keys of every table that points at a user.
        UniqueConstraint("agency_id", "id", name="uq_users_agency_id_id"),
        UniqueConstraint("phone", name="uq_users_phone"),
        CheckConstraint("role IN ('manager', 'staff')", name="role"),
        # E.164: "+", then 8 to 15 digits, the first not 0. One expression for both engines: a
        # regex (Postgres ``~``) or GLOB (SQLite) would need two spellings. ``rtrim`` with a
        # character set exists in both, and is empty only when every character is a digit.
        CheckConstraint(PHONE_E164_CHECK, name="phone_e164"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    phone: Mapped[str] = mapped_column(String(20))
    role: Mapped[str] = mapped_column(String(16))
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Added by migration 0003; what the manager typed when creating the account.
    display_name: Mapped[str | None] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now, server_default=func.now()
    )


class StaffSignLink(Base):
    """Which Amadeus staff sign (``tickets.staff_sign``) belongs to which user."""

    __tablename__ = "staff_sign_links"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "user_id"],
            ["users.agency_id", "users.id"],
            name="fk_staff_sign_links_agency_user",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("agency_id", "staff_sign", name="uq_staff_sign_links_agency_staff_sign"),
        CheckConstraint("length(staff_sign) BETWEEN 2 AND 32", name="staff_sign_length"),
        Index("ix_staff_sign_links_agency_user", "agency_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    user_id: Mapped[int] = mapped_column(BigIntType)
    staff_sign: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


class UserSession(Base):
    """A server-side session; only the sha256 of the cookie token is stored."""

    __tablename__ = "user_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "user_id"],
            ["users.agency_id", "users.id"],
            name="fk_user_sessions_agency_user",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        CheckConstraint("length(token_hash) = 64", name="token_hash_length"),
        Index("ix_user_sessions_agency_user", "agency_id", "user_id"),
        Index("ix_user_sessions_absolute_expires_at", "absolute_expires_at"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    user_id: Mapped[int] = mapped_column(BigIntType)
    token_hash: Mapped[str] = mapped_column(String(64))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    idle_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    absolute_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TemporaryPassword(Base):
    """A manager-issued, single-use, short-lived password (only its Argon2 hash is stored)."""

    __tablename__ = "temporary_passwords"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "user_id"],
            ["users.agency_id", "users.id"],
            name="fk_temporary_passwords_agency_user",
            ondelete="RESTRICT",
        ),
        Index("ix_temporary_passwords_agency_user", "agency_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    user_id: Mapped[int] = mapped_column(BigIntType)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditEvent(Base):
    """Security-relevant events. ``agency_id`` is NULL for events before a tenant is known."""

    __tablename__ = "audit_events"
    __table_args__ = (
        # Both columns are nullable: the key is only enforced once an agency and actor are set.
        ForeignKeyConstraint(
            ["agency_id", "actor_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_audit_events_agency_actor",
            ondelete="RESTRICT",
        ),
        # Without an agency the foreign key above is not enforced (a NULL column skips it), so an
        # unscoped row could name any user as its actor. Only a row with an agency has an actor.
        CheckConstraint("agency_id IS NOT NULL OR actor_user_id IS NULL", name="actor_has_agency"),
        Index("ix_audit_events_agency_created_at", "agency_id", "created_at"),
        Index("ix_audit_events_created_at", "created_at"),
        # A row without an agency is never readable (row-level security, migration 0003), and
        # INSERT ... RETURNING needs SELECT on the new row. Without RETURNING, SQLAlchemy takes
        # the id from the sequence first, so the ORM can still write such a row.
        {"implicit_returning": False},
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int | None] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    actor_user_id: Mapped[int | None] = mapped_column(BigIntType)
    event_type: Mapped[str] = mapped_column(String(64))
    ip: Mapped[str | None] = mapped_column(String(45))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


class RateLimitBucket(Base):
    """Fixed-window counters shared by every instance. Not agency-scoped (keys hold no tenant)."""

    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        CheckConstraint("count >= 0", name="count_nonneg"),
        Index("ix_rate_limit_buckets_window_start", "window_start"),
        # Migration 0004: every login attempt updates a row, so keep room for HOT updates.
        {"postgresql_with": {"fillfactor": 70}},
    )

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
