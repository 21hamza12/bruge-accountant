"""the in-app chat: chat_messages (phase E, part 1)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-23 16:00:00.000000

EXPAND-ONLY: one new table, nothing existing is touched, so the previous release keeps working.

Written beside phase D (0014, ``ai_usage`` and ``agency_ai_settings``) and depends on nothing in
it; it follows 0014 now that phase D is merged.

* ``chat_messages``: append-only. One row for each message of a conversation: ``role`` is ``user``
  (what the person typed) or ``app`` (the reply), ``text`` is 1 to 4000 characters (CHECK), and
  ``(agency_id, user_id)`` is a composite foreign key to ``users``, ``ON DELETE RESTRICT``. A
  conversation belongs to its user: the application filters on the session's user, and nobody
  else (a manager included) reads it. No ticket screenshot and no pasted Amadeus text is stored.
* Index ``(agency_id, user_id, created_at)``: "the last 50 messages of this person".

Postgres: ENABLE and FORCE ROW LEVEL SECURITY; ``tenant_read`` + ``tenant_insert`` only (no UPDATE
or DELETE policy). When APP_DB_ROLE names an existing role, the privileges of ``db_roles.GRANTS``
on this table only are granted (SELECT, INSERT); without the variable (single-role mode) nothing
is granted and a warning is logged, as in 0008 and 0013. SQLite: BEFORE UPDATE and BEFORE DELETE
triggers abort.

THE DOWNGRADE IRREVERSIBLY DESTROYS every conversation; take a backup first. It revokes the
privileges on this table only.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app import db_roles
from app.db_roles import ROLE_ENV_VAR

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
TABLE = "chat_messages"
TENANT_ID = "app_current_agency_id()"
INDEX = "ix_chat_messages_agency_user_created"
SQLITE_TRIGGERS = {f"trg_{TABLE}_no_{verb}": verb for verb in ("update", "delete")}


def _create_table() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", BIGINT_ID, nullable=False),
        sa.Column("agency_id", sa.Integer(), nullable=False),
        sa.Column("user_id", BIGINT_ID, nullable=False),
        sa.Column("role", sa.String(length=8), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("role IN ('user', 'app')", name=op.f("ck_chat_messages_role")),
        sa.CheckConstraint(
            "length(text) BETWEEN 1 AND 4000", name=op.f("ck_chat_messages_text_length")
        ),
        sa.ForeignKeyConstraint(
            ["agency_id"],
            ["agencies.id"],
            name=op.f("fk_chat_messages_agency_id_agencies"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agency_id", "user_id"],
            ["users.agency_id", "users.id"],
            name=op.f("fk_chat_messages_agency_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chat_messages")),
    )
    op.create_index(INDEX, TABLE, ["agency_id", "user_id", "created_at"])


def upgrade() -> None:
    bind = op.get_bind()
    role = None
    if bind.dialect.name == "postgresql":
        role = db_roles.configured_role(
            bind, os.environ.get(ROLE_ENV_VAR, ""), must_exist=True
        )  # fail before any DDL on a bad name
    _create_table()
    if bind.dialect.name != "postgresql":
        for name, verb in SQLITE_TRIGGERS.items():
            op.execute(
                f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON {TABLE} BEGIN "
                f"SELECT RAISE(ABORT, '{TABLE} is append-only'); END"
            )
        return
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_read ON {TABLE} FOR SELECT USING (agency_id = {TENANT_ID})")
    op.execute(
        f"CREATE POLICY tenant_insert ON {TABLE} FOR INSERT WITH CHECK (agency_id = {TENANT_ID})"
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
        op.execute(f"DROP POLICY tenant_insert ON {TABLE}")
        op.execute(f"DROP POLICY tenant_read ON {TABLE}")
    else:
        for name in SQLITE_TRIGGERS:
            op.execute(f"DROP TRIGGER {name}")
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
