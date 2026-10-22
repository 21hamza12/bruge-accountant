"""comparison runs and comparison flags (B8 wiring)

Revision ID: 0011
Revises: 0009
Create Date: 2026-10-22 18:00:00.000000

EXPAND-ONLY: two new tables, nothing existing is touched, so the previous release keeps working.

* ``comparison_runs``: append-only. One row each time the app report of a day is compared with the
  Amadeus report (the upload that holds the line set): who started it (NULL: the system), when and
  the counts (matched, flags, blocking, cancelled, moves, partner tickets not in the report).
* ``comparison_flags``: the things to settle. ``kind`` is different / not_sent / not_in_amadeus /
  elsewhere / bad_number; ``field`` is set for ``different`` (the compared field) and ``bad_number``
  (``doc_number``) only. ``status`` is open / answered / resolved / superseded / moved. A flag is
  never deleted (no DELETE grant); ``fingerprint`` is a short hash of the ticket facts on both sides (a
  decision is kept only for the same facts); ``UNIQUE (agency_id, id)`` lets B9 reference a flag; its status changes only through ``app/flags.py``. A new run keeps
  the flag when the values did not change, supersedes it when they did.
  Postgres: a partial UNIQUE index on ``(agency_id, report_day_id, doc_number, kind,
  coalesce(field, ''))`` for the live statuses (open, answered, resolved) allows one live flag per
  key, even when two runs race. SQLite (development, tests): no such index (an expression index
  breaks the reflection tests); ``app/flags.py`` keeps the rule under the day lock.

Every reference is a composite ``(agency_id, x_id)`` foreign key, ``ON DELETE RESTRICT``.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY on both; ``tenant_read`` + ``tenant_insert`` only on
``comparison_runs`` (no UPDATE or DELETE policy), ``tenant_isolation`` on ``comparison_flags``.
When APP_DB_ROLE names an existing role, the privileges of ``db_roles.GRANTS`` on these two tables
only are granted (runs: SELECT, INSERT; flags: SELECT, INSERT, UPDATE); without the variable
(single-role mode) nothing is granted and a warning is logged, as in 0008. SQLite: BEFORE UPDATE and
BEFORE DELETE triggers on ``comparison_runs``.

THE DOWNGRADE IRREVERSIBLY DESTROYS every run and every flag with its status (the decisions of B9
live there); take a backup first. It revokes the privileges on these two tables only.
"""

import logging
import os
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0011"
# TODO rebase onto 0010 (B6 wiring partner_aliases) before merge
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
TABLES = ("comparison_runs", "comparison_flags")
KINDS = "'different', 'not_sent', 'not_in_amadeus', 'elsewhere', 'bad_number'"
FIELDS = (
    "'airline', 'total_doc', 'pax_name', 'pax_type', 'staff_sign', 'rloc', 'trnc', 'doc_number'"
)
STATUSES = "'open', 'answered', 'resolved', 'superseded', 'moved'"
LIVE_UNIQUE = "uq_comparison_flags_live_key"
SQLITE_TRIGGERS = {f"trg_comparison_runs_no_{verb}": verb for verb in ("update", "delete")}


def _created_at() -> sa.Column[datetime]:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False)


def _updated_at() -> sa.Column[datetime]:
    return sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False)


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
        "comparison_runs",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("upload_id", BIGINT_ID, nullable=False),
        sa.Column("started_by_user_id", BIGINT_ID, nullable=True),
        sa.Column("matched_count", sa.Integer(), nullable=False),
        sa.Column("flag_count", sa.Integer(), nullable=False),
        sa.Column("blocking_count", sa.Integer(), nullable=False),
        sa.Column("cancelled_count", sa.Integer(), nullable=False),
        sa.Column("move_count", sa.Integer(), nullable=False),
        sa.Column("partner_missing_count", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(
            "matched_count >= 0 AND flag_count >= 0 AND blocking_count >= 0 "
            "AND cancelled_count >= 0 AND move_count >= 0 AND partner_missing_count >= 0",
            name=op.f("ck_comparison_runs_counts_nonneg"),
        ),
        sa.CheckConstraint(
            "blocking_count <= flag_count", name=op.f("ck_comparison_runs_blocking_within_flags")
        ),
        _agency_fk("comparison_runs"),
        _composite_fk("fk_comparison_runs_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_comparison_runs_agency_upload", "upload_id", "uploads"),
        _composite_fk("fk_comparison_runs_agency_started_by", "started_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comparison_runs")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_comparison_runs_agency_id_id")),
    )
    op.create_index(
        "ix_comparison_runs_agency_day", "comparison_runs", ["agency_id", "report_day_id", "id"]
    )
    op.create_index(
        "ix_comparison_runs_agency_upload", "comparison_runs", ["agency_id", "upload_id"]
    )
    op.create_table(
        "comparison_flags",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("run_id", BIGINT_ID, nullable=False),
        sa.Column("doc_number", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("field", sa.String(length=16), nullable=True),
        sa.Column("app_value", sa.Text(), nullable=True),
        sa.Column("amadeus_value", sa.Text(), nullable=True),
        sa.Column("fingerprint", sa.String(length=16), nullable=False),
        sa.Column("sale_id", BIGINT_ID, nullable=True),
        sa.Column("amadeus_seq", sa.Integer(), nullable=True),
        sa.Column("responsible_sign", sa.String(length=32), nullable=False),
        sa.Column("needs_validation", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("blocking", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(f"kind IN ({KINDS})", name=op.f("ck_comparison_flags_kind")),
        sa.CheckConstraint(
            f"field IS NULL OR field IN ({FIELDS})", name=op.f("ck_comparison_flags_field")
        ),
        sa.CheckConstraint(
            "(kind IN ('different', 'bad_number')) = (field IS NOT NULL)",
            name=op.f("ck_comparison_flags_field_iff_different_or_bad_number"),
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_comparison_flags_status")),
        _agency_fk("comparison_flags"),
        _composite_fk("fk_comparison_flags_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_comparison_flags_agency_run", "run_id", "comparison_runs"),
        _composite_fk("fk_comparison_flags_agency_sale", "sale_id", "sales"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comparison_flags")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_comparison_flags_agency_id_id")),
    )
    op.create_index(
        "ix_comparison_flags_agency_day_status",
        "comparison_flags",
        ["agency_id", "report_day_id", "status"],
    )
    op.create_index(
        "ix_comparison_flags_agency_sign_status",
        "comparison_flags",
        ["agency_id", "responsible_sign", "status"],
    )
    op.create_index(
        "ix_comparison_flags_agency_status_blocking",
        "comparison_flags",
        ["agency_id", "status", "blocking"],
    )
    op.create_index("ix_comparison_flags_agency_run", "comparison_flags", ["agency_id", "run_id"])
    op.create_index("ix_comparison_flags_agency_sale", "comparison_flags", ["agency_id", "sale_id"])


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_tables()
    if bind.dialect.name != "postgresql":
        for name, verb in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON comparison_runs BEGIN "
                "SELECT RAISE(ABORT, 'comparison_runs is append-only'); END"
            )
        return
    op.execute(
        f"CREATE UNIQUE INDEX {LIVE_UNIQUE} ON comparison_flags "
        "(agency_id, report_day_id, doc_number, kind, coalesce(field, '')) "
        "WHERE status IN ('open', 'answered', 'resolved')"
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_read ON comparison_runs FOR SELECT USING (agency_id = {TENANT_ID})"
    )
    op.execute(
        "CREATE POLICY tenant_insert ON comparison_runs FOR INSERT "
        f"WITH CHECK (agency_id = {TENANT_ID})"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON comparison_flags "
        f"USING (agency_id = {TENANT_ID}) WITH CHECK (agency_id = {TENANT_ID})"
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
        op.execute("DROP POLICY tenant_isolation ON comparison_flags")
        op.execute("DROP POLICY tenant_insert ON comparison_runs")
        op.execute("DROP POLICY tenant_read ON comparison_runs")
        op.execute(f"DROP INDEX {LIVE_UNIQUE}")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    for index in (
        "ix_comparison_flags_agency_sale",
        "ix_comparison_flags_agency_run",
        "ix_comparison_flags_agency_status_blocking",
        "ix_comparison_flags_agency_sign_status",
        "ix_comparison_flags_agency_day_status",
    ):
        op.drop_index(index, table_name="comparison_flags")
    op.drop_table("comparison_flags")
    op.drop_index("ix_comparison_runs_agency_upload", table_name="comparison_runs")
    op.drop_index("ix_comparison_runs_agency_day", table_name="comparison_runs")
    op.drop_table("comparison_runs")
