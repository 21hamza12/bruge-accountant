"""partners and their fee history

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21 14:00:00.000000

EXPAND-ONLY: two new tables, nothing existing is touched, so the previous release keeps working.

* ``partners``: a partner agency per tenant. ``UNIQUE (agency_id, name_key)`` (the name casefolded
  with whitespace collapsed), ``name_key <> 'bruge'`` (BRUGE means a direct sale), and the same
  E.164 CHECK as ``users.phone`` on the optional phone. Mutable, so ``tenant_isolation``.
* ``partner_fees``: append-only fee history; composite foreign keys to ``partners`` and ``users``
  (``ON DELETE RESTRICT``). Postgres: ``tenant_read`` and ``tenant_insert`` policies and no
  UPDATE or DELETE policy, so a row can neither be edited nor removed, not even by the owner
  (FORCE ROW LEVEL SECURITY). SQLite (development and tests only): BEFORE UPDATE and BEFORE
  DELETE triggers that RAISE(ABORT).

Postgres only for row-level security and grants: ENABLE and FORCE on both tables, then, when
APP_DB_ROLE names an existing role, the privileges of ``db_roles.GRANTS`` on these two tables only
(``partners``: SELECT, INSERT, UPDATE; ``partner_fees``: SELECT, INSERT). Without the variable
(single-role mode) nothing is granted and a warning is logged, as in 0003. The role must not be a
superuser or have BYPASSRLS. THE DOWNGRADE IRREVERSIBLY DESTROYS the partners and their whole
fee history (the tables are dropped); take a backup first. The downgrade revokes the privileges on the same two tables only (the role keeps its access to
the older ones) and drops everything.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
TABLES = ("partners", "partner_fees")
# Frozen here on purpose; app/models.py carries the same text (as 0002 does).
PHONE_E164_CHECK = (
    "substr(phone, 1, 1) = '+' AND length(phone) BETWEEN 9 AND 16 "
    "AND substr(phone, 2, 1) <> '0' AND rtrim(substr(phone, 2), '0123456789') = ''"
)
APPEND_ONLY_MESSAGE = "partner_fees is append-only"
SQLITE_TRIGGERS = {
    "trg_partner_fees_no_update": "BEFORE UPDATE ON partner_fees",
    "trg_partner_fees_no_delete": "BEFORE DELETE ON partner_fees",
}


def _run(statements: Sequence[str]) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


def _create_tables() -> None:
    op.create_table(
        "partners",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("name_key", sa.String(length=480), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("name_key <> 'bruge'", name=op.f("ck_partners_name_key_not_bruge")),
        sa.CheckConstraint("name_key <> ''", name=op.f("ck_partners_name_key_nonempty")),
        sa.CheckConstraint(
            f"phone IS NULL OR ({PHONE_E164_CHECK})", name=op.f("ck_partners_phone_e164")
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_partners_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partners")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_partners_agency_id_id")),
        sa.UniqueConstraint("agency_id", "name_key", name=op.f("uq_partners_agency_name_key")),
    )
    op.create_table(
        "partner_fees",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", BIGINT_ID, nullable=False),
        sa.Column("fee", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("set_by_user_id", BIGINT_ID, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint("fee >= 0", name=op.f("ck_partner_fees_fee_nonneg")),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_partner_fees_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "partner_id"],
            ["partners.agency_id", "partners.id"],
            name=op.f("fk_partner_fees_agency_partner"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "set_by_user_id"],
            ["users.agency_id", "users.id"],
            name=op.f("fk_partner_fees_agency_set_by"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_fees")),
    )
    op.create_index(
        "ix_partner_fees_lookup",
        "partner_fees",
        [
            "agency_id",
            "partner_id",
            sa.column("valid_from").desc(),
            sa.column("created_at").desc(),
            sa.column("id").desc(),
        ],
    )


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_tables()
    if bind.dialect.name != "postgresql":
        for name, when in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} {when} BEGIN "
                f"SELECT RAISE(ABORT, '{APPEND_ONLY_MESSAGE}'); END"
            )
        return
    for table in TABLES:
        _run([f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"])
        _run([f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"])
    _run(
        [
            "CREATE POLICY tenant_isolation ON partners "
            f"USING (agency_id = {TENANT_ID}) WITH CHECK (agency_id = {TENANT_ID})",
            f"CREATE POLICY tenant_read ON partner_fees FOR SELECT USING (agency_id = {TENANT_ID})",
            "CREATE POLICY tenant_insert ON partner_fees FOR INSERT "
            f"WITH CHECK (agency_id = {TENANT_ID})",
        ]
    )
    if role is None:
        log.warning(
            "%s is not set: single-role mode. No privileges were granted on %s.",
            ROLE_ENV_VAR,
            ", ".join(TABLES),
        )
        return
    db_roles.apply_grants(bind, role, tables=TABLES)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=False)
        if role is not None:
            db_roles.revoke_table_grants(bind, role, TABLES)
        _run(
            [
                "DROP POLICY tenant_insert ON partner_fees",
                "DROP POLICY tenant_read ON partner_fees",
                "DROP POLICY tenant_isolation ON partners",
            ]
        )
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    op.drop_index("ix_partner_fees_lookup", table_name="partner_fees")
    op.drop_table("partner_fees")
    op.drop_table("partners")
