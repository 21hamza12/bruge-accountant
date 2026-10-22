"""sale_tickets.doc_number is exactly 10 digits

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22 15:00:00.000000

The Amadeus export holds the 10-digit ticket number (3940299438) and the comparison matches on it,
so a sale must store the same 10 digits; the airline prefix (495-3940299438) is cut off by the
application before it reaches the database (``sales_rules._validate_ticket``). 0008 allowed 8 to 15
digits; this tightens the CHECK to exactly 10. Adding a CHECK validates every row, so a database
that holds another length fails loudly (the tables are new and no release wrote such a number).

Postgres: drop and create ``ck_sale_tickets_doc_number_format`` (``SET LOCAL lock_timeout`` makes
it fail fast). Row-level security and grants are untouched. SQLite (development and tests): the
table is rebuilt with ``batch_alter_table(recreate="always")`` with foreign keys off, as in 0005.
Downgrade restores the 8 to 15 digit rule, which every 10-digit number satisfies.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "sale_tickets"
CONSTRAINT = "ck_sale_tickets_doc_number_format"
DIGITS = "rtrim(doc_number, '0123456789') = ''"
TEN = f"length(doc_number) = 10 AND {DIGITS}"
WIDE = f"length(doc_number) BETWEEN 8 AND 15 AND {DIGITS}"


def _rebuild_sqlite(condition: str) -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        bind.commit()  # the previous migration's version stamp may have left a transaction open
        bind.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if bind.exec_driver_sql("PRAGMA foreign_keys").scalar() != 0:
        raise RuntimeError("could not switch SQLite foreign keys off for the rebuild")
    try:
        with op.batch_alter_table(TABLE, recreate="always") as batch:
            batch.drop_constraint(op.f(CONSTRAINT), type_="check")
            batch.create_check_constraint(op.f(CONSTRAINT), sa.text(condition))
        problems = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"the rebuild left {len(problems)} broken foreign keys")
    finally:
        bind.exec_driver_sql("PRAGMA foreign_keys=ON")


def _replace(condition: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
        op.drop_constraint(op.f(CONSTRAINT), TABLE, type_="check")
        op.create_check_constraint(op.f(CONSTRAINT), TABLE, sa.text(condition))
    else:
        _rebuild_sqlite(condition)


def upgrade() -> None:
    _replace(TEN)


def downgrade() -> None:
    _replace(WIDE)
