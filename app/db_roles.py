"""The application's database role (``APP_DB_ROLE``): validation, privilege guard and grants.

Migration 0003 and the ``grant-app-role`` command grant privileges to that role. DDL cannot take
bind parameters, so the role name is checked against a strict pattern and then always
double-quoted as an identifier; table and schema names are quoted the same way. The pattern
forbids quotes and every other special character, so quoting can never be broken out of. This is
the only module allowed to build raw DDL statements: a code guard test enforces it.

Which schema: the grants name the schema the tables actually live in (``current_schema()`` of
the connection, where Alembic created them), not the literal ``public``. The one statement that
does say ``public`` on purpose is ``HARDENING``: that is the schema PostgreSQL 14 and older give
``CREATE`` to every user (the pseudo-role ``PUBLIC``) in, and it is the same schema everywhere.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import DDL, Boolean, Connection, Engine, String, column, func, select, table
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

ROLE_ENV_VAR = "APP_DB_ROLE"
_ROLE_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")

# table -> privileges for the app role. Read-only: agencies and alembic_version. DELETE exists
# only where a narrow policy limits it: sessions and temporary passwords (purge and revocation),
# audit events (the 400-day retention policy), sign links and rate limit buckets. Never on
# tickets, uploads, users or agencies. A test fails when a table is missing here and from
# GRANT_EXEMPT, so a future table cannot silently go without a decision.
GRANTS = {
    "agencies": "SELECT",
    "alembic_version": "SELECT",
    "uploads": "SELECT, INSERT, UPDATE",
    "tickets": "SELECT, INSERT, UPDATE",
    "users": "SELECT, INSERT, UPDATE",
    "temporary_passwords": "SELECT, INSERT, UPDATE, DELETE",
    "staff_sign_links": "SELECT, INSERT, UPDATE, DELETE",
    "user_sessions": "SELECT, INSERT, UPDATE, DELETE",
    "rate_limit_buckets": "SELECT, INSERT, UPDATE, DELETE",
    "audit_events": "SELECT, INSERT, DELETE",
    # Append-only snapshot of every upload (migration 0006): never updated, never deleted.
    "amadeus_lines": "SELECT, INSERT",
    # Phase B3 (migration 0007). Partner fees are append-only: no UPDATE, no DELETE.
    "partners": "SELECT, INSERT, UPDATE",
    "partner_fees": "SELECT, INSERT",
    # Phase B4 (migration 0008). Days are mutable; their events and the sale versions are
    # append-only. ``sale_tickets`` is the one table of the phase with DELETE: it holds the
    # current membership of a sale, and removing a ticket (or voiding the sale) must free its
    # number; the history of what was there is in ``sale_versions``.
    "report_days": "SELECT, INSERT, UPDATE",
    "day_events": "SELECT, INSERT",
    "sales": "SELECT, INSERT, UPDATE",
    "sale_tickets": "SELECT, INSERT, DELETE",
    "sale_versions": "SELECT, INSERT",
}
# The tables migration 0003 grants on, frozen. 0003 passes this to ``apply_grants`` instead of
# relying on GRANTS, which later phases extend: a fresh ``alembic upgrade head`` runs 0003 before
# those tables exist, and a GRANT on a missing table fails. Never edit this list; a later
# migration grants on its own tables (``apply_grants(bind, role, tables=(...))``).
MIGRATION_0003_TABLES: tuple[str, ...] = (
    "agencies",
    "alembic_version",
    "uploads",
    "tickets",
    "users",
    "temporary_passwords",
    "staff_sign_links",
    "user_sessions",
    "rate_limit_buckets",
    "audit_events",
)
# Tables deliberately given no privileges at all (with the reason, when there are any).
GRANT_EXEMPT: frozenset[str] = frozenset()

HARDENING = "REVOKE CREATE ON SCHEMA public FROM PUBLIC"

_PG_ROLES = table(
    "pg_roles",
    column("rolname", String),
    column("rolsuper", Boolean),
    column("rolbypassrls", Boolean),
)
_log = logging.getLogger("alembic.runtime.migration")
_PREPARER = postgresql.dialect().identifier_preparer  # type: ignore[no-untyped-call]


class PrivilegedRoleError(RuntimeError):
    """The database role is a superuser or has BYPASSRLS, so row-level security would not apply."""


def validate_role_name(name: str) -> str:
    """Return ``name`` if it is a plain lowercase Postgres role name, else raise ValueError."""
    if not isinstance(name, str):
        raise TypeError("the role name must be a str")
    if _ROLE_RE.fullmatch(name) is None:
        # The value itself is not echoed: it may be hostile, or a secret typed in the wrong box.
        raise ValueError(
            f"{ROLE_ENV_VAR} must be 1-63 characters: lowercase letters, digits and underscores, "
            "not starting with a digit"
        )
    return name


def quote_role(name: str) -> str:
    """The role name as a double-quoted SQL identifier (validated first)."""
    return '"' + validate_role_name(name) + '"'


def _quote(identifier: str) -> str:
    return _PREPARER.quote_identifier(identifier)


# --- the privileged-role guard -------------------------------------------------------------


def describe_privilege_problem(name: str, *, superuser: bool, bypassrls: bool) -> str | None:
    """A sentence naming why the role skips row-level security, or None for a plain role."""
    kinds = []
    if superuser:
        kinds.append("is a superuser")
    if bypassrls:
        kinds.append("has BYPASSRLS")
    if not kinds:
        return None
    return f"role {name!r} {' and '.join(kinds)}, so row-level security would not apply to it"


def role_privilege_problem(connection: Connection, role: str | None = None) -> str | None:
    """Why ``role`` (default: the connected user) skips row-level security, or None if it does
    not. A role that does not exist is reported as a problem too."""
    wanted = _PG_ROLES.c.rolname == (func.current_user() if role is None else role)
    row = connection.execute(
        select(_PG_ROLES.c.rolname, _PG_ROLES.c.rolsuper, _PG_ROLES.c.rolbypassrls).where(wanted)
    ).first()
    if row is None:
        return f"role {role!r} does not exist"
    return describe_privilege_problem(
        row.rolname, superuser=row.rolsuper, bypassrls=row.rolbypassrls
    )


def privileged_startup_message(problem: str) -> str:
    return (
        f"{problem}. In production DATABASE_URL must name a plain role: not a superuser and "
        "without BYPASSRLS (see docs/runbook.md section 3). Refusing to start."
    )


def check_connecting_role(engine: Engine) -> str | None:
    """The problem with the role this engine connects as, or None. A server that cannot be
    reached is not a verdict (the health check reports it), so that too returns None."""
    if engine.dialect.name != "postgresql":
        return None
    try:
        with engine.connect() as connection:
            return role_privilege_problem(connection)
    except SQLAlchemyError:
        return None


# --- grants ---------------------------------------------------------------------------------


def grant_statements(role: str, schema: str, tables: Iterable[str] | None = None) -> list[str]:
    """GRANT statements for ``role`` on the tables in ``schema`` (all identifiers quoted).

    ``tables`` limits the statements to those tables (each must be in GRANTS, which says what the
    role may do on it); the default is every table in GRANTS."""
    who, where = quote_role(role), _quote(schema)
    names = list(GRANTS) if tables is None else list(tables)
    unknown = [name for name in names if name not in GRANTS]
    if unknown:
        raise ValueError(f"no grants are defined for: {', '.join(sorted(unknown))}")
    statements = [f"GRANT USAGE ON SCHEMA {where} TO {who}"]
    statements += [f"GRANT {GRANTS[name]} ON {where}.{_quote(name)} TO {who}" for name in names]
    statements.append(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {where} TO {who}")
    return statements


def revoke_statements(role: str, schema: str, tables: Iterable[str] | None = None) -> list[str]:
    """REVOKE statements for ``role``: every table of the schema, or only ``tables``. Sequences
    and the schema are always included (their grants are not tracked per table)."""
    who, where = quote_role(role), _quote(schema)
    if tables is None:
        on_tables = [f"REVOKE ALL ON ALL TABLES IN SCHEMA {where} FROM {who}"]
    else:
        on_tables = [f"REVOKE ALL ON {where}.{_quote(name)} FROM {who}" for name in tables]
    return [
        *on_tables,
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {where} FROM {who}",
        f"REVOKE USAGE ON SCHEMA {where} FROM {who}",
    ]


def _current_schema(connection: Connection) -> str:
    schema: Any = connection.scalar(select(func.current_schema()))
    if not schema:
        raise RuntimeError("the connection has no current schema (empty search_path)")
    return str(schema)


def assert_grantable(connection: Connection, role: str) -> None:
    """The role exists and does not skip row-level security; raises otherwise."""
    validate_role_name(role)
    exists = connection.scalar(select(func.count()).where(_PG_ROLES.c.rolname == role))
    if not exists:
        raise RuntimeError(
            f"{ROLE_ENV_VAR} names the role {role!r}, which does not exist. Create it first "
            f"(CREATE ROLE {role} LOGIN ...), or unset {ROLE_ENV_VAR} for single-role mode."
        )
    problem = role_privilege_problem(connection, role)
    if problem is not None:
        raise PrivilegedRoleError(f"{ROLE_ENV_VAR}: {problem}")


def configured_role(connection: Connection, raw: str, *, must_exist: bool) -> str | None:
    """The validated role named by ``raw`` (the ``APP_DB_ROLE`` value, which the migration reads:
    only app/settings.py reads the environment inside ``app``), or None for single-role mode
    (blank or, on a downgrade, a role that no longer exists). With ``must_exist`` (an upgrade)
    the role must exist and must not skip row-level security; otherwise this raises."""
    raw = raw.strip()
    if not raw:
        return None
    role = validate_role_name(raw)
    exists = connection.execute(select(_PG_ROLES.c.rolname).where(_PG_ROLES.c.rolname == role))
    if exists.scalar() is None:
        if must_exist:
            raise RuntimeError(
                f"{ROLE_ENV_VAR} names the role {role!r}, which does not exist. Create it first "
                f"(CREATE ROLE {role} LOGIN ...), or unset {ROLE_ENV_VAR} for single-role mode."
            )
        _log.warning("%s=%s does not exist; no privileges to revoke from it.", ROLE_ENV_VAR, role)
        return None
    if must_exist:
        problem = role_privilege_problem(connection, role)
        if problem is not None:
            raise RuntimeError(f"{ROLE_ENV_VAR}: {problem}")
    return role


def apply_grants(connection: Connection, role: str, tables: Iterable[str] | None = None) -> None:
    """Give ``role`` the privileges of GRANTS, on every table or only on ``tables``. Idempotent:
    safe to run again, for a role created after migration 0003 or to repair one that lost its
    grants. A migration passes its own tables, because GRANTS also names tables that a later
    migration creates."""
    assert_grantable(connection, role)
    schema = _current_schema(connection)
    for statement in (HARDENING, *grant_statements(role, schema, tables)):
        connection.execute(DDL(statement))  # type: ignore[no-untyped-call]


def revoke_grants(connection: Connection, role: str, tables: Iterable[str] | None = None) -> None:
    schema = _current_schema(connection)
    for statement in revoke_statements(role, schema, tables):
        connection.execute(DDL(statement))  # type: ignore[no-untyped-call]


def revoke_table_grants(connection: Connection, role: str, tables: Iterable[str]) -> None:
    """Take ``role``'s privileges on ``tables`` only. Unlike ``revoke_grants`` it leaves the
    schema and the sequences alone: for a migration that is undone while the older tables (and
    the role's access to them) stay."""
    schema = _current_schema(connection)
    who, where = quote_role(role), _quote(schema)
    for name in tables:
        connection.execute(DDL(f"REVOKE ALL ON {where}.{_quote(name)} FROM {who}"))  # type: ignore[no-untyped-call]
