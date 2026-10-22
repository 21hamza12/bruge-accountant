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
    false,
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


class AmadeusLine(Base):
    """One parsed line of one upload of an Amadeus report: the original report, as received.

    Append-only (migration 0006: no UPDATE/DELETE policy on Postgres, triggers on SQLite). Unlike
    ``tickets`` it is never overwritten by a later upload, so the lines of an upload can be read
    back in file order (``seq_order``). A re-upload of identical bytes for the same day stores no
    second set; ``repository.lines_upload_for`` names the upload that holds the lines.
    """

    __tablename__ = "amadeus_lines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name="fk_amadeus_lines_agency_upload",
            ondelete="RESTRICT",
        ),
        # The parser keeps the first line of a repeated DOC NUMBER: a valid parse never hits this.
        UniqueConstraint(
            "agency_id", "upload_id", "doc_number", name="uq_amadeus_lines_agency_upload_doc"
        ),
        CheckConstraint("pax_type IN ('ADT', 'CHD', 'INF')", name="pax_type"),
        CheckConstraint("seq_order >= 1", name="seq_order_positive"),
        CheckConstraint("source_row >= 1", name="source_row_positive"),
        CheckConstraint("doc_number <> ''", name="doc_number_nonempty"),
        Index("ix_amadeus_lines_agency_upload_order", "agency_id", "upload_id", "seq_order"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    upload_id: Mapped[int] = mapped_column(BigIntType)
    seq_no: Mapped[str] = mapped_column(String(32))
    seq_order: Mapped[int] = mapped_column(Integer)
    doc_number: Mapped[str] = mapped_column(String(32))
    airline: Mapped[str] = mapped_column(String(8))
    total_doc: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    comm: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    pax_name: Mapped[str] = mapped_column(Text)
    pax_type: Mapped[str] = mapped_column(String(3))
    staff_sign: Mapped[str] = mapped_column(String(32))
    rloc: Mapped[str] = mapped_column(String(16))
    trnc: Mapped[str] = mapped_column(String(16))
    form_of_payment: Mapped[str | None] = mapped_column(String(16))
    confirmed: Mapped[str | None] = mapped_column(String(32))
    flags: Mapped[list[str]] = mapped_column(JSONType)
    source_row: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


ROLE_MANAGER = "manager"
ROLE_ACCOUNTANT = "accountant"
ROLE_STAFF = "staff"


class User(Base):
    """A login. The phone (E.164) is unique across the whole platform (login has no agency)."""

    __tablename__ = "users"
    __table_args__ = (
        # Target of the composite foreign keys of every table that points at a user.
        UniqueConstraint("agency_id", "id", name="uq_users_agency_id_id"),
        UniqueConstraint("phone", name="uq_users_phone"),
        CheckConstraint("role IN ('manager', 'accountant', 'staff')", name="role"),
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


class Partner(Base):
    """A partner agency that buys tickets from this agency (AG = its name in the Amadeus report).

    ``name_key`` is the name casefolded with whitespace collapsed: unique per agency, and never
    ``bruge``, which means a direct sale. Deactivating keeps the row and its fee history.
    """

    __tablename__ = "partners"
    __table_args__ = (
        UniqueConstraint("agency_id", "id", name="uq_partners_agency_id_id"),
        UniqueConstraint("agency_id", "name_key", name="uq_partners_agency_name_key"),
        CheckConstraint("name_key <> 'bruge'", name="name_key_not_bruge"),
        CheckConstraint("name_key <> ''", name="name_key_nonempty"),
        # Same E.164 rule as users.phone; a partner may have no phone.
        CheckConstraint(f"phone IS NULL OR ({PHONE_E164_CHECK})", name="phone_e164"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(String(120))
    name_key: Mapped[str] = mapped_column(String(480))
    phone: Mapped[str | None] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now, server_default=func.now()
    )


class PartnerFee(Base):
    """One entry of a partner's fee history. Append-only: a change of fee is a new row, and the
    fee valid on a day is the row with the largest ``valid_from`` on or before it (ties: the
    latest ``created_at``, then the highest id)."""

    __tablename__ = "partner_fees"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "partner_id"],
            ["partners.agency_id", "partners.id"],
            name="fk_partner_fees_agency_partner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "set_by_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_partner_fees_agency_set_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("fee >= 0", name="fee_nonneg"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    partner_id: Mapped[int] = mapped_column(BigIntType)
    fee: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    valid_from: Mapped[date] = mapped_column(Date)
    set_by_user_id: Mapped[int] = mapped_column(BigIntType)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


# The fee lookup: the newest entry of one partner on or before a day (migration 0007).
Index(
    "ix_partner_fees_lookup",
    PartnerFee.agency_id,
    PartnerFee.partner_id,
    PartnerFee.valid_from.desc(),
    PartnerFee.created_at.desc(),
    PartnerFee.id.desc(),
)


# --- Phase B4: report days and sales (migration 0008) ----------------------------------------

DAY_OPEN = "open"
DAY_COMPARED = "compared"
DAY_APPROVED = "approved"
DAY_CLOSED = "closed"
DAY_STATUSES = (DAY_OPEN, DAY_COMPARED, DAY_APPROVED, DAY_CLOSED)
DAY_EVENT_KINDS = ("open", "close", "reopen", "compare", "approve")
SALE_ACTIVE = "active"
SALE_VOIDED = "voided"
AG_DIRECT = "direct"
AG_PARTNER = "partner"
AG_LABEL_DIRECT = "BRUGE"
PAYMENT_METHODS = ("cash", "bankily", "masrvi", "sedad", "virement", "cheque", "credit")
SALE_SOURCES = ("form", "paste", "comparison")
TICKET_TYPES = ("TKTT", "EMDA", "EMDS", "RFND", "CANX")
PAX_TYPES = ("ADT", "CHD", "INF")
MIN_REASON_LENGTH = 5


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# Same E.164 rule as users.phone, on the ``ap`` column.
AP_E164_CHECK = PHONE_E164_CHECK.replace("phone", "ap")
DOC_NUMBER_CHECK = "length(doc_number) = 10 AND rtrim(doc_number, '0123456789') = ''"
REASON_CHECK = f"reason IS NULL OR length(trim(reason)) >= {MIN_REASON_LENGTH}"


class ReportDay(Base):
    """One day of one agency: ``open`` -> ``compared`` -> ``approved`` -> ``closed``.

    Mutable (status, upload compared, closing stamp); every change of status is also a row of the
    append-only ``day_events``. B4 only opens, closes and reopens; the comparison (B8) and the
    approvals (B9) use the other statuses.
    """

    __tablename__ = "report_days"
    __table_args__ = (
        UniqueConstraint("agency_id", "id", name="uq_report_days_agency_id_id"),
        UniqueConstraint("agency_id", "day", name="uq_report_days_agency_day"),
        ForeignKeyConstraint(
            ["agency_id", "compared_upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name="fk_report_days_agency_compared_upload",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "closed_by_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_report_days_agency_closed_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_in_list("status", DAY_STATUSES), name="status"),
        CheckConstraint(
            "status <> 'closed' OR (closed_at IS NOT NULL AND closed_by_user_id IS NOT NULL)",
            name="closed_is_stamped",
        ),
        # The foreign key's column: finding the days an upload was compared on.
        Index("ix_report_days_agency_compared_upload", "agency_id", "compared_upload_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    day: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=DAY_OPEN)
    compared_upload_id: Mapped[int | None] = mapped_column(BigIntType)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    closed_by_user_id: Mapped[int | None] = mapped_column(BigIntType)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now, server_default=func.now()
    )


class DayEvent(Base):
    """Append-only: one row per change of status of a report day (who, when, why)."""

    __tablename__ = "day_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "report_day_id"],
            ["report_days.agency_id", "report_days.id"],
            name="fk_day_events_agency_report_day",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "actor_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_day_events_agency_actor",
            ondelete="RESTRICT",
        ),
        CheckConstraint(_in_list("kind", DAY_EVENT_KINDS), name="kind"),
        CheckConstraint(_in_list("to_status", DAY_STATUSES), name="to_status"),
        CheckConstraint(
            "from_status IS NULL OR " + _in_list("from_status", DAY_STATUSES), name="from_status"
        ),
        CheckConstraint(
            "kind <> 'reopen' OR (reason IS NOT NULL AND "
            f"length(trim(reason)) >= {MIN_REASON_LENGTH})",
            name="reopen_has_reason",
        ),
        Index("ix_day_events_agency_report_day", "agency_id", "report_day_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    report_day_id: Mapped[int] = mapped_column(BigIntType)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[int] = mapped_column(BigIntType)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


class Sale(Base):
    """The head of a sale: one or more tickets sold with ONE price (P.B), AG, CLT, AP, payment.

    Written only by ``app/sales.py``. The content lives in the append-only ``sale_versions``;
    ``current_version`` says which one is current. The row never moves to another day or agency.
    """

    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("agency_id", "id", name="uq_sales_agency_id_id"),
        ForeignKeyConstraint(
            ["agency_id", "report_day_id"],
            ["report_days.agency_id", "report_days.id"],
            name="fk_sales_agency_report_day",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "created_by_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_sales_agency_created_by",
            ondelete="RESTRICT",
        ),
        CheckConstraint("current_version >= 1", name="current_version_positive"),
        CheckConstraint(_in_list("status", (SALE_ACTIVE, SALE_VOIDED)), name="status"),
        CheckConstraint("length(staff_sign) BETWEEN 2 AND 32", name="staff_sign_length"),
        Index("ix_sales_agency_day_sign", "agency_id", "report_day_id", "staff_sign"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    report_day_id: Mapped[int] = mapped_column(BigIntType)
    staff_sign: Mapped[str] = mapped_column(String(32))
    created_by_user_id: Mapped[int] = mapped_column(BigIntType)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default=SALE_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, onupdate=_now, server_default=func.now()
    )


class SaleTicket(Base):
    """The tickets a sale holds now. ``UNIQUE (agency_id, doc_number)`` is the duplicate guard: a
    ticket number is in at most one sale per agency, even when two requests race.

    A ticket removed from a sale (or the tickets of a voided sale) has its row DELETED, which
    frees the number; what was there stays in the sale's versions. Only ``app/sales.py`` writes.
    """

    __tablename__ = "sale_tickets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "sale_id"],
            ["sales.agency_id", "sales.id"],
            name="fk_sale_tickets_agency_sale",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("agency_id", "doc_number", name="uq_sale_tickets_agency_doc_number"),
        UniqueConstraint(
            "agency_id", "sale_id", "position", name="uq_sale_tickets_agency_sale_position"
        ),
        CheckConstraint(DOC_NUMBER_CHECK, name="doc_number_format"),
        CheckConstraint("position >= 1", name="position_positive"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    sale_id: Mapped[int] = mapped_column(BigIntType)
    doc_number: Mapped[str] = mapped_column(String(15))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )


class SaleVersion(Base):
    """Append-only: a full snapshot of a sale at one version (including its tickets). The first
    row is the creation; every edit and the void add one; no row ever changes."""

    __tablename__ = "sale_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["agency_id", "sale_id"],
            ["sales.agency_id", "sales.id"],
            name="fk_sale_versions_agency_sale",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "report_day_id"],
            ["report_days.agency_id", "report_days.id"],
            name="fk_sale_versions_agency_report_day",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "partner_id"],
            ["partners.agency_id", "partners.id"],
            name="fk_sale_versions_agency_partner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agency_id", "actor_user_id"],
            ["users.agency_id", "users.id"],
            name="fk_sale_versions_agency_actor",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "agency_id", "sale_id", "version_no", name="uq_sale_versions_agency_sale_version"
        ),
        CheckConstraint("version_no >= 1", name="version_no_positive"),
        CheckConstraint(_in_list("ag_kind", (AG_DIRECT, AG_PARTNER)), name="ag_kind"),
        CheckConstraint(
            "(ag_kind = 'partner') = (partner_id IS NOT NULL)", name="partner_iff_partner_sale"
        ),
        CheckConstraint(
            "ag_kind <> 'partner' OR fee_applied IS NOT NULL", name="partner_sale_has_fee"
        ),
        CheckConstraint(
            "ag_kind <> 'direct' OR (clt IS NOT NULL AND length(trim(clt)) >= 1)",
            name="direct_sale_has_client",
        ),
        CheckConstraint("pb >= 0", name="pb_nonneg"),
        CheckConstraint("cost_total >= 0", name="cost_total_nonneg"),
        CheckConstraint("fee_applied IS NULL OR fee_applied >= 0", name="fee_applied_nonneg"),
        CheckConstraint(
            "fee_override_reason IS NULL OR length(trim(fee_override_reason)) >= "
            f"{MIN_REASON_LENGTH}",
            name="fee_override_reason_length",
        ),
        CheckConstraint(f"ap IS NULL OR ({AP_E164_CHECK})", name="ap_e164"),
        CheckConstraint("NOT client_refused_phone OR ap IS NULL", name="refused_phone_has_no_ap"),
        CheckConstraint(_in_list("payment_method", PAYMENT_METHODS), name="payment_method"),
        CheckConstraint(_in_list("source", SALE_SOURCES), name="source"),
        CheckConstraint(REASON_CHECK, name="reason_length"),
        CheckConstraint("length(staff_sign) BETWEEN 2 AND 32", name="staff_sign_length"),
        # The foreign key's column: a partner's sales (statements, invoices).
        Index("ix_sale_versions_agency_partner", "agency_id", "partner_id"),
    )

    id: Mapped[int] = mapped_column(BigIntType, primary_key=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="RESTRICT"))
    sale_id: Mapped[int] = mapped_column(BigIntType)
    version_no: Mapped[int] = mapped_column(Integer)
    report_day_id: Mapped[int] = mapped_column(BigIntType)
    staff_sign: Mapped[str] = mapped_column(String(32))
    ag_kind: Mapped[str] = mapped_column(String(8))
    partner_id: Mapped[int | None] = mapped_column(BigIntType)
    ag_label: Mapped[str] = mapped_column(String(120))
    # The fee row used is informational (no foreign key: partner_fees has no (agency_id, id) key
    # to reference, and the amount applied is stored right here).
    partner_fee_id: Mapped[int | None] = mapped_column(BigIntType)
    fee_applied: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    fee_override_reason: Mapped[str | None] = mapped_column(Text)
    pb: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    cost_total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    clt: Mapped[str | None] = mapped_column(Text)
    ap: Mapped[str | None] = mapped_column(String(20))
    client_refused_phone: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    payment_method: Mapped[str] = mapped_column(String(16))
    tickets: Mapped[list[dict[str, Any]]] = mapped_column(JSONType)
    voided: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    source: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[int] = mapped_column(BigIntType)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=_now, server_default=func.now()
    )
