"""AI usage, the AI settings of an agency, and the 'screenshot' sale source (phase D)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-23 16:00:00.000000

EXPAND-ONLY for the tables (two new ones); the one change to an existing object widens a CHECK
(``sale_versions.source`` also accepts ``screenshot``), which the previous release never writes.

* ``ai_usage``: append-only, one row for each call to the AI or refusal to make one (agency, user
  by composite foreign key, time, purpose ``screenshot``, model, tokens, ``cost_micro_usd`` as
  BIGINT, outcome ``ok`` / ``fallback`` / ``error`` / ``refused_budget``). Never any image or
  text. Index ``(agency_id, created_at)`` serves the month-to-date SUM that is the budget check.
* ``agency_ai_settings``: one row per agency at most (``agency_id`` is the primary key): ``enabled``
  and ``monthly_budget_micro_usd``. Mutable. Edited only by ``python -m app ai-budget``.
* ``sale_versions.source``: ``ck_sale_versions_source`` also allows ``screenshot``.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY on both; ``ai_usage`` has ``tenant_read`` +
``tenant_insert`` only (no UPDATE or DELETE policy), ``agency_ai_settings`` has ``tenant_isolation``
(FOR ALL). With APP_DB_ROLE the privileges of ``db_roles.GRANTS`` on these two tables only are
granted (ai_usage: SELECT, INSERT; agency_ai_settings: SELECT only, the ``ai-budget`` command writes it as the owner); without it nothing
is granted and a warning is logged, as in 0008. The CHECK is replaced with ``SET LOCAL
lock_timeout``. SQLite: BEFORE UPDATE / BEFORE DELETE triggers on ``ai_usage``; ``sale_versions``
is rebuilt (``batch_alter_table(recreate="always")``, foreign keys off, as in 0009) and its own
append-only triggers are created again.

THE DOWNGRADE IRREVERSIBLY DESTROYS the usage history and the AI settings of every agency (take a
backup first). It also puts the narrower CHECK back, which FAILS (and rolls everything back) while
a sale version with source ``screenshot`` exists: those rows are history and are never rewritten.
"""

import logging
import os
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
TABLES = ("ai_usage", "agency_ai_settings")
SOURCE_CONSTRAINT = "ck_sale_versions_source"
SOURCES_OLD = "'form', 'paste', 'comparison'"
SOURCES_NEW = "'form', 'paste', 'screenshot', 'comparison'"
SQLITE_TRIGGERS = {f"trg_ai_usage_no_{verb}": ("ai_usage", verb) for verb in ("update", "delete")}
SALE_VERSION_TRIGGERS = {
    f"trg_sale_versions_no_{verb}": ("sale_versions", verb) for verb in ("update", "delete")
}


def _created_at() -> sa.Column[datetime]:
    return sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False)


def _create_tables() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("user_id", BIGINT_ID, nullable=False),
        _created_at(),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_micro_usd", BIGINT_ID, nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.CheckConstraint("purpose IN ('screenshot')", name=op.f("ck_ai_usage_purpose")),
        sa.CheckConstraint(
            "outcome IN ('ok', 'fallback', 'error', 'refused_budget')",
            name=op.f("ck_ai_usage_outcome"),
        ),
        sa.CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_micro_usd >= 0",
            name=op.f("ck_ai_usage_nonneg"),
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_ai_usage_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "user_id"],
            ["users.agency_id", "users.id"],
            name=op.f("fk_ai_usage_agency_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_usage")),
    )
    op.create_index("ix_ai_usage_agency_created", "ai_usage", ["agency_id", "created_at"])
    op.create_index("ix_ai_usage_agency_user", "ai_usage", ["agency_id", "user_id"])
    op.create_table(
        "agency_ai_settings",
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("monthly_budget_micro_usd", BIGINT_ID, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(
            "monthly_budget_micro_usd >= 0", name=op.f("ck_agency_ai_settings_budget_nonneg")
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_agency_ai_settings_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("agency_id", name=op.f("pk_agency_ai_settings")),
    )


def _rebuild_sale_versions_sqlite(condition: str) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        bind.commit()  # the previous migration's version stamp may have left a transaction open
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        raise RuntimeError("could not switch SQLite foreign keys off for the rebuild")
    try:
        with op.batch_alter_table("sale_versions", recreate="always") as batch:
            batch.drop_constraint(op.f(SOURCE_CONSTRAINT), type_="check")
            batch.create_check_constraint(op.f(SOURCE_CONSTRAINT), sa.text(condition))
        problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"the rebuild left {len(problems)} broken foreign keys")
    finally:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _set_sources(sources: str) -> None:
    condition = f"source IN ({sources})"
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.drop_constraint(op.f(SOURCE_CONSTRAINT), "sale_versions", type_="check")
        op.create_check_constraint(op.f(SOURCE_CONSTRAINT), "sale_versions", sa.text(condition))
        return
    for name in SALE_VERSION_TRIGGERS:  # the rebuild drops the table, and its triggers with it
        op.execute(f"DROP TRIGGER {name}")
    _rebuild_sale_versions_sqlite(condition)
    for name, (table, verb) in SALE_VERSION_TRIGGERS.items():
        op.execute(
            f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON {table} BEGIN "
            f"SELECT RAISE(ABORT, '{table} is append-only'); END"
        )


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_tables()
    _set_sources(SOURCES_NEW)
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
    op.execute(f"CREATE POLICY tenant_read ON ai_usage FOR SELECT USING (agency_id = {TENANT_ID})")
    op.execute(
        f"CREATE POLICY tenant_insert ON ai_usage FOR INSERT WITH CHECK (agency_id = {TENANT_ID})"
    )
    op.execute(
        "CREATE POLICY tenant_isolation ON agency_ai_settings "
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
    # Refuse before touching anything. (On Postgres row-level security hides other agencies' rows
    # from this count; putting the narrower CHECK back then fails and rolls back by itself.)
    kept = bind.execute(
        sa.text("SELECT count(*) FROM sale_versions WHERE source = 'screenshot'")
    ).scalar()
    if kept:
        raise RuntimeError(
            f"{kept} sale version(s) have the source 'screenshot': the downgrade would orphan "
            "them. Nothing was changed."
        )
    _set_sources(SOURCES_OLD)  # first: it fails, and rolls back, while a screenshot sale exists
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=False)
        if role is not None:
            db_roles.revoke_table_grants(bind, role, TABLES)
        op.execute("DROP POLICY tenant_isolation ON agency_ai_settings")
        op.execute("DROP POLICY tenant_insert ON ai_usage")
        op.execute("DROP POLICY tenant_read ON ai_usage")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    op.drop_table("agency_ai_settings")
    op.drop_index("ix_ai_usage_agency_user", table_name="ai_usage")
    op.drop_index("ix_ai_usage_agency_created", table_name="ai_usage")
    op.drop_table("ai_usage")
