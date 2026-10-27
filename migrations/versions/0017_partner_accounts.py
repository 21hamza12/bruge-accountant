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

Guards added after the F1 review (edited in place: 0017 was never released):

* A period is born open (``tenant_insert`` WITH CHECK ``status = 'open'``; SQLite trigger). The
  close is its only UPDATE: ``tenant_update`` WITH CHECK ``status = 'closed'``, and a BEFORE
  UPDATE trigger (plpgsql; SQLite trigger) refuses any change of ``id``, ``agency_id``,
  ``partner_id``, ``start_date``, ``end_date``, ``created_at`` and any update of a row that is not
  open or that does not close it. Postgres only: CHECK ``closing_balance = opening_balance +
  sales_total + entries_total - payments_total`` once closed (SQLite stores NUMERIC as a float).
* Nothing lands in or before a closed period: BEFORE INSERT triggers on ``partner_entries`` (by
  ``entry_date``), ``partner_payments`` (by ``paid_on``) and ``partner_entry_decisions`` (by its
  entry's date) refuse a date on or before the ``end_date`` of a closed period of the same
  partner. On Postgres they first take a shared advisory lock per partner, so a concurrent close
  (which takes the exclusive one) and an insert never cross (see ``PG_FUNCTIONS``). Every plpgsql
  function pins its ``search_path``.
* A decision fits its entry: ``entry_kind`` and ``entry_source`` are copied from the entry and
  held to it by the foreign key ``(agency_id, entry_id, entry_kind, entry_source)`` ->
  ``partner_entries (agency_id, id, kind, source)``; a CHECK admits only approve / reject of an
  ``AJUSTEMENT`` and confirm / reject of an ``amadeus`` ``RFND``.
* ``request_key`` (nullable, at most 64 characters) on lines and payments, unique per agency
  where set (partial unique indexes): F2's double-click guard.

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
# F2's idempotency keys: one per agency and table, rows without one are not limited.
REQUEST_KEY_INDEXES = {
    "ix_partner_entries_request_key": "partner_entries",
    "ix_partner_payments_request_key": "partner_payments",
}
REQUEST_KEY_CHECK = "request_key IS NULL OR length(request_key) BETWEEN 1 AND 64"
DECISION_FITS_ENTRY = (
    "(entry_kind = 'AJUSTEMENT' AND decision IN ('approve', 'reject')) OR "
    "(entry_kind = 'RFND' AND entry_source = 'amadeus' AND decision IN ('confirm', 'reject'))"
)
# Postgres only: SQLite stores NUMERIC as a float, so an exact equality there is not reliable.
CLOSING_RECONCILES = (
    "status <> 'closed' OR "
    "closing_balance = opening_balance + sales_total + entries_total - payments_total"
)
# The columns of a period that never change, not even when it closes.
IDENTITY = ("id", "agency_id", "partner_id", "start_date", "end_date", "created_at")
CLOSED_MESSAGE = "a closed partner period ends on or after this date"
CHANGE_MESSAGE = "a partner period changes only by its close"
# pg_advisory_xact_lock(_shared) key: this constant namespace + the partner id folded into an
# integer. Two partners that fold to the same key only wait for each other, never wrongly pass.
ADVISORY_NAMESPACE = 170017
PG_SEARCH_PATH = "SET search_path = pg_catalog, public, pg_temp"


def _closed_on_or_after(agency: str, partner: str, day: str) -> str:
    """A closed period of the partner that ends on or after ``day``: nothing may be dated on or
    before the end of the latest closed period, even in a half-month that never had a row (a
    late line goes into the open period as a correction). DDL text for trigger bodies; the
    arguments are constant column or parameter names written in this file, never data."""
    return (
        f"SELECT 1 FROM {PERIODS} p WHERE p.agency_id = {agency} AND p.partner_id = {partner} "  # noqa: S608
        f"AND p.status = 'closed' AND p.end_date >= {day}"
    )


_IDENTITY_CHANGED = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in IDENTITY)
_PG_IDENTITY_CHANGED = " OR ".join(f"NEW.{c} IS DISTINCT FROM OLD.{c}" for c in IDENTITY)
SQLITE_TRIGGERS = {
    f"trg_{table}_no_{verb}": (
        f"BEFORE {verb.upper()} ON {table} BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
    )
    for table in APPEND_ONLY
    for verb in ("update", "delete")
} | {
    f"trg_{PERIODS}_born_open": (
        f"BEFORE INSERT ON {PERIODS} WHEN NEW.status <> 'open' BEGIN "
        f"SELECT RAISE(ABORT, 'a partner period is born open'); END"
    ),
    # The close is the only update: an open row, closed, same identity (Postgres: the trigger
    # function below plus tenant_update).
    f"trg_{PERIODS}_only_the_close": (
        f"BEFORE UPDATE ON {PERIODS} "
        f"WHEN OLD.status <> 'open' OR NEW.status <> 'closed' OR {_IDENTITY_CHANGED} BEGIN "
        f"SELECT RAISE(ABORT, '{CHANGE_MESSAGE}'); END"
    ),
    f"trg_{PERIODS}_no_delete": (
        f"BEFORE DELETE ON {PERIODS} BEGIN "
        f"SELECT RAISE(ABORT, '{PERIODS} rows are never deleted'); END"
    ),
    "trg_partner_entries_period_open": (
        "BEFORE INSERT ON partner_entries "
        f"WHEN EXISTS ({_closed_on_or_after('NEW.agency_id', 'NEW.partner_id', 'NEW.entry_date')}) "
        f"BEGIN SELECT RAISE(ABORT, '{CLOSED_MESSAGE}'); END"
    ),
    "trg_partner_payments_period_open": (
        "BEFORE INSERT ON partner_payments "
        f"WHEN EXISTS ({_closed_on_or_after('NEW.agency_id', 'NEW.partner_id', 'NEW.paid_on')}) "
        f"BEGIN SELECT RAISE(ABORT, '{CLOSED_MESSAGE}'); END"
    ),
    "trg_partner_entry_decisions_period_open": (
        "BEFORE INSERT ON partner_entry_decisions WHEN EXISTS ("  # noqa: S608 - constant DDL
        "SELECT 1 FROM partner_entries e WHERE e.agency_id = NEW.agency_id "
        "AND e.id = NEW.entry_id AND EXISTS ("
        f"{_closed_on_or_after('e.agency_id', 'e.partner_id', 'e.entry_date')})) "
        f"BEGIN SELECT RAISE(ABORT, '{CLOSED_MESSAGE}'); END"
    ),
}

# Postgres: trigger functions in plpgsql, SECURITY INVOKER (the default), so they read under the
# caller's row-level security and privileges. Each one pins its search_path (pg_catalog, public,
# then pg_temp last), so a temporary table cannot shadow partner_periods or partner_entries. No
# bind-parameter colon and no percent sign in them: Alembic sends each one through text().
#
# partner_period_assert_open: no line, payment or decision may be dated on or before the end of a
# closed period of its partner. The race with a concurrent close, including a period whose row
# does not exist yet (built on demand), is settled by a transaction-scoped advisory lock per
# partner, key (ADVISORY_NAMESPACE, partner id folded into an integer):
#   1. take the SHARED lock: inserts of the same partner run side by side, and wait while a close
#      of that partner holds the EXCLUSIVE lock;
#   2. then check, as a fresh statement (READ COMMITTED: a new snapshot), that no closed period of
#      the partner ends on or after the date. A close that committed while we waited is seen.
# F2's close MUST take pg_advisory_xact_lock (EXCLUSIVE) with the same key BEFORE it reads the
# totals: it then waits for every insert in flight to commit, and its totals include them. A key
# shared by two partners only makes them wait for each other; it never lets a line through.
# The FOR SHARE lock on the open period row (the first fix) is dropped: the advisory lock covers
# every case it covered and the one it missed (no row yet, so nothing to lock), and it no longer
# needs the UPDATE privilege nor the update policy's USING on a read. Under REPEATABLE READ or
# SERIALIZABLE the check would run on the transaction's first snapshot: the application uses
# READ COMMITTED (the default).
PG_FUNCTIONS = {
    "partner_period_assert_open(integer, bigint, date)": f"""
CREATE FUNCTION partner_period_assert_open(p_agency integer, p_partner bigint, p_day date)
RETURNS void LANGUAGE plpgsql {PG_SEARCH_PATH} AS $$
BEGIN
    PERFORM pg_advisory_xact_lock_shared({ADVISORY_NAMESPACE}, CAST(mod(p_partner, 2147483647) AS integer));
    IF EXISTS ({_closed_on_or_after("p_agency", "p_partner", "p_day")}) THEN
        RAISE EXCEPTION '{CLOSED_MESSAGE}' USING ERRCODE = 'check_violation';
    END IF;
END $$""",
    "partner_entries_period_open()": f"""
CREATE FUNCTION partner_entries_period_open() RETURNS trigger LANGUAGE plpgsql {PG_SEARCH_PATH} AS $$
BEGIN
    PERFORM partner_period_assert_open(NEW.agency_id, NEW.partner_id, NEW.entry_date);
    RETURN NEW;
END $$""",
    "partner_payments_period_open()": f"""
CREATE FUNCTION partner_payments_period_open() RETURNS trigger LANGUAGE plpgsql {PG_SEARCH_PATH} AS $$
BEGIN
    PERFORM partner_period_assert_open(NEW.agency_id, NEW.partner_id, NEW.paid_on);
    RETURN NEW;
END $$""",
    # An entry that is not visible (another agency, or none) is left to the foreign key.
    "partner_entry_decisions_period_open()": f"""
CREATE FUNCTION partner_entry_decisions_period_open() RETURNS trigger
LANGUAGE plpgsql {PG_SEARCH_PATH} AS $$
DECLARE
    line record;
BEGIN
    SELECT e.partner_id, e.entry_date INTO line
      FROM partner_entries e WHERE e.agency_id = NEW.agency_id AND e.id = NEW.entry_id;
    IF FOUND THEN
        PERFORM partner_period_assert_open(NEW.agency_id, line.partner_id, line.entry_date);
    END IF;
    RETURN NEW;
END $$""",  # noqa: S608 - constant DDL, no data
    "partner_periods_only_the_close()": f"""
CREATE FUNCTION partner_periods_only_the_close() RETURNS trigger
LANGUAGE plpgsql {PG_SEARCH_PATH} AS $$
BEGIN
    IF OLD.status <> 'open' OR NEW.status <> 'closed' OR {_PG_IDENTITY_CHANGED} THEN
        RAISE EXCEPTION '{CHANGE_MESSAGE}' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$""",
}
PG_TRIGGERS = {
    "trg_partner_entries_period_open": (
        "partner_entries",
        "BEFORE INSERT",
        "partner_entries_period_open()",
    ),
    "trg_partner_payments_period_open": (
        "partner_payments",
        "BEFORE INSERT",
        "partner_payments_period_open()",
    ),
    "trg_partner_entry_decisions_period_open": (
        "partner_entry_decisions",
        "BEFORE INSERT",
        "partner_entry_decisions_period_open()",
    ),
    "trg_partner_periods_only_the_close": (
        PERIODS,
        "BEFORE UPDATE",
        "partner_periods_only_the_close()",
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


def _create_periods(postgres: bool) -> None:
    t = PERIODS
    unset = " AND ".join(f"{c} IS NULL" for c in FROZEN)
    stamped = " AND ".join(f"{c} IS NOT NULL" for c in FROZEN)
    postgres_only = [_check(t, "closing_reconciles", CLOSING_RECONCILES)] if postgres else []
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
        *postgres_only,
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
        sa.Column("request_key", sa.String(length=64), nullable=True),
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
        _check(t, "request_key_length", REQUEST_KEY_CHECK),
        _agency_fk(t),
        _composite_fk("fk_partner_entries_agency_partner", "partner_id", "partners"),
        _composite_fk("fk_partner_entries_agency_sale", "sale_id", "sales"),
        _composite_fk("fk_partner_entries_agency_created_by", "created_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_entries")),
        sa.UniqueConstraint(
            "agency_id",
            "id",
            "kind",
            "source",
            name=op.f("uq_partner_entries_agency_id_kind_source"),
        ),
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
        sa.Column("entry_kind", sa.String(length=16), nullable=False),
        sa.Column("entry_source", sa.String(length=8), nullable=False),
        sa.Column("decision", sa.String(length=8), nullable=False),
        sa.Column("decided_by_user_id", BIGINT_ID, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        _created_at(),
        _check(t, "decision", f"decision IN ({DECISIONS})"),
        _check(t, "decision_fits_entry", DECISION_FITS_ENTRY),
        _agency_fk(t),
        # The entry's own kind and source, so the CHECK above judges the real entry.
        sa.ForeignKeyConstraint(
            ["agency_id", "entry_id", "entry_kind", "entry_source"],
            [
                "partner_entries.agency_id",
                "partner_entries.id",
                "partner_entries.kind",
                "partner_entries.source",
            ],
            name=op.f("fk_partner_entry_decisions_agency_entry"),
            ondelete="RESTRICT",
        ),
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
        sa.Column("request_key", sa.String(length=64), nullable=True),
        _created_at(),
        _check(t, "amount_positive", "amount > 0"),
        _check(t, "method", f"method IN ({METHODS})"),
        _check(
            t,
            "reference_length",
            "reference IS NULL OR length(trim(reference)) BETWEEN 1 AND 64",
        ),
        _check(t, "request_key_length", REQUEST_KEY_CHECK),
        _agency_fk(t),
        _composite_fk("fk_partner_payments_agency_partner", "partner_id", "partners"),
        _composite_fk("fk_partner_payments_agency_received_by", "received_by_user_id", "users"),
        _composite_fk("fk_partner_payments_agency_recorded_by", "recorded_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_payments")),
    )


def _create_tables(postgres: bool) -> None:
    _create_periods(postgres)
    _create_entries()
    _create_decisions()
    _create_payments()
    for name, (table, columns) in INDEXES.items():
        op.create_index(name, table, columns)
    for name, table in REQUEST_KEY_INDEXES.items():
        op.create_index(
            name,
            table,
            ["agency_id", "request_key"],
            unique=True,
            sqlite_where=sa.column("request_key").is_not(None),
            postgresql_where=sa.column("request_key").is_not(None),
        )


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    postgres = bind.dialect.name == "postgresql"
    _create_tables(postgres)
    if not postgres:
        for name, body in SQLITE_TRIGGERS.items():
            op.execute(f"CREATE TRIGGER {name} {body}")
        return
    for definition in PG_FUNCTIONS.values():
        op.execute(definition)
    for name, (table, when, function) in PG_TRIGGERS.items():
        op.execute(
            f"CREATE TRIGGER {name} {when} ON {table} FOR EACH ROW EXECUTE FUNCTION {function}"
        )
    for table in TABLES:
        # A period is born open; the other tables only need the tenant.
        born_open = " AND status = 'open'" if table == PERIODS else ""
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_read ON {table} FOR SELECT USING (agency_id = {TENANT_ID})"
        )
        op.execute(
            f"CREATE POLICY tenant_insert ON {table} FOR INSERT "
            f"WITH CHECK (agency_id = {TENANT_ID}{born_open})"
        )
    # Closing a period is the one UPDATE: an open row of the tenant in, a closed row out (the
    # trigger above also keeps its identity).
    op.execute(
        f"CREATE POLICY tenant_update ON {PERIODS} FOR UPDATE "
        f"USING (agency_id = {TENANT_ID} AND status = 'open') "
        f"WITH CHECK (agency_id = {TENANT_ID} AND status = 'closed')"
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
        for name, (table, _when, _function) in PG_TRIGGERS.items():
            op.execute(f"DROP TRIGGER {name} ON {table}")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    for name, (table, _columns) in INDEXES.items():
        op.drop_index(name, table_name=table)
    for name, table in REQUEST_KEY_INDEXES.items():
        op.drop_index(name, table_name=table)
    op.drop_index(ONE_OPENING, table_name="partner_entries")
    op.drop_table("partner_payments")
    op.drop_table("partner_entry_decisions")
    op.drop_table("partner_entries")
    op.drop_table(PERIODS)
    if bind.dialect.name == "postgresql":
        # The trigger functions go after their triggers; the helper after the functions.
        for signature in reversed(PG_FUNCTIONS):
            op.execute(f"DROP FUNCTION {signature}")
