"""amadeus_lines: the append-only original Amadeus report, one set of lines per upload

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21 12:00:00.000000

EXPAND-ONLY: a new table nobody reads yet; the previous release ignores it. A release that does
not write it simply leaves it empty.

``amadeus_lines`` keeps every parsed line of every upload exactly as received. ``tickets`` cannot do
that (it upserts on the ticket number and overwrites ``last_upload_id``), so later steps (the daily
comparison, the TJQ APRES) read this table instead.

* ``(agency_id, upload_id)`` is a composite foreign key to ``uploads(agency_id, id)`` (the unique
  key ``uq_uploads_agency_id_id`` already exists), ``ON DELETE RESTRICT``.
* ``UNIQUE (agency_id, upload_id, doc_number)``: the parser keeps the first of two lines with the
  same DOC NUMBER, so a valid parse never violates it.
* Append-only. Postgres: row-level security ENABLEd and FORCEd with ``tenant_read`` (SELECT) and
  ``tenant_insert`` (INSERT) and no UPDATE or DELETE policy, so even the owner cannot change or
  remove a row. The app role gets SELECT and INSERT only (``db_roles.GRANTS``). SQLite (development
  and tests): ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers that abort.
* Append-only is fully enforced only in app-role mode (``APP_DB_ROLE`` set): the app role has no
  UPDATE, DELETE or TRUNCATE privilege. In single-role mode the connecting role owns the table
  and can still TRUNCATE it, DROP POLICY or DISABLE ROW LEVEL SECURITY; FORCE RLS and the missing
  policies only stop ordinary statements. Production must use the separate app role
  (docs/runbook.md section 3). No BEFORE TRUNCATE trigger is installed: the owner could drop it.
* Deleting an upload or an agency is refused once lines exist (``ON DELETE RESTRICT`` plus
  append-only). A privileged, audited retention or erasure path is a later-phase decision.
* Identical re-uploads (same sha256 and report date) store no second line set; see
  ``repository.lines_upload_for``.
* Grants go to the app role (``APP_DB_ROLE``) for this table only; without the variable nothing is
  granted (single-role mode, as in 0003).

Downgrade drops the table (with its policies, indexes and triggers); the stored lines are lost.
"""

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "amadeus_lines"
BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TENANT_ID = "app_current_agency_id()"
SQLITE_TRIGGERS = {
    "trg_amadeus_lines_no_update": "BEFORE UPDATE",
    "trg_amadeus_lines_no_delete": "BEFORE DELETE",
}
APPEND_ONLY_MESSAGE = "amadeus_lines is append-only"


def upgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    role = (
        db_roles.configured_role(bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True)
        if postgres
        else None
    )  # fail before any DDL on a bad role

    op.create_table(
        TABLE,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("upload_id", BIGINT_ID, nullable=False),
        sa.Column("seq_no", sa.String(length=32), nullable=False),
        sa.Column("seq_order", sa.Integer(), nullable=False),
        sa.Column("doc_number", sa.String(length=32), nullable=False),
        sa.Column("airline", sa.String(length=8), nullable=False),
        sa.Column("total_doc", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("tax", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("comm", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("pax_name", sa.Text(), nullable=False),
        sa.Column("pax_type", sa.String(length=3), nullable=False),
        sa.Column("staff_sign", sa.String(length=32), nullable=False),
        sa.Column("rloc", sa.String(length=16), nullable=False),
        sa.Column("trnc", sa.String(length=16), nullable=False),
        sa.Column("form_of_payment", sa.String(length=16), nullable=True),
        sa.Column("confirmed", sa.String(length=32), nullable=True),
        sa.Column("flags", JSON_TYPE, nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "pax_type IN ('ADT', 'CHD', 'INF')", name=op.f("ck_amadeus_lines_pax_type")
        ),
        sa.CheckConstraint("seq_order >= 1", name=op.f("ck_amadeus_lines_seq_order_positive")),
        sa.CheckConstraint("source_row >= 1", name=op.f("ck_amadeus_lines_source_row_positive")),
        sa.CheckConstraint("doc_number <> ''", name=op.f("ck_amadeus_lines_doc_number_nonempty")),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_amadeus_lines_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "upload_id"],
            ["uploads.agency_id", "uploads.id"],
            name=op.f("fk_amadeus_lines_agency_upload"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_amadeus_lines")),
        sa.UniqueConstraint(
            "agency_id", "upload_id", "doc_number", name="uq_amadeus_lines_agency_upload_doc"
        ),
    )
    op.create_index(
        "ix_amadeus_lines_agency_upload_order", TABLE, ["agency_id", "upload_id", "seq_order"]
    )

    if not postgres:
        for name, when in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} {when} ON {TABLE} "
                f"BEGIN SELECT RAISE(ABORT, '{APPEND_ONLY_MESSAGE}'); END"
            )
        return

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_read ON {TABLE} FOR SELECT USING (agency_id = {TENANT_ID})")
    op.execute(
        f"CREATE POLICY tenant_insert ON {TABLE} FOR INSERT WITH CHECK (agency_id = {TENANT_ID})"
    )
    if role is not None:
        db_roles.apply_grants(bind, role, tables=(TABLE,))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(f"DROP POLICY tenant_insert ON {TABLE}")
        op.execute(f"DROP POLICY tenant_read ON {TABLE}")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    op.drop_index("ix_amadeus_lines_agency_upload_order", table_name=TABLE)
    op.drop_table(TABLE)
