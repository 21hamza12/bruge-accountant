"""users.role may be 'accountant'

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21 10:00:00.000000

EXPAND-ONLY for the previous release: it never writes the new value and reads roles only to
compare them with 'manager', so a database that holds an accountant is a problem for it only in
the sense that it treats that user as staff. Two visible effects there: an accountant shows as
staff, and the manager's page offers no temporary-password button for an accountant (the old
release only knows staff), so such a reset needs the new release or the CLI. Rolling back past 0005 is refused while any
accountant exists (change their role first), because the old constraint would reject them.

Postgres: ``ck_users_role`` is dropped and created again with the third value. Adding a CHECK
validates every row, so a database with a role outside the list fails loudly. Row-level security
is untouched (the policies do not look at ``role``) and no grants change.

SQLite (development and tests only): a CHECK cannot be altered in place, so ``users`` is rebuilt
with ``batch_alter_table(recreate="always")``. Other tables point at ``users`` with ON DELETE
RESTRICT, and the rebuild drops the old table, so foreign key enforcement is switched off for the
rebuild (``PRAGMA foreign_keys`` is ignored inside a transaction, hence the check that it really
went off), then ``PRAGMA foreign_key_check`` must come back empty before enforcement is switched
on again. Rows are copied as they are. The SQLite rebuild is NOT atomic (SQLite DDL is not
transactional under Alembic here): a crash half way can leave an ``_alembic_tmp_users`` table, so
this path is for development and tests only, which is all SQLite is used for. The ``bind.commit()``
below exists because ``PRAGMA foreign_keys`` is a silent no-op while a transaction is open, and the
previous migration's version stamp may have left one open; it ends that implicit transaction so the
pragma can take effect.

Postgres takes ACCESS EXCLUSIVE on ``users`` for the constraint change; ``SET LOCAL lock_timeout``
makes the migration fail fast (5 s) instead of queueing behind a long query and blocking logins.
Downgrade count under ``--sql`` (offline) is impossible, so offline downgrade is refused.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_users_role"
WITH_ACCOUNTANT = "role IN ('manager', 'accountant', 'staff')"
WITHOUT_ACCOUNTANT = "role IN ('manager', 'staff')"


def _rebuild_sqlite_users(condition: str) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        # A transaction is open (the previous migration's version stamp): close it, then retry.
        bind.commit()
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        raise RuntimeError("could not switch SQLite foreign keys off for the users rebuild")
    try:
        with op.batch_alter_table("users", recreate="always") as batch:
            batch.drop_constraint(op.f(CONSTRAINT), type_="check")
            batch.create_check_constraint(op.f(CONSTRAINT), sa.text(condition))
        problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"the users rebuild left {len(problems)} broken foreign keys")
    finally:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def _replace_postgres_check(condition: str) -> None:
    op.execute(LOCK_TIMEOUT)
    op.drop_constraint(op.f(CONSTRAINT), "users", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "users", sa.text(condition))


def _replace(condition: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_postgres_check(condition)
    else:
        _rebuild_sqlite_users(condition)


def upgrade() -> None:
    _replace(WITH_ACCOUNTANT)


def _count_accountants() -> int:
    if op.get_context().as_sql:
        raise RuntimeError(
            "the downgrade of 0005 must count accountants and cannot do that in offline (--sql) "
            "mode; run it against the database"
        )
    bind = op.get_bind()
    count_sql = sa.text("SELECT count(*) FROM users WHERE role = 'accountant'")
    if bind.dialect.name != "postgresql":
        return int(bind.execute(count_sql).scalar() or 0)
    # Row-level security is FORCEd on users, so without a bound tenant the owner sees no rows at
    # all and the count would always be 0. Lift FORCE for the count and put it back at once (the
    # ALTER takes the lock the constraint change needs anyway, and it is transactional).
    op.execute(LOCK_TIMEOUT)
    op.execute("ALTER TABLE users NO FORCE ROW LEVEL SECURITY")
    try:
        return int(bind.execute(count_sql).scalar() or 0)
    finally:
        op.execute("ALTER TABLE users FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    count = _count_accountants()
    if count:
        raise RuntimeError(
            f"cannot downgrade past 0005: {count} user(s) have the role 'accountant'. "
            "Change their role to manager or staff first."
        )
    _replace(WITHOUT_ACCOUNTANT)
