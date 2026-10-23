"""flag decisions and day acknowledgements (B9 approvals)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-23 10:00:00.000000

EXPAND-ONLY: two new tables, nothing existing is touched, so the previous release keeps working.

Rebased onto 0012 (B10, ``day_exports``): nothing here depends on it, the chain is linear.

* ``flag_decisions``: append-only. One row for each answer of an agent (``amadeus_right``,
  ``mine_right``, ``void_entry``, ``acknowledged``) and each decision of a validator
  (``validated``, ``rejected``) on a comparison flag, with who, when, the reason and the old and
  new value. A reason of 5 characters or more is required for ``mine_right``, ``rejected`` and
  ``void_entry`` (CHECK). The stage must match the choice (CHECK).
* ``flag_decisions.sale_version_no``: the version of the sale an agent answered on.
* ``sale_versions.override_digest`` (nullable, added here): a short hash of the one-off fee, its
  reason, the partner and the tickets with their costs, written by ``app/sales.py`` from now on.
  The approvals compare it with ``day_acknowledgements.digest`` in SQL. Versions written before
  this migration keep NULL (the table is append-only, nothing is backfilled): they show as waiting
  for validation, which is the safe side.
* ``day_acknowledgements``: append-only. ``partner_list``: somebody who may validate money has
  seen the partner tickets that are not in the Amadeus report (the run and a digest of the list).
  ``override``: a one-off partner fee was approved (the sale, its version and a digest of the fee
  and its reason).

Every reference is a composite ``(agency_id, x_id)`` foreign key, ``ON DELETE RESTRICT``.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY; ``tenant_read`` + ``tenant_insert`` only (no
UPDATE or DELETE policy). When APP_DB_ROLE names an existing role, the privileges of
``db_roles.GRANTS`` on these two tables only are granted (SELECT, INSERT); without the variable
(single-role mode) nothing is granted and a warning is logged, as in 0008. SQLite: BEFORE UPDATE
and BEFORE DELETE triggers abort.

THE DOWNGRADE IRREVERSIBLY DESTROYS every decision and acknowledgement; take a backup first. It
revokes the privileges on these two tables only.
"""

import logging
import os
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
TABLES = ("flag_decisions", "day_acknowledgements")
AGENT_CHOICES = "'amadeus_right', 'mine_right', 'void_entry', 'acknowledged'"
CHOICES = f"{AGENT_CHOICES}, 'validated', 'rejected'"
SQLITE_TRIGGERS = {
    f"trg_{table}_no_{verb}": (table, verb) for table in TABLES for verb in ("update", "delete")
}


def _created_at() -> sa.Column[datetime]:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False)


def _agency_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["agency_id"],
        ["agencies.id"],
        name=op.f(f"fk_{table}_agency_id_agencies"),
        ondelete="RESTRICT",
    )


def _composite_fk(name: str, column: str, target: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["agency_id", column],
        [f"{target}.agency_id", f"{target}.id"],
        name=op.f(name),
        ondelete="RESTRICT",
    )


def _create_tables() -> None:
    op.create_table(
        "flag_decisions",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("flag_id", BIGINT_ID, nullable=False),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("choice", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("sale_version_no", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", BIGINT_ID, nullable=False),
        _created_at(),
        sa.CheckConstraint("stage IN ('agent', 'validator')", name=op.f("ck_flag_decisions_stage")),
        sa.CheckConstraint(f"choice IN ({CHOICES})", name=op.f("ck_flag_decisions_choice")),
        sa.CheckConstraint(
            f"(stage = 'agent') = (choice IN ({AGENT_CHOICES}))",
            name=op.f("ck_flag_decisions_stage_matches_choice"),
        ),
        sa.CheckConstraint(
            "NOT (choice IN ('mine_right', 'rejected', 'void_entry')) OR "
            "(reason IS NOT NULL AND length(trim(reason)) >= 5)",
            name=op.f("ck_flag_decisions_reason_required"),
        ),
        _agency_fk("flag_decisions"),
        _composite_fk("fk_flag_decisions_agency_flag", "flag_id", "comparison_flags"),
        _composite_fk("fk_flag_decisions_agency_actor", "actor_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_flag_decisions")),
    )
    op.create_index(
        "ix_flag_decisions_agency_flag", "flag_decisions", ["agency_id", "flag_id", "id"]
    )
    op.create_index(
        "ix_flag_decisions_agency_actor", "flag_decisions", ["agency_id", "actor_user_id"]
    )
    op.create_table(
        "day_acknowledgements",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("run_id", BIGINT_ID, nullable=True),
        sa.Column("sale_id", BIGINT_ID, nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=True),
        sa.Column("digest", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", BIGINT_ID, nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "kind IN ('partner_list', 'override')", name=op.f("ck_day_acknowledgements_kind")
        ),
        sa.CheckConstraint(
            "(kind = 'override') = (sale_id IS NOT NULL)",
            name=op.f("ck_day_acknowledgements_sale_iff_override"),
        ),
        sa.CheckConstraint(
            "(sale_id IS NULL) = (version_no IS NULL)",
            name=op.f("ck_day_acknowledgements_version_with_sale"),
        ),
        sa.CheckConstraint(
            "(kind = 'partner_list') = (run_id IS NOT NULL)",
            name=op.f("ck_day_acknowledgements_run_iff_list"),
        ),
        _agency_fk("day_acknowledgements"),
        _composite_fk("fk_day_acknowledgements_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_day_acknowledgements_agency_run", "run_id", "comparison_runs"),
        _composite_fk("fk_day_acknowledgements_agency_sale", "sale_id", "sales"),
        _composite_fk("fk_day_acknowledgements_agency_actor", "actor_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_day_acknowledgements")),
    )
    op.create_index(
        "ix_day_acknowledgements_agency_day",
        "day_acknowledgements",
        ["agency_id", "report_day_id", "kind", "id"],
    )
    op.create_index(
        "ix_day_acknowledgements_agency_sale", "day_acknowledgements", ["agency_id", "sale_id"]
    )
    op.create_index(
        "ix_day_acknowledgements_agency_run", "day_acknowledgements", ["agency_id", "run_id"]
    )
    op.create_index(
        "ix_day_acknowledgements_agency_actor",
        "day_acknowledgements",
        ["agency_id", "actor_user_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_tables()
    op.add_column(
        "sale_versions", sa.Column("override_digest", sa.String(length=16), nullable=True)
    )
    if bind.dialect.name != "postgresql":
        for name, (table, verb) in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
        return
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_read ON {table} FOR SELECT USING (agency_id = {TENANT_ID})"
        )
        op.execute(
            f"CREATE POLICY tenant_insert ON {table} FOR INSERT "
            f"WITH CHECK (agency_id = {TENANT_ID})"
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
        for table in TABLES:
            op.execute(f"DROP POLICY tenant_insert ON {table}")
            op.execute(f"DROP POLICY tenant_read ON {table}")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    for index in (
        "ix_day_acknowledgements_agency_actor",
        "ix_day_acknowledgements_agency_run",
        "ix_day_acknowledgements_agency_sale",
        "ix_day_acknowledgements_agency_day",
    ):
        op.drop_index(index, table_name="day_acknowledgements")
    op.drop_table("day_acknowledgements")
    op.drop_column("sale_versions", "override_digest")
    op.drop_index("ix_flag_decisions_agency_actor", table_name="flag_decisions")
    op.drop_index("ix_flag_decisions_agency_flag", table_name="flag_decisions")
    op.drop_table("flag_decisions")
