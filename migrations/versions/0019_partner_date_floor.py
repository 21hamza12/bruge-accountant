"""a floor on the dates of partner lines and payments (phase F, slice F5 review)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-28 12:00:00.000000

EXPAND-ONLY: no table, column or grant changes; the previous release never writes such dates.

A line (``partner_entries.entry_date``) or a payment (``partner_payments.paid_on``) dated before
1 January 2020 is refused. Defence in depth behind the application's own checks (the opening
balance is refused more than 400 days back): a typo such as 0001 or 2002 would otherwise make
every account page walk thousands of half-months, and the tables are append-only, so it could
never be removed.

Postgres: ``CHECK (entry_date >= DATE '2020-01-01')`` and ``CHECK (paid_on >= ...)``, named like
the models' (``ck_partner_entries_entry_date_floor``, ``ck_partner_payments_paid_on_floor``);
adding them validates the rows already stored. SQLite cannot add a CHECK to an existing table
without rebuilding it (and with it 0017's triggers and partial indexes), so a BEFORE INSERT
trigger per table aborts instead; rows are never updated (0017's triggers). A database built
from the models (``create_all``) has the CHECKs themselves.

The downgrade drops the CHECKs (Postgres) or the triggers (SQLite); no data is touched.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FLOOR = "2020-01-01"  # app.models.PARTNER_DATE_FLOOR, frozen here
MESSAGE = "a partner date before 2020-01-01"
# table -> (column, constraint name, SQLite trigger name)
FLOORS = {
    "partner_entries": (
        "entry_date",
        "ck_partner_entries_entry_date_floor",
        "trg_partner_entries_date_floor",
    ),
    "partner_payments": (
        "paid_on",
        "ck_partner_payments_paid_on_floor",
        "trg_partner_payments_date_floor",
    ),
}


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for table, (column, check, trigger) in FLOORS.items():
        if postgres:
            op.create_check_constraint(op.f(check), table, f"{column} >= DATE '{FLOOR}'")
        else:
            op.execute(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table} "
                f"WHEN NEW.{column} < '{FLOOR}' "
                f"BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END"
            )


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for table, (_column, check, trigger) in FLOORS.items():
        if postgres:
            op.drop_constraint(op.f(check), table, type_="check")
        else:
            op.execute(f"DROP TRIGGER {trigger}")
