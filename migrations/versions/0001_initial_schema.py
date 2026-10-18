"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-18 05:36:38.938585

This migration has never been deployed, so it is edited in place instead of stacking a
second revision on top of it. Keep it in step with app/models.py (a test compares them).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NOW = sa.func.now()


def upgrade() -> None:
    op.create_table(
        "agencies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agencies")),
        sa.UniqueConstraint("slug", name=op.f("uq_agencies_slug")),
    )
    op.create_table(
        "uploads",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("report_range_raw", sa.String(length=32), nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("generated_on", sa.Date(), nullable=False),
        sa.Column("ticket_count", sa.Integer(), nullable=False),
        sa.Column("warnings", JSON_TYPE, nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("ticket_count >= 0", name=op.f("ck_uploads_ticket_count_nonneg")),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_uploads_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_uploads")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_uploads_agency_id_id")),
    )
    op.create_index("ix_uploads_agency_sha256", "uploads", ["agency_id", "sha256"])
    op.create_index("ix_uploads_agency_report_date", "uploads", ["agency_id", "report_date"])
    op.create_table(
        "tickets",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("first_upload_id", BIGINT_ID, nullable=False),
        sa.Column("last_upload_id", BIGINT_ID, nullable=False),
        sa.Column("report_date", sa.Date(), nullable=False),
        sa.Column("ticket_no", sa.String(length=32), nullable=False),
        sa.Column("seq_no", sa.String(length=32), nullable=False),
        sa.Column("airline", sa.String(length=8), nullable=False),
        sa.Column("cost", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("passenger_name", sa.Text(), nullable=False),
        sa.Column("pax_type", sa.String(length=3), nullable=False),
        sa.Column("staff_sign", sa.String(length=32), nullable=False),
        sa.Column("transaction_type", sa.String(length=16), nullable=False),
        sa.Column("booking_ref", sa.String(length=16), nullable=False),
        sa.Column("flags", JSON_TYPE, nullable=False),
        sa.Column("price_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("pax_type IN ('ADT', 'CHD', 'INF')", name=op.f("ck_tickets_pax_type")),
        sa.CheckConstraint(
            "price_status IN ('awaiting_price')", name=op.f("ck_tickets_price_status")
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_tickets_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "first_upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name=op.f("fk_tickets_agency_first_upload"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "last_upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name=op.f("fk_tickets_agency_last_upload"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("agency_id", "ticket_no", name="uq_tickets_agency_ticket_no"),
    )
    op.create_index(
        "ix_tickets_agency_report_date_staff",
        "tickets",
        ["agency_id", "report_date", "staff_sign"],
    )
    op.create_index("ix_tickets_agency_booking_ref", "tickets", ["agency_id", "booking_ref"])
    op.create_index("ix_tickets_agency_first_upload", "tickets", ["agency_id", "first_upload_id"])
    op.create_index("ix_tickets_agency_last_upload", "tickets", ["agency_id", "last_upload_id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_agency_last_upload", table_name="tickets")
    op.drop_index("ix_tickets_agency_first_upload", table_name="tickets")
    op.drop_index("ix_tickets_agency_booking_ref", table_name="tickets")
    op.drop_index("ix_tickets_agency_report_date_staff", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_uploads_agency_report_date", table_name="uploads")
    op.drop_index("ix_uploads_agency_sha256", table_name="uploads")
    op.drop_table("uploads")
    op.drop_table("agencies")
