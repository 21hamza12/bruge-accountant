"""Tenant binding for row-level security (Postgres); every call is a no-op on SQLite.

Policies compare ``agency_id`` with ``current_setting('app.agency_id')``. That setting is
transaction-local (``set_config(..., true)``), so it can never leak to the next user of a pooled
connection, and an unbound session sees zero rows (fails closed).

``import_report`` commits midway, and a commit ends the transaction together with the setting.
So the tenant is remembered in ``session.info`` and an ``after_begin`` listener, registered
once on the ``Session`` class (no session can forget to install it), applies it again at the
start of every transaction.

Only the ORM expression layer is used (``func.set_config``): the value always travels as a bound
parameter, never inside SQL text.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, event, func, select
from sqlalchemy.orm import Session, SessionTransaction

TENANT_SETTING = "app.agency_id"
INFO_KEY = "bruge.agency_id"

# The only other settings the application may set. They feed the narrow policies that let a
# login (by exact phone) or a session lookup (by exact token hash) find one row before the
# tenant is known.
LOOKUP_KEYS = frozenset({"app.login_phone", "app.session_token_hash"})


def set_config_statement(key: str, value: str) -> Any:
    """``SELECT set_config(key, value, true)``: transaction-local, parameters bound."""
    return select(func.set_config(key, value, True))


def _is_postgres(bind: Engine | Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _set_config(connection: Connection, key: str, value: str) -> None:
    connection.execute(set_config_statement(key, value))


def bound_tenant(session: Session) -> int | None:
    """The agency this session is bound to, if any."""
    value = session.info.get(INFO_KEY)
    return value if isinstance(value, int) else None


def bind_tenant(session: Session, agency_id: int) -> None:
    """Scope this session to one agency, now and in every later transaction."""
    if isinstance(agency_id, bool) or not isinstance(agency_id, int):
        raise TypeError("agency_id must be an int")
    if agency_id <= 0:
        raise ValueError("agency_id must be positive")
    session.info[INFO_KEY] = agency_id
    if _is_postgres(session.get_bind()):
        was_open = session.in_transaction()
        connection = session.connection()  # begins a transaction; after_begin then applies it
        if was_open:
            _set_config(connection, TENANT_SETTING, str(agency_id))


def lookup_scope(session: Session, key: str, value: str) -> None:
    """Set one narrow lookup setting for the current transaction only (not re-applied)."""
    if key not in LOOKUP_KEYS:
        raise ValueError(f"unknown lookup key {key!r}; allowed: {sorted(LOOKUP_KEYS)}")
    if not isinstance(value, str):
        raise TypeError("lookup value must be a str")
    if _is_postgres(session.get_bind()):
        _set_config(session.connection(), key, value)


def _after_begin(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    agency_id = bound_tenant(session)
    if agency_id is not None and _is_postgres(connection):
        _set_config(connection, TENANT_SETTING, str(agency_id))


event.listen(Session, "after_begin", _after_begin)
