"""report days, day events, sales, sale tickets and sale versions

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-22 10:00:00.000000

EXPAND-ONLY: five new tables, nothing existing is touched, so the previous release keeps working.

* ``report_days``: one row per agency and date (``UNIQUE (agency_id, day)``), status ``open`` /
  ``compared`` / ``approved`` / ``closed`` (a closed day carries who and when), optional composite
  foreign key to the ``uploads`` row that was compared. Mutable: ``tenant_isolation``.
* ``day_events``: append-only history of the status changes (who, when, from, to, reason; a
  reopen must carry a reason of 5 characters or more).
* ``sales``: the head of a sale (day, staff sign, ``current_version``, ``active`` / ``voided``).
  Mutable, written only by ``app/sales.py``: ``tenant_isolation``.
* ``sale_tickets``: the tickets a sale holds NOW. ``UNIQUE (agency_id, doc_number)`` is the
  duplicate guard (race-safe), ``UNIQUE (agency_id, sale_id, position)`` keeps the entry order.
  A ticket removed from a sale, or the tickets of a voided sale, are DELETED so the number is
  free again; the history is in ``sale_versions``. This is the only table of the phase with a
  DELETE grant, and it holds no history of its own. The ticket-number CHECK (8 to 15 digits) is
  written with ``rtrim`` so it is identical on Postgres and SQLite.
* ``sale_versions``: append-only full snapshot of a sale at each version (the tickets as JSON,
  money as text inside it, ``NUMERIC(14,2)`` outside). CHECKs: partner sale iff ``partner_id``
  is set and carries a fee; a direct sale has a client name; P.B and cost not negative; phone in
  E.164 or empty; payment method, source and reason length. ``partner_fee_id`` is informational
  and has no foreign key (``partner_fees`` has no ``(agency_id, id)`` key to reference, and the
  amount applied is stored beside it).

Every child reference is a composite ``(agency_id, x_id)`` foreign key, ``ON DELETE RESTRICT``, so
a row can never point into another agency (the nullable ones are not enforced while NULL).

Postgres: ENABLE and FORCE ROW LEVEL SECURITY on all five; ``tenant_isolation`` (FOR ALL) on
``report_days``, ``sales``, ``sale_tickets``; ``tenant_read`` + ``tenant_insert`` only on
``day_events`` and ``sale_versions`` (no UPDATE or DELETE policy: not even the owner can change a
row). When APP_DB_ROLE names an existing role, the privileges of ``db_roles.GRANTS`` on these five
tables only are granted (``report_days``, ``sales``: SELECT, INSERT, UPDATE; ``sale_tickets``:
SELECT, INSERT, DELETE; ``day_events``, ``sale_versions``: SELECT, INSERT). Without the variable
(single-role mode) nothing is granted and a warning is logged, as in 0003. SQLite (development
and tests): BEFORE UPDATE and BEFORE DELETE triggers on the two append-only tables.

Not in this migration, on purpose (later phases): retention or erasure of the client names and
phones kept in the append-only ``sale_versions``; column-level grants (the app role can UPDATE
any column of ``sales`` and ``report_days``) and a database trigger that refuses writes on a
closed day. Today those two rules are enforced by ``app/sales.py`` and ``app/days.py`` only.

THE DOWNGRADE IRREVERSIBLY DESTROYS every sale, its whole version history and the days with their
events (the tables are dropped); take a backup first. It revokes the privileges on these five
tables only and drops the policies, triggers and tables.
"""

import logging
import os
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
TABLES = ("report_days", "day_events", "sales", "sale_tickets", "sale_versions")
MUTABLE = ("report_days", "sales", "sale_tickets")
APPEND_ONLY = ("day_events", "sale_versions")
# Frozen here on purpose; app/models.py carries the same texts (as 0002 and 0007 do).
PHONE_E164_AP = (
    "substr(ap, 1, 1) = '+' AND length(ap) BETWEEN 9 AND 16 "
    "AND substr(ap, 2, 1) <> '0' AND rtrim(substr(ap, 2), '0123456789') = ''"
)
DOC_NUMBER_CHECK = "length(doc_number) BETWEEN 8 AND 15 AND rtrim(doc_number, '0123456789') = ''"
DAY_STATUSES = "'open', 'compared', 'approved', 'closed'"
DAY_EVENT_KINDS = "'open', 'close', 'reopen', 'compare', 'approve'"
PAYMENT_METHODS = "'cash', 'bankily', 'masrvi', 'sedad', 'virement', 'cheque', 'credit'"
SALE_SOURCES = "'form', 'paste', 'comparison'"
SQLITE_TRIGGERS = {
    f"trg_{table}_no_{verb}": (table, verb)
    for table in APPEND_ONLY
    for verb in ("update", "delete")
}


def _run(statements: Sequence[str]) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


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
        "report_days",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("compared_upload_id", BIGINT_ID, nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_user_id", BIGINT_ID, nullable=True),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(f"status IN ({DAY_STATUSES})", name=op.f("ck_report_days_status")),
        sa.CheckConstraint(
            "status <> 'closed' OR (closed_at IS NOT NULL AND closed_by_user_id IS NOT NULL)",
            name=op.f("ck_report_days_closed_is_stamped"),
        ),
        _agency_fk("report_days"),
        _composite_fk("fk_report_days_agency_compared_upload", "compared_upload_id", "uploads"),
        _composite_fk("fk_report_days_agency_closed_by", "closed_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_days")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_report_days_agency_id_id")),
        sa.UniqueConstraint("agency_id", "day", name=op.f("uq_report_days_agency_day")),
    )
    op.create_index(
        "ix_report_days_agency_compared_upload",
        "report_days",
        ["agency_id", "compared_upload_id"],
    )
    op.create_table(
        "day_events",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("from_status", sa.String(length=16), nullable=True),
        sa.Column("to_status", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", BIGINT_ID, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(f"kind IN ({DAY_EVENT_KINDS})", name=op.f("ck_day_events_kind")),
        sa.CheckConstraint(f"to_status IN ({DAY_STATUSES})", name=op.f("ck_day_events_to_status")),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({DAY_STATUSES})",
            name=op.f("ck_day_events_from_status"),
        ),
        sa.CheckConstraint(
            "kind <> 'reopen' OR (reason IS NOT NULL AND length(trim(reason)) >= 5)",
            name=op.f("ck_day_events_reopen_has_reason"),
        ),
        _agency_fk("day_events"),
        _composite_fk("fk_day_events_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_day_events_agency_actor", "actor_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_day_events")),
    )
    op.create_index(
        "ix_day_events_agency_report_day", "day_events", ["agency_id", "report_day_id", "id"]
    )
    op.create_table(
        "sales",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("staff_sign", sa.String(length=32), nullable=False),
        sa.Column("created_by_user_id", BIGINT_ID, nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("current_version >= 1", name=op.f("ck_sales_current_version_positive")),
        sa.CheckConstraint("status IN ('active', 'voided')", name=op.f("ck_sales_status")),
        sa.CheckConstraint(
            "length(staff_sign) BETWEEN 2 AND 32", name=op.f("ck_sales_staff_sign_length")
        ),
        _agency_fk("sales"),
        _composite_fk("fk_sales_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_sales_agency_created_by", "created_by_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sales")),
        sa.UniqueConstraint("agency_id", "id", name=op.f("uq_sales_agency_id_id")),
    )
    op.create_index(
        "ix_sales_agency_day_sign", "sales", ["agency_id", "report_day_id", "staff_sign"]
    )
    op.create_table(
        "sale_tickets",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("sale_id", BIGINT_ID, nullable=False),
        sa.Column("doc_number", sa.String(length=15), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        _created_at(),
        sa.CheckConstraint(DOC_NUMBER_CHECK, name=op.f("ck_sale_tickets_doc_number_format")),
        sa.CheckConstraint("position >= 1", name=op.f("ck_sale_tickets_position_positive")),
        _agency_fk("sale_tickets"),
        _composite_fk("fk_sale_tickets_agency_sale", "sale_id", "sales"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sale_tickets")),
        sa.UniqueConstraint(
            "agency_id", "doc_number", name=op.f("uq_sale_tickets_agency_doc_number")
        ),
        sa.UniqueConstraint(
            "agency_id", "sale_id", "position", name=op.f("uq_sale_tickets_agency_sale_position")
        ),
    )
    op.create_table(
        "sale_versions",
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("sale_id", BIGINT_ID, nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("report_day_id", BIGINT_ID, nullable=False),
        sa.Column("staff_sign", sa.String(length=32), nullable=False),
        sa.Column("ag_kind", sa.String(length=8), nullable=False),
        sa.Column("partner_id", BIGINT_ID, nullable=True),
        sa.Column("ag_label", sa.String(length=120), nullable=False),
        sa.Column("partner_fee_id", BIGINT_ID, nullable=True),
        sa.Column("fee_applied", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("fee_override_reason", sa.Text(), nullable=True),
        sa.Column("pb", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("cost_total", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("clt", sa.Text(), nullable=True),
        sa.Column("ap", sa.String(length=20), nullable=True),
        sa.Column("client_refused_phone", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("payment_method", sa.String(length=16), nullable=False),
        sa.Column("tickets", JSON_TYPE, nullable=False),
        sa.Column("voided", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", BIGINT_ID, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint("version_no >= 1", name=op.f("ck_sale_versions_version_no_positive")),
        sa.CheckConstraint(
            "ag_kind IN ('direct', 'partner')", name=op.f("ck_sale_versions_ag_kind")
        ),
        sa.CheckConstraint(
            "(ag_kind = 'partner') = (partner_id IS NOT NULL)",
            name=op.f("ck_sale_versions_partner_iff_partner_sale"),
        ),
        sa.CheckConstraint(
            "ag_kind <> 'partner' OR fee_applied IS NOT NULL",
            name=op.f("ck_sale_versions_partner_sale_has_fee"),
        ),
        sa.CheckConstraint(
            "ag_kind <> 'direct' OR (clt IS NOT NULL AND length(trim(clt)) >= 1)",
            name=op.f("ck_sale_versions_direct_sale_has_client"),
        ),
        sa.CheckConstraint("pb >= 0", name=op.f("ck_sale_versions_pb_nonneg")),
        sa.CheckConstraint("cost_total >= 0", name=op.f("ck_sale_versions_cost_total_nonneg")),
        sa.CheckConstraint(
            "fee_applied IS NULL OR fee_applied >= 0",
            name=op.f("ck_sale_versions_fee_applied_nonneg"),
        ),
        sa.CheckConstraint(
            "fee_override_reason IS NULL OR length(trim(fee_override_reason)) >= 5",
            name=op.f("ck_sale_versions_fee_override_reason_length"),
        ),
        sa.CheckConstraint(
            f"ap IS NULL OR ({PHONE_E164_AP})", name=op.f("ck_sale_versions_ap_e164")
        ),
        sa.CheckConstraint(
            "NOT client_refused_phone OR ap IS NULL",
            name=op.f("ck_sale_versions_refused_phone_has_no_ap"),
        ),
        sa.CheckConstraint(
            f"payment_method IN ({PAYMENT_METHODS})", name=op.f("ck_sale_versions_payment_method")
        ),
        sa.CheckConstraint(f"source IN ({SALE_SOURCES})", name=op.f("ck_sale_versions_source")),
        sa.CheckConstraint(
            "reason IS NULL OR length(trim(reason)) >= 5",
            name=op.f("ck_sale_versions_reason_length"),
        ),
        sa.CheckConstraint(
            "length(staff_sign) BETWEEN 2 AND 32", name=op.f("ck_sale_versions_staff_sign_length")
        ),
        _agency_fk("sale_versions"),
        _composite_fk("fk_sale_versions_agency_sale", "sale_id", "sales"),
        _composite_fk("fk_sale_versions_agency_report_day", "report_day_id", "report_days"),
        _composite_fk("fk_sale_versions_agency_partner", "partner_id", "partners"),
        _composite_fk("fk_sale_versions_agency_actor", "actor_user_id", "users"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sale_versions")),
        sa.UniqueConstraint(
            "agency_id", "sale_id", "version_no", name=op.f("uq_sale_versions_agency_sale_version")
        ),
    )

    op.create_index("ix_sale_versions_agency_partner", "sale_versions", ["agency_id", "partner_id"])


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_tables()
    if bind.dialect.name != "postgresql":
        for name, (table, verb) in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON {table} BEGIN "
                f"SELECT RAISE(ABORT, '{table} is append-only'); END"
            )
        return
    for table in TABLES:
        _run([f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"])
        _run([f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"])
    for table in MUTABLE:
        _run(
            [
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (agency_id = {TENANT_ID}) WITH CHECK (agency_id = {TENANT_ID})"
            ]
        )
    for table in APPEND_ONLY:
        _run(
            [
                f"CREATE POLICY tenant_read ON {table} FOR SELECT USING (agency_id = {TENANT_ID})",
                f"CREATE POLICY tenant_insert ON {table} FOR INSERT "
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
        for table in APPEND_ONLY:
            _run([f"DROP POLICY tenant_insert ON {table}", f"DROP POLICY tenant_read ON {table}"])
        for table in MUTABLE:
            _run([f"DROP POLICY tenant_isolation ON {table}"])
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    op.drop_index("ix_sale_versions_agency_partner", table_name="sale_versions")
    op.drop_table("sale_versions")
    op.drop_table("sale_tickets")
    op.drop_index("ix_sales_agency_day_sign", table_name="sales")
    op.drop_table("sales")
    op.drop_index("ix_day_events_agency_report_day", table_name="day_events")
    op.drop_table("day_events")
    op.drop_index("ix_report_days_agency_compared_upload", table_name="report_days")
    op.drop_table("report_days")
