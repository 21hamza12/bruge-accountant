"""ai_usage also logs the chat (phase E, part 2)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-23 20:00:00.000000

EXPAND-ONLY: ``ck_ai_usage_purpose`` also accepts ``chat`` (it allowed ``screenshot`` only). The
previous release never writes a ``chat`` row, so it keeps working. Nothing else changes: no column,
no index, and no grant or policy (``ai_usage`` keeps ``tenant_read`` + ``tenant_insert`` and
``SELECT, INSERT`` for the app role from 0014).

Postgres: the CHECK is replaced inside one transaction with ``SET LOCAL lock_timeout``. SQLite:
the table is rebuilt (``batch_alter_table(recreate="always")``, foreign keys off, as in 0014); its
two append-only triggers go before the rebuild and are created again after it.

THE DOWNGRADE puts the narrower CHECK back and REFUSES (nothing is changed) while a row with
purpose ``chat`` exists: that history is never rewritten or deleted. On Postgres ``ai_usage`` is
under FORCE ROW LEVEL SECURITY, so the up-front count sees only what the connecting role may read;
the database is the second guard (the narrower CHECK is validated against every row and fails, and
the transaction rolls back), which is turned here into the same plain refusal. Run it as the
migration owner role (``MIGRATION_DATABASE_URL``: superuser or ``BYPASSRLS``) to get the exact count.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_ai_usage_purpose"
PURPOSES_OLD = "'screenshot'"
PURPOSES_NEW = "'screenshot', 'chat'"
SQLITE_TRIGGERS = {f"trg_ai_usage_no_{verb}": verb for verb in ("update", "delete")}
REFUSAL = (
    "ai_usage row(s) with the purpose 'chat' exist: the downgrade would orphan them. Nothing was "
    "changed. On Postgres the count above sees only the rows the connecting role may read under "
    "row-level security; run the downgrade as the migration owner role (MIGRATION_DATABASE_URL, "
    "a superuser or a BYPASSRLS role, never the application role) to see every agency's rows. "
    "Without that role the narrower CHECK is refused by the database itself while any such row "
    "exists, with the same effect."
)


def _rebuild_sqlite(condition: str) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        bind.commit()  # the previous migration's version stamp may have left a transaction open
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        raise RuntimeError("could not switch SQLite foreign keys off for the rebuild")
    try:
        with op.batch_alter_table("ai_usage", recreate="always") as batch:
            batch.drop_constraint(op.f(CONSTRAINT), type_="check")
            batch.create_check_constraint(op.f(CONSTRAINT), sa.text(condition))
        problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"the rebuild left {len(problems)} broken foreign keys")
    finally:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _set_purposes(purposes: str) -> None:
    condition = f"purpose IN ({purposes})"
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.drop_constraint(op.f(CONSTRAINT), "ai_usage", type_="check")
        op.create_check_constraint(op.f(CONSTRAINT), "ai_usage", sa.text(condition))
        return
    for name in SQLITE_TRIGGERS:  # the rebuild drops the table, and its triggers with it
        op.execute(f"DROP TRIGGER {name}")
    _rebuild_sqlite(condition)
    for name, verb in SQLITE_TRIGGERS.items():
        op.execute(
            f"CREATE TRIGGER {name} BEFORE {verb.upper()} ON ai_usage BEGIN "
            "SELECT RAISE(ABORT, 'ai_usage is append-only'); END"
        )


def upgrade() -> None:
    _set_purposes(PURPOSES_NEW)


def downgrade() -> None:
    bind = op.get_bind()
    # Refuse before touching anything. On Postgres ``ai_usage`` is under FORCE ROW LEVEL SECURITY
    # (0014), so this count only sees the rows the connecting role may read: with no agency bound,
    # none. As in 0014, the database is the second guard: putting the narrower CHECK back is
    # validated against EVERY row and fails (the transaction rolls back, nothing changes) while a
    # 'chat' row of any agency exists.
    kept = bind.execute(sa.text("SELECT count(*) FROM ai_usage WHERE purpose = 'chat'")).scalar()
    if kept:
        raise RuntimeError(f"{kept} {REFUSAL}")
    try:
        _set_purposes(PURPOSES_OLD)
    except sa.exc.IntegrityError as exc:  # a CHECK violation: rows exist that it refuses
        if bind.dialect.name != "postgresql":
            raise
        raise RuntimeError(f"At least one {REFUSAL}") from exc
