"""row-level security, grants for the app role, users.display_name

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18 15:00:00.000000

Postgres only (everything except ``users.display_name`` is skipped on SQLite).

Row-level security on every table that holds tenant data, ENABLEd **and FORCEd**. FORCE makes the
policies apply to the table owner too, so a host that cannot create a second database role
(single-role mode: the app connects as the owner) still gets tenant isolation. With a separate
role (``APP_DB_ROLE``) the grants below also limit what that role can do.

The tenant is ``current_setting('app.agency_id')``, set per transaction by app.tenancy. Unset or
empty means NULL, a comparison with NULL is never true, so an unbound session sees zero rows.

Policies (permissive ones are OR-ed together):

* ``tenant_isolation`` (FOR ALL, USING and WITH CHECK ``agency_id = app_current_agency_id()``) on
  agencies (``id = ...``), uploads, tickets, users, staff_sign_links, user_sessions and
  temporary_passwords.
* Narrow SELECT-only lookups that work before the tenant is known, each keyed on an exact value
  the caller must already hold: ``users.login_lookup`` (``app.login_phone``),
  ``user_sessions.session_lookup`` (``app.session_token_hash``), ``agencies.slug_lookup``
  (``app.agency_slug``, for the admin CLI).
* ``audit_events`` is append-only and its agency_id may be NULL (a failed login for an unknown
  phone). ``tenant_read`` (SELECT) shows a tenant its own rows only; ``tenant_or_unscoped_insert``
  (INSERT) allows a row for the bound tenant or with NULL agency_id, from any session. There is
  no UPDATE or DELETE policy, so rows cannot be edited even by the owner. A NULL-agency row is
  never readable through any policy. Consequence: INSERT ... RETURNING of such a row fails
  (RETURNING needs SELECT), so the AuditEvent model sets implicit_returning=False and the id
  comes from the sequence beforehand.
* ``rate_limit_buckets`` has no agency, so it has no RLS.

Grants, only when APP_DB_ROLE names an existing role (otherwise single-role mode: nothing is
granted and a warning is logged). No DELETE on tickets or uploads; the role cannot create tables.

Downgrade reverses all of it, except that CREATE on schema public is not given back to PUBLIC.
"""

import logging
import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db_roles import ROLE_ENV_VAR, quote_role, validate_role_name

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

TENANT_ID = "app_current_agency_id()"
# Tables with an ``agency_id`` column and the full tenant_isolation policy.
ISOLATED_TABLES = (
    "uploads",
    "tickets",
    "users",
    "staff_sign_links",
    "user_sessions",
    "temporary_passwords",
)
RLS_TABLES = ("agencies", *ISOLATED_TABLES, "audit_events")
# table -> privileges for the app role (agencies and alembic_version are read-only)
GRANTS = {
    "agencies": "SELECT",
    "alembic_version": "SELECT",
    "uploads": "SELECT, INSERT, UPDATE",
    "tickets": "SELECT, INSERT, UPDATE",
    "users": "SELECT, INSERT, UPDATE",
    "temporary_passwords": "SELECT, INSERT, UPDATE",
    "staff_sign_links": "SELECT, INSERT, UPDATE, DELETE",
    "user_sessions": "SELECT, INSERT, UPDATE, DELETE",
    "rate_limit_buckets": "SELECT, INSERT, UPDATE, DELETE",
    "audit_events": "SELECT, INSERT",
}


def _app_role(bind: sa.Connection, *, must_exist: bool) -> str | None:
    """The validated APP_DB_ROLE, or None for single-role mode (unset, blank or - on downgrade -
    a role that no longer exists)."""
    raw = os.environ.get(ROLE_ENV_VAR, "").strip()
    if not raw:
        return None
    role = validate_role_name(raw)
    exists = bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role})
    if exists.scalar() is None:
        if must_exist:
            raise RuntimeError(
                f"{ROLE_ENV_VAR} names the role {role!r}, which does not exist. Create it first "
                f"(CREATE ROLE {role} LOGIN ...), or unset {ROLE_ENV_VAR} for single-role mode."
            )
        log.warning("%s=%s does not exist; no privileges to revoke from it.", ROLE_ENV_VAR, role)
        return None
    return role


def _run(statements: Sequence[str]) -> None:
    for statement in statements:
        op.execute(sa.text(statement))


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    role = _app_role(bind, must_exist=True)  # fail before any DDL on a bad name

    _run(
        [
            f"CREATE FUNCTION {TENANT_ID} RETURNS bigint LANGUAGE sql STABLE AS "
            "$$ SELECT CAST(NULLIF(current_setting('app.agency_id', true), '') AS bigint) $$"
        ]
    )
    for table in RLS_TABLES:
        _run([f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"])
        _run([f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"])
    _run(
        [
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING ({'id' if table == 'agencies' else 'agency_id'} = {TENANT_ID}) "
            f"WITH CHECK ({'id' if table == 'agencies' else 'agency_id'} = {TENANT_ID})"
            for table in ("agencies", *ISOLATED_TABLES)
        ]
    )
    _run(
        [
            "CREATE POLICY login_lookup ON users FOR SELECT "
            "USING (phone = current_setting('app.login_phone', true))",
            "CREATE POLICY session_lookup ON user_sessions FOR SELECT "
            "USING (token_hash = current_setting('app.session_token_hash', true))",
            "CREATE POLICY slug_lookup ON agencies FOR SELECT "
            "USING (slug = current_setting('app.agency_slug', true))",
            f"CREATE POLICY tenant_read ON audit_events FOR SELECT USING (agency_id = {TENANT_ID})",
            "CREATE POLICY tenant_or_unscoped_insert ON audit_events FOR INSERT "
            f"WITH CHECK (agency_id IS NULL OR agency_id = {TENANT_ID})",
        ]
    )

    if role is None:
        log.warning(
            "%s is not set: single-role mode. No privileges were granted. Row-level security is "
            "FORCEd, so the connecting owner role is isolated per tenant, but that role can "
            "still run ALTER TABLE ... NO FORCE ROW LEVEL SECURITY. Prefer a separate role.",
            ROLE_ENV_VAR,
        )
        return
    quoted = quote_role(role)
    _run(
        ["REVOKE CREATE ON SCHEMA public FROM PUBLIC", f"GRANT USAGE ON SCHEMA public TO {quoted}"]
    )
    _run([f"GRANT {privileges} ON {table} TO {quoted}" for table, privileges in GRANTS.items()])
    _run([f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted}"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        role = _app_role(bind, must_exist=False)
        if role is None:
            log.warning(
                "%s is not set (or the role is gone): privileges granted to the app role were "
                "not revoked. Revoke them by hand if that role should lose access.",
                ROLE_ENV_VAR,
            )
        else:
            quoted = quote_role(role)
            _run(
                [
                    f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted}",
                    f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {quoted}",
                    f"REVOKE USAGE ON SCHEMA public FROM {quoted}",
                ]
            )
        _run(
            [
                "DROP POLICY tenant_or_unscoped_insert ON audit_events",
                "DROP POLICY tenant_read ON audit_events",
                "DROP POLICY slug_lookup ON agencies",
                "DROP POLICY session_lookup ON user_sessions",
                "DROP POLICY login_lookup ON users",
            ]
        )
        _run([f"DROP POLICY tenant_isolation ON {t}" for t in ("agencies", *ISOLATED_TABLES)])
        for table in RLS_TABLES:
            _run([f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"])
            _run([f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"])
        _run([f"DROP FUNCTION {TENANT_ID}"])
    with op.batch_alter_table("users") as batch:
        batch.drop_column("display_name")
