"""agency slug must not be empty; fillfactor 70 on rate_limit_buckets

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-18 20:00:00.000000

EXPAND-ONLY: the previous release keeps working against this schema, so rolling back is a
redeploy (docs/phase2-plan.md, the expand/contract rule). Nothing is removed or renamed; the one
new restriction (an empty slug is refused) is something the previous release never wrote, because
the CLI validates slugs against ``^[a-z0-9][a-z0-9-]{0,62}$``.

* ``CHECK (slug <> '')`` on ``agencies``. The ``slug_lookup`` row-level security policy compares
  the slug with ``current_setting('app.agency_slug', true)``, and after any transaction that set
  the setting a pooled connection answers ``''`` instead of NULL. An agency with an empty slug
  would then match the policy on such a connection. The constraint makes that impossible, which
  is cleaner than hiding it behind NULLIF inside the policy. (SQLite, which is development and
  tests only, gets two triggers instead: see upgrade().)
* ``fillfactor = 70`` on ``rate_limit_buckets`` (Postgres only). Every login attempt UPDATEs a
  counter row, so most writes can be HOT updates only if each page keeps free space.

Adding the constraint validates the existing rows: a database that somehow holds an agency with an
empty slug fails this migration loudly, and the row must be fixed by hand first.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_agencies_slug_nonempty"
EMPTY_SLUG_MESSAGE = "agencies.slug must not be empty"
SQLITE_TRIGGERS = {
    "trg_agencies_slug_nonempty_insert": "BEFORE INSERT ON agencies WHEN NEW.slug = ''",
    "trg_agencies_slug_nonempty_update": "BEFORE UPDATE OF slug ON agencies WHEN NEW.slug = ''",
}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_check_constraint(op.f(CONSTRAINT), "agencies", "slug <> ''")
        op.execute("ALTER TABLE rate_limit_buckets SET (fillfactor = 70)")
        return
    # SQLite (development and tests only; production refuses it). It cannot add a CHECK to an
    # existing table without rebuilding it, and rebuilding ``agencies`` fails while other tables
    # hold rows that point at it. Two triggers enforce the same rule without touching the table;
    # databases built straight from the models get the real CHECK (app/models.py).
    for name, when in SQLITE_TRIGGERS.items():
        op.execute(
            f"CREATE TRIGGER {name} {when} BEGIN SELECT RAISE(ABORT, '{EMPTY_SLUG_MESSAGE}'); END"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE rate_limit_buckets RESET (fillfactor)")
        op.drop_constraint(op.f(CONSTRAINT), "agencies", type_="check")
        return
    for name in SQLITE_TRIGGERS:
        op.execute(f"DROP TRIGGER {name}")
