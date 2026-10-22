"""day_exports: the stored TJQ APRES workbook of each close

Revision ID: 0012
Revises: 0009
Create Date: 2026-10-22 18:00:00.000000

EXPAND-ONLY: one new table, nothing existing is touched, so the previous release keeps working.

``day_exports`` is append-only: one row per close of a report day (``version`` 1, 2, ...), the
.xlsx bytes (``content``), their ``sha256`` (64 lowercase hex) and who closed. ``UNIQUE
(agency_id, report_day_id, version)`` is the version guard and also serves the lookups of a day's
versions. Composite ``(agency_id, x_id)`` foreign keys to ``report_days`` and ``users``,
``ON DELETE RESTRICT``: a row can never point into another agency, and a day with exports cannot be
deleted.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY; ``tenant_read`` and ``tenant_insert`` only (no
UPDATE or DELETE policy: not even the owner can change a row). When APP_DB_ROLE names an existing
role, ``db_roles.GRANTS`` for this table only is granted (SELECT, INSERT); without the variable
(single-role mode) nothing is granted and a warning is logged, as in 0003. SQLite (development and
tests): BEFORE UPDATE and BEFORE DELETE triggers that RAISE(ABORT).

THE DOWNGRADE IRREVERSIBLY DESTROYS every stored export (the table is dropped); take a backup first.
"""

import logging
import os
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0012"
# TODO rebase onto 0011 (B8 wiring) before merge
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
TABLE = "day_exports"
TENANT_ID = "app_current_agency_id()"
SQLITE_TRIGGERS = {f"trg_{TABLE}_no_{verb}": verb for verb in ("update", "delete")}


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _composite_fk(name: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["agency_id", column],
        [f"{target}.agency_id", f"{target}.id"],
        name=op.f(name),
        ondelete="RESTRICT",
    )


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    op.create_table(
        TABLE,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by_user_id", BIGINT_ID, nullable=False),
        _created_at(),
        sa.CheckConstraint("version >= 1", name=op.f("ck_day_exports_version_positive")),
        sa.CheckConstraint(
            "length(sha256) = 64 AND rtrim(sha256, '0123456789abcdef') = ''",
            name=op.f("ck_day_exports_sha256_hex"),
        ),
        sa.CheckConstraint(
            "length(content) > 0 AND length(content) <= 5000000",
            name=op.f("ck_day_exports_content_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_day_exports_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        _composite_fk("fk_day_exports_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_day_exports_agency_created_by", "created_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_day_exports")),
        sa.UniqueConstraint(
            "agency_id",
            "report_day_id",
            "version",
            name=op.f("uq_day_exports_agency_day_version"),
        ),
    )
    if bind.dialect.name != "postgresql":
        for name, verb in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON {TABLE} BEGIN "
                f"SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
            )
        return
    for statement in (
        f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY tenant_read ON {TABLE} FOR SELECT USING (agency_id = {TENANT_ID})",
        f"CREATE POLICY tenant_insert ON {TABLE} FOR INSERT WITH CHECK (agency_id = {TENANT_ID})",
    ):
        op.execute(sa.text(statement))
    if role is None:
        log.warning(
            "%s is not set: single-role mode. No privileges were granted on %s.",
            ROLE_ENV_VAR,
            TABLE,
        )
        return
    db_roles.apply_grants(bind, role, tables=(TABLE,))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=False)
        if role is not None:
            db_roles.revoke_table_grants(bind, role, (TABLE,))
        op.execute(sa.text(f"DROP POLICY tenant_insert ON {TABLE}"))
        op.execute(sa.text(f"DROP POLICY tenant_read ON {TABLE}"))
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    op.drop_table(TABLE)
