"""auth and tenancy schema (expand only)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18 12:00:00.000000

Adds users, staff sign links, sessions, temporary passwords, audit events and rate limit
buckets, and gives ``uploads`` its counts and uploader. Only additions, all nullable or new,
so the previous release keeps working against this schema (expand/contract rule). Keep it in
step with app/models.py (a test compares them).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NOW = sa.func.now()
# E.164: "+", then 8 to 15 digits, the first not 0. One portable expression: ``rtrim`` with a
# character set exists in Postgres and SQLite and is empty only when every character is a digit.
# (Frozen here on purpose; app/models.py carries the same text.)
PHONE_E164_CHECK = (
    "substr(phone, 1, 1) = '+' AND length(phone) BETWEEN 9 AND 16 "
    "AND substr(phone, 2, 1) <> '0' AND rtrim(substr(phone, 2), '0123456789') = ''"
)


def _agency_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["agency_id"],
        ["agencies.id"],
        name=op.f(f"fk_{table}_agency_id_agencies"),
        ondelete="RESTRICT",
    )


def _user_fk(name: str, columns: list[str]) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        columns,
        ["users.agency_id", "users.id"],
        name=name,
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("role IN ('manager', 'staff')", name=op.f("ck_users_role")),
        sa.CheckConstraint(PHONE_E164_CHECK, name=op.f("ck_users_phone_e164")),
        _agency_fk("users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_users_agency_id_id")),
        sa.UniqueConstraint("phone", name=op.f("uq_users_phone")),
    )
    op.create_table(
        "staff_sign_links",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("user_id", BIGINT_ID, nullable=False),
        sa.Column("staff_sign", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "length(staff_sign) BETWEEN 2 AND 32",
            name=op.f("ck_staff_sign_links_staff_sign_length"),
        ),
        _agency_fk("staff_sign_links"),
        _user_fk(op.f("fk_staff_sign_links_agency_user"), ["agency_id", "user_id"]),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_staff_sign_links")),
        sa.UniqueConstraint(
            "agency_id", "staff_sign", name=op.f("uq_staff_sign_links_agency_staff_sign")
        ),
    )
    op.create_index("ix_staff_sign_links_agency_user", "staff_sign_links", ["agency_id", "user_id"])
    op.create_table(
        "user_sessions",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("user_id", BIGINT_ID, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_user_sessions_token_hash_length")
        ),
        _agency_fk("user_sessions"),
        _user_fk(op.f("fk_user_sessions_agency_user"), ["agency_id", "user_id"]),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_sessions_token_hash")),
    )
    op.create_index("ix_user_sessions_agency_user", "user_sessions", ["agency_id", "user_id"])
    op.create_index(
        "ix_user_sessions_absolute_expires_at", "user_sessions", ["absolute_expires_at"]
    )
    op.create_table(
        "temporary_passwords",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("user_id", BIGINT_ID, nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        _agency_fk("temporary_passwords"),
        _user_fk(op.f("fk_temporary_passwords_agency_user"), ["agency_id", "user_id"]),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_temporary_passwords")),
    )
    op.create_index(
        "ix_temporary_passwords_agency_user", "temporary_passwords", ["agency_id", "user_id"]
    )
    op.create_table(
        "audit_events",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", BIGINT_ID, nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=True),
        sa.Column("details", JSON_TYPE, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "agency_id IS NOT NULL OR actor_user_id IS NULL",
            name=op.f("ck_audit_events_actor_has_agency"),
        ),
        _agency_fk("audit_events"),
        _user_fk(op.f("fk_audit_events_agency_actor"), ["agency_id", "actor_user_id"]),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        "ix_audit_events_agency_created_at", "audit_events", ["agency_id", "created_at"]
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_table(
        "rate_limit_buckets",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("count >= 0", name=op.f("ck_rate_limit_buckets_count_nonneg")),
        sa.PrimaryKeyConstraint("key", "window_start", name=op.f("pk_rate_limit_buckets")),
    )
    op.create_index("ix_rate_limit_buckets_window_start", "rate_limit_buckets", ["window_start"])

    with op.batch_alter_table("uploads") as batch:
        batch.add_column(sa.Column("inserted_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("updated_count", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("uploaded_by_user_id", BIGINT_ID, nullable=True))
        batch.create_check_constraint(
            op.f("ck_uploads_inserted_count_nonneg"), "inserted_count >= 0"
        )
        batch.create_check_constraint(op.f("ck_uploads_updated_count_nonneg"), "updated_count >= 0")
        batch.create_foreign_key(
            op.f("fk_uploads_agency_uploaded_by_user"),
            "users",
            ["agency_id", "uploaded_by_user_id"],
            ["agency_id", "id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_uploads_agency_uploaded_by", ["agency_id", "uploaded_by_user_id"])


def downgrade() -> None:
    with op.batch_alter_table("uploads") as batch:
        batch.drop_index("ix_uploads_agency_uploaded_by")
        batch.drop_constraint(op.f("fk_uploads_agency_uploaded_by_user"), type_="foreignkey")
        batch.drop_constraint(op.f("ck_uploads_updated_count_nonneg"), type_="check")
        batch.drop_constraint(op.f("ck_uploads_inserted_count_nonneg"), type_="check")
        batch.drop_column("uploaded_by_user_id")
        batch.drop_column("updated_count")
        batch.drop_column("inserted_count")
    op.drop_index("ix_rate_limit_buckets_window_start", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_agency_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_temporary_passwords_agency_user", table_name="temporary_passwords")
    op.drop_table("temporary_passwords")
    op.drop_index("ix_user_sessions_absolute_expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_agency_user", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index("ix_staff_sign_links_agency_user", table_name="staff_sign_links")
    op.drop_table("staff_sign_links")
    op.drop_table("users")
