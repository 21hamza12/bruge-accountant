"""the agency's own details for its documents: agency_details (phase F, slice F3)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-27 18:00:00.000000

EXPAND-ONLY: one new table, nothing existing is touched, so the previous release keeps working.

* ``agency_details``: at most one row per agency (``agency_id`` is the primary key and a foreign
  key to ``agencies``, ``ON DELETE RESTRICT``). The French and Arabic names, the address, the
  phones, the e-mail, the NIF, the bank and its account (RIB), and the Bankily, Masrvi and Sedad
  numbers: all optional, each 1 to N characters when present (CHECK; the limits are
  ``app.models.AGENCY_DETAIL_LIMITS``). ``request_key`` is the key of the last form that saved
  the row; ``(agency_id, updated_by_user_id)`` is a composite foreign key to ``users``.
  A separate tenant table because the application role only reads ``agencies``.

Postgres: ENABLE and FORCE ROW LEVEL SECURITY; ``tenant_read``, ``tenant_insert`` and
``tenant_update`` (USING and WITH CHECK on the tenant); no DELETE policy. When APP_DB_ROLE names
an existing role, the privileges of ``db_roles.GRANTS`` on this table only are granted (SELECT,
INSERT, UPDATE); without the variable (single-role mode) nothing is granted and a warning is
logged, as in 0015 and 0017. SQLite: a BEFORE DELETE trigger aborts.

THE DOWNGRADE IRREVERSIBLY DESTROYS the details typed by the managers; take a backup first. It
revokes the privileges on this table only.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
TABLE = "agency_details"
TENANT_ID = "app_current_agency_id()"
# Frozen copy of app.models.AGENCY_DETAIL_LIMITS as of this revision (a migration never follows
# later edits of the models).
LIMITS = {
    "name_fr": 120,
    "name_ar": 120,
    "address": 300,
    "phones": 120,
    "email": 120,
    "nif": 40,
    "bank_name": 120,
    "bank_account": 60,
    "bankily": 40,
    "masrvi": 40,
    "sedad": 40,
}
REQUEST_KEY_LENGTH = 64
SQLITE_TRIGGER = f"trg_{TABLE}_no_delete"


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("agency_id", sa.Integer(), nullable=False),
        *(
            sa.Column(name, sa.String(length=limit), nullable=True)
            for name, limit in LIMITS.items()
        ),
        sa.Column("request_key", sa.String(length=REQUEST_KEY_LENGTH), nullable=True),
        sa.Column("updated_by_user_id", BIGINT_ID, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *(
            sa.CheckConstraint(
                f"{name} IS NULL OR length({name}) BETWEEN 1 AND {limit}",
                name=op.f(f"ck_{TABLE}_{name}_length"),
            )
            for name, limit in LIMITS.items()
        ),
        sa.CheckConstraint(
            f"request_key IS NULL OR length(request_key) BETWEEN 1 AND {REQUEST_KEY_LENGTH}",
            name=op.f(f"ck_{TABLE}_request_key_length"),
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_agency_details_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "updated_by_user_id"],
            ["users.agency_id", "users.id"],
            name=op.f("fk_agency_details_agency_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("agency_id", name=op.f("pk_agency_details")),
    )


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
            f"CREATE TRIGGER {SQLITE_TRIGGER} BEFORE DELETE ON {TABLE} BEGIN "
            f"SELECT RAISE(ABORT, '{TABLE} is never deleted'); END"
        )
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_read ON {TABLE} FOR SELECT USING (agency_id = {TENANT_ID})")
    op.execute(
        f"CREATE POLICY tenant_insert ON {TABLE} FOR INSERT WITH CHECK (agency_id = {TENANT_ID})"
    )
    op.execute(
        f"CREATE POLICY tenant_update ON {TABLE} FOR UPDATE "
        f"USING (agency_id = {TENANT_ID}) WITH CHECK (agency_id = {TENANT_ID})"
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
        op.execute(f"DROP POLICY tenant_update ON {TABLE}")
        op.execute(f"DROP POLICY tenant_insert ON {TABLE}")
        op.execute(f"DROP POLICY tenant_read ON {TABLE}")
    else:
        op.execute(f"DROP TRIGGER {SQLITE_TRIGGER}")
    op.drop_table(TABLE)
