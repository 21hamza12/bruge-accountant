"""partner aliases: other names an agency is known by in the Amadeus text

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22 18:00:00.000000

EXPAND-ONLY: one new table, nothing existing is touched, so the previous release keeps working.

* ``partner_aliases``: an alias (``alias``, and ``alias_key``, its normalised form: accents
  removed, case folded, punctuation and spaces collapsed) of one partner, proposed by an agent
  (``pending``) and decided by a manager or accountant (``approved`` or ``rejected``, with who and
  when). ``UNIQUE (agency_id, alias_key)``: one row per text per agency, so a rejected text is
  never proposed again. Composite foreign keys, ``ON DELETE RESTRICT``, to ``partners`` and (twice)
  ``users``; CHECKs: status in the list, a non-empty key, and a decision (who and when) exactly
  when the status is not ``pending``. The partner's own name is an implicit alias and is not
  stored. Rows are never deleted.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY, ``tenant_isolation`` (FOR ALL), then, when
APP_DB_ROLE names an existing role, the privileges of ``db_roles.GRANTS`` on this table only
(SELECT, INSERT, UPDATE; no DELETE). Without the variable (single-role mode) nothing is granted
and a warning is logged, as in 0003. SQLite (development and tests): a BEFORE DELETE trigger.
THE DOWNGRADE DESTROYS the aliases (the table is dropped): the paste screen then matches the
partners' own names only. It revokes the privileges on this table only.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
NOW = sa.func.now()
TENANT_ID = "app_current_agency_id()"
TABLE = "partner_aliases"
TRIGGER = "trg_partner_aliases_no_delete"
STATUSES = "'pending', 'approved', 'rejected'"
DECISION = (
    "(status = 'pending' AND decided_at IS NULL AND decided_by_user_id IS NULL) OR "
    "(status <> 'pending' AND decided_at IS NOT NULL AND decided_by_user_id IS NOT NULL)"
)


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("partner_id", BIGINT_ID, nullable=False),
        sa.Column("alias", sa.String(length=120), nullable=False),
        sa.Column("alias_key", sa.String(length=480), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("proposed_by_user_id", BIGINT_ID, nullable=False),
        sa.Column("decided_by_user_id", BIGINT_ID, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.CheckConstraint(f"status IN ({STATUSES})", name=op.f("ck_partner_aliases_status")),
        sa.CheckConstraint("alias_key <> ''", name=op.f("ck_partner_aliases_alias_key_nonempty")),
        sa.CheckConstraint(DECISION, name=op.f("ck_partner_aliases_decision_consistent")),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_partner_aliases_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "partner_id"],
            ["partners.agency_id", "partners.id"],
            name=op.f("fk_partner_aliases_agency_partner"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "proposed_by_user_id"],
            ["users.agency_id", "users.id"],
            name=op.f("fk_partner_aliases_agency_proposed_by"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "decided_by_user_id"],
            ["users.agency_id", "users.id"],
            name=op.f("fk_partner_aliases_agency_decided_by"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_partner_aliases")),
        sa.UniqueConstraint(
            "agency_id", "alias_key", name=op.f("uq_partner_aliases_agency_alias_key")
        ),
    )
    op.create_index("ix_partner_aliases_agency_status", TABLE, ["agency_id", "status"])


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_table()
    if bind.dialect.name != "postgresql":
        op.execute(
            f"CREATE TRIGGER {TRIGGER} BEFORE DELETE ON {TABLE} BEGIN "
            "SELECT RAISE(ABORT, 'partner_aliases rows are never deleted'); END"
        )
        return
    op.execute(sa.text(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"CREATE POLICY tenant_isolation ON {TABLE} "
            f"USING (agency_id = {TENANT_ID}) WITH CHECK (agency_id = {TENANT_ID})"
        )
    )
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
        op.execute(sa.text(f"DROP POLICY tenant_isolation ON {TABLE}"))
    else:
        op.execute(f"DROP TRIGGER {TRIGGER}")
    op.drop_index("ix_partner_aliases_agency_status", table_name=TABLE)
    op.drop_table(TABLE)
