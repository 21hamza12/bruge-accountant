"""partner accounts: periods, manual lines, their decisions and payments (phase F, slice F1)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-27 12:00:00.000000

EXPAND-ONLY: four new tables, nothing existing is touched, so the previous release keeps working.

* ``partner_periods``: one half-month of one partner (``start_date`` the 1st and ``end_date`` the
  15th, or the 16th and the last day of the same month: CHECKs), ``open`` or ``closed``.
  ``UNIQUE (agency_id, partner_id, start_date)`` is also the period lookup. The frozen-at-close
  columns (``opening_balance``, ``sales_total``, ``entries_total``, ``payments_total``,
  ``closing_balance``, ``closed_at``, ``closed_by_user_id``, ``snapshot``) are all NULL while
  open and all set once closed (CHECK). Closing is the one UPDATE of a row: A CLOSED ROW NEVER
  CHANGES. Postgres: ``tenant_update`` only matches open rows, and there is no DELETE policy.
  SQLite: a BEFORE UPDATE trigger aborts on a closed row, a BEFORE DELETE trigger always aborts.
* ``partner_entries``: append-only manual lines (``OPENING``, ``ADM``, ``RFND``, ``CHANGEMENT``,
  ``AJUSTEMENT``, ``CORRECTION``) with a signed amount (CHECK per kind), a reason of 5 characters
  or more, a source (``manual``, ``amadeus``, ``system``), an optional sale (``CORRECTION``) and
  ticket number (``RFND``). At most one ``OPENING`` per partner: a partial unique index.
* ``partner_entry_decisions``: append-only; the one decision (``approve``, ``confirm``,
  ``reject``) on an entry, ``UNIQUE (agency_id, entry_id)``.
* ``partner_payments``: append-only; amount > 0, a method of the payment list except ``credit``,
  an optional reference of at most 64 characters, who received and who recorded it.
* Indexes: ``(agency_id, partner_id, entry_date)`` and ``(agency_id, partner_id, paid_on)`` for
  the account and the statement; ``(agency_id, sale_id)`` for the corrections of a sale.

Every reference is a composite ``(agency_id, x_id)`` foreign key, ``ON DELETE RESTRICT``.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY on the four. ``tenant_read`` + ``tenant_insert``
on all; ``partner_periods`` also gets ``tenant_update`` (open rows only). When APP_DB_ROLE names an
existing role, the privileges of ``db_roles.GRANTS`` on these four tables only are granted
(``SELECT, INSERT, UPDATE`` on the periods, ``SELECT, INSERT`` on the rest); without the variable
(single-role mode) nothing is granted and a warning is logged, as in 0013 and 0015. SQLite: the
three append-only tables get BEFORE UPDATE and BEFORE DELETE triggers that abort.

The day CHECKs need ``date + 1 day``, which has no portable SQL: ``_DayAfter`` compiles it for
each database (as ``app.models`` does; tests/test_migration_0017.py compares the two).

THE DOWNGRADE IRREVERSIBLY DESTROYS every period, line, decision and payment; take a backup
first. It revokes the privileges on these four tables only.
"""

import logging
import os
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.compiler import SQLCompiler
from sqlalchemy.sql.functions import FunctionElement

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
MONEY = sa.Numeric(14, 2)
JSON_TYPE = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
PERIODS = "partner_periods"
APPEND_ONLY = ("partner_entries", "partner_entry_decisions", "partner_payments")
TABLES = (PERIODS, *APPEND_ONLY)
MIN_REASON_LENGTH = 5  # app.models.MIN_REASON_LENGTH, frozen here
DOC_NUMBER_CHECK = "length(doc_number) = 10 AND rtrim(doc_number, '0123456789') = ''"
KINDS = "'OPENING', 'ADM', 'RFND', 'CHANGEMENT', 'AJUSTEMENT', 'CORRECTION'"
SOURCES = "'manual', 'amadeus', 'system'"
DECISIONS = "'approve', 'confirm', 'reject'"
METHODS = "'cash', 'bankily', 'masrvi', 'sedad', 'virement', 'cheque'"
FROZEN = (
    "opening_balance",
    "sales_total",
    "entries_total",
    "payments_total",
    "closing_balance",
    "closed_at",
    "closed_by_user_id",
    "snapshot",
)
INDEXES = {
    "ix_partner_entries_agency_partner_date": (
        "partner_entries",
        ["agency_id", "partner_id", "entry_date"],
    ),
    "ix_partner_entries_agency_sale": ("partner_entries", ["agency_id", "sale_id"]),
    "ix_partner_payments_agency_partner_paid_on": (
        "partner_payments",
        ["agency_id", "partner_id", "paid_on"],
    ),
}
ONE_OPENING = "ix_partner_entries_one_opening"
SQLITE_TRIGGERS = {
    f"trg_{table}_no_{verb}": (
        f"BEFORE {verb.upper()} ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )
    for table in APPEND_ONLY
    for verb in ("update", "delete")
} | {
    f"trg_{PERIODS}_closed_no_update": (
        f"BEFORE UPDATE ON {PERIODS} WHEN OLD.status = 'closed' BEGIN "
        f"SELECT RAISE(ABORT, 'a closed {PERIODS} row never changes'); END"
    ),
    f"trg_{PERIODS}_no_delete": (
        f"BEFORE DELETE ON {PERIODS} BEGIN "
        f"SELECT RAISE(ABORT, '{PERIODS} rows are never deleted'); END"
    ),
}


class _DayAfter(FunctionElement[date]):
    type = sa.Date()
    inherit_cache = True
    name = "day_after_0017"


@compiles(_DayAfter)
def _day_after_default(element: _DayAfter, compiler: SQLCompiler, **kw: Any) -> str:
    return f"({compiler.process(element.clauses, **kw)} + 1)"


@compiles(_DayAfter, "sqlite")
def _day_after_sqlite(element: _DayAfter, compiler: SQLCompiler, **kw: Any) -> str:
    return f"date({compiler.process(element.clauses, **kw)}, '+1 day')"


def _day(column: Any) -> Any:
    return sa.extract("day", column)


def _period_dates_check() -> Any:
    start, end = sa.column("start_date", sa.Date()), sa.column("end_date", sa.Date())
    return sa.and_(
        sa.and_(
            sa.extract("year", start) == sa.extract("year", end),
            sa.extract("month", start) == sa.extract("month", end),
        ),
        sa.or_(
            sa.and_(_day(start) == 1, _day(end) == 15),
            sa.and_(_day(start) == 16, _day(_DayAfter(end)) == 1),
        ),
    )


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


def _check(table: str, name: str, sqltext: Any) -> sa.CheckConstraint:
    return sa.CheckConstraint(sqltext, name=op.f(f"ck_{table}_{name}"))


def _create_periods() -> None:
    t = PERIODS
    unset = " AND ".join(f"{c} IS NULL" for c in FROZEN)
    stamped = " AND ".join(f"{c} IS NOT NULL" for c in FROZEN)
    op.create_table(
        t,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", BIGINT_ID, nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=8), nullable=False),
        sa.Column("opening_balance", MONEY, nullable=True),
        sa.Column("sales_total", MONEY, nullable=True),
        sa.Column("entries_total", MONEY, nullable=True),
        sa.Column("payments_total", MONEY, nullable=True),
        sa.Column("closing_balance", MONEY, nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_user_id", BIGINT_ID, nullable=True),
        sa.Column("snapshot", JSON_TYPE, nullable=True),
        _created_at(),
        _check(t, "status", "status IN ('open', 'closed')"),
        _check(t, "start_day", _day(sa.column("start_date")).in_([1, 16])),
        _check(t, "end_date", _period_dates_check()),
        _check(
            t,
            "frozen_iff_closed",
            f"(status = 'open' AND {unset}) OR (status = 'closed' AND {stamped})",
        ),
        _agency_fk(t),
        _composite_fk("fk_partner_periods_agency_partner", "partner_id", "partners"),
        _composite_fk("fk_partner_periods_agency_closed_by", "closed_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_periods")),
        sa.UniqueConstraint(
            "agency_id",
            "partner_id",
            "start_date",
            name=op.f("uq_partner_periods_agency_partner_start"),
        ),
    )


def _create_entries() -> None:
    t = "partner_entries"
    op.create_table(
        t,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", BIGINT_ID, nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("sale_id", BIGINT_ID, nullable=True),
        sa.Column("doc_number", sa.String(length=15), nullable=True),
        sa.Column("created_by_user_id", BIGINT_ID, nullable=False),
        _created_at(),
        _check(t, "kind", f"kind IN ({KINDS})"),
        _check(t, "source", f"source IN ({SOURCES})"),
        _check(
            t,
            "amount_sign",
            "(kind NOT IN ('ADM', 'CHANGEMENT') OR amount > 0) "
            "AND (kind <> 'RFND' OR amount < 0) "
            "AND (kind NOT IN ('AJUSTEMENT', 'CORRECTION') OR amount <> 0)",
        ),
        _check(t, "reason_length", f"length(trim(reason)) >= {MIN_REASON_LENGTH}"),
        _check(t, "correction_is_system", "(kind = 'CORRECTION') = (source = 'system')"),
        _check(
            t,
            "amadeus_is_refund_with_ticket",
            "source <> 'amadeus' OR (kind = 'RFND' AND doc_number IS NOT NULL)",
        ),
        _check(t, "sale_only_on_correction", "sale_id IS NULL OR kind = 'CORRECTION'"),
        _check(t, "doc_number_only_on_refund", "doc_number IS NULL OR kind = 'RFND'"),
        _check(t, "doc_number_format", f"doc_number IS NULL OR ({DOC_NUMBER_CHECK})"),
        _agency_fk(t),
        _composite_fk("fk_partner_entries_agency_partner", "partner_id", "partners"),
        _composite_fk("fk_partner_entries_agency_sale", "sale_id", "sales"),
        _composite_fk("fk_partner_entries_agency_created_by", "created_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_entries")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_partner_entries_agency_id_id")),
    )
    op.create_index(
        ONE_OPENING,
        t,
        ["agency_id", "partner_id"],
        unique=True,
        sqlite_where=sa.column("kind") == "OPENING",
        postgresql_where=sa.column("kind") == "OPENING",
    )


def _create_decisions() -> None:
    t = "partner_entry_decisions"
    op.create_table(
        t,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("entry_id", BIGINT_ID, nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("decided_by_user_id", BIGINT_ID, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
        _check(t, "decision", f"decision IN ({DECISIONS})"),
        _agency_fk(t),
        _composite_fk("fk_partner_entry_decisions_agency_entry", "entry_id", "partner_entries"),
        _composite_fk(
            "fk_partner_entry_decisions_agency_decided_by", "decided_by_user_id", "users"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_entry_decisions")),
        sa.UniqueConstraint(
            "agency_id", "entry_id", name=op.f("uq_partner_entry_decisions_agency_entry")
        ),
    )


def _create_payments() -> None:
    t = "partner_payments"
    op.create_table(
        t,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", BIGINT_ID, nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("reference", sa.String(length=64), nullable=True),
        sa.Column("received_by_user_id", BIGINT_ID, nullable=False),
        sa.Column("recorded_by_user_id", BIGINT_ID, nullable=False),
        _created_at(),
        _check(t, "amount_positive", "amount > 0"),
        _check(t, "method", f"method IN ({METHODS})"),
        _check(
            t,
            "reference_length",
            "reference IS NULL OR length(trim(reference)) BETWEEN 1 AND 64",
        ),
        _agency_fk(t),
        _composite_fk("fk_partner_payments_agency_partner", "partner_id", "partners"),
        _composite_fk("fk_partner_payments_agency_received_by", "received_by_user_id", "users"),
        _composite_fk("fk_partner_payments_agency_recorded_by", "recorded_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_payments")),
    )


def _create_tables() -> None:
    _create_periods()
    _create_entries()
    _create_decisions()
    _create_payments()
    for name, (table, columns) in INDEXES.items():
        op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_tables()
    if bind.dialect.name != "postgresql":
        for name, body in SQLITE_TRIGGERS.items():
            op.execute(f"CREATE TRIGGER {name} {body}")
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
    # Closing a period is the one UPDATE: only an open row of the tenant can be its target.
    op.execute(
        f"CREATE POLICY tenant_update ON {PERIODS} FOR UPDATE "
        f"USING (agency_id = {TENANT_ID} AND status = 'open') "
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
        op.execute(f"DROP POLICY tenant_update ON {PERIODS}")
        for table in TABLES:
            op.execute(f"DROP POLICY tenant_insert ON {table}")
            op.execute(f"DROP POLICY tenant_read ON {table}")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    for name, (table, _columns) in INDEXES.items():
        op.drop_index(name, table_name=table)
    op.drop_index(ONE_OPENING, table_name="partner_entries")
    op.drop_table("partner_payments")
    op.drop_table("partner_entry_decisions")
    op.drop_table("partner_entries")
    op.drop_table(PERIODS)
