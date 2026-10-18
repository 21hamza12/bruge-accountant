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

Rules of the binding:

* A session is bound to one agency. ``bind_tenant`` for another agency raises
  :class:`TenantBindingError` (nothing silently re-points a session); for the same agency it does
  nothing. ``rebind_tenant`` is the explicit switch, for the CLI, bootstrap and admin paths only.
  ``unbind_tenant`` clears the binding and, inside a transaction, the setting right away.
* Never call ``bind_tenant``, ``rebind_tenant`` or ``unbind_tenant`` inside ``begin_nested()``.
  ``set_config(..., true)`` is rolled back with a savepoint, so after a savepoint rollback the
  database would no longer have the setting while ``session.info`` still said "bound". They raise
  :class:`TenantBindingError` there instead (the simpler of the two fixes: no event to keep in
  step). Bind first, then open savepoints.
* Row-level security contains forgotten-filter and IDOR bugs. It does not contain SQL injection:
  a query that can run ``set_config`` can switch tenant. That is why ``set_config`` may only
  appear in this module (code guard) and all SQL goes through the ORM.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, event, func, select
from sqlalchemy.orm import Session, SessionTransaction

TENANT_SETTING = "app.agency_id"
INFO_KEY = "bruge.agency_id"

# The only other settings the application may set. They feed the narrow SELECT policies of
# migration 0003 that let a login (exact phone), a session lookup (exact token hash) or the admin
# CLI (exact agency slug) find one row before the tenant is known.
LOOKUP_KEYS = frozenset({"app.login_phone", "app.session_token_hash", "app.agency_slug"})


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


class TenantBindingError(RuntimeError):
    """The session is bound to another agency, or the change was attempted inside a savepoint."""


def _check_agency_id(agency_id: int) -> None:
    if isinstance(agency_id, bool) or not isinstance(agency_id, int):
        raise TypeError("agency_id must be an int")
    if agency_id <= 0:
        raise ValueError("agency_id must be positive")


def _refuse_in_savepoint(session: Session) -> None:
    if session.in_nested_transaction():
        raise TenantBindingError(
            "the tenant cannot be changed inside begin_nested(): a savepoint rollback would "
            "drop the database setting while the session still says it is bound; bind first"
        )


def _apply(session: Session, agency_id: int) -> None:
    session.info[INFO_KEY] = agency_id
    if _is_postgres(session.get_bind()):
        was_open = session.in_transaction()
        connection = session.connection()  # begins a transaction; after_begin then applies it
        if was_open:
            _set_config(connection, TENANT_SETTING, str(agency_id))


def bind_tenant(session: Session, agency_id: int) -> None:
    """Scope this session to one agency, now and in every later transaction.

    Binding the agency the session already has does nothing. Binding a different one raises
    :class:`TenantBindingError`: use :func:`rebind_tenant` where switching is the intent.
    """
    _check_agency_id(agency_id)
    _refuse_in_savepoint(session)
    current = bound_tenant(session)
    if current is not None:
        if current != agency_id:
            raise TenantBindingError(
                f"the session is already bound to agency {current}; refusing to re-point it to "
                f"{agency_id} (rebind_tenant is for admin paths that mean it)"
            )
        return
    _apply(session, agency_id)


def rebind_tenant(session: Session, agency_id: int) -> None:
    """Deliberately move the session to another agency. For the CLI, bootstrap and admin paths
    only; request handling and the repository use :func:`bind_tenant`."""
    _check_agency_id(agency_id)
    _refuse_in_savepoint(session)
    _apply(session, agency_id)


def unbind_tenant(session: Session) -> None:
    """Forget the bound agency, now: inside a transaction the setting is emptied at once, and
    later transactions start with no tenant (zero rows)."""
    _refuse_in_savepoint(session)
    had_binding = session.info.pop(INFO_KEY, None) is not None
    if had_binding and _is_postgres(session.get_bind()) and session.in_transaction():
        _set_config(session.connection(), TENANT_SETTING, "")


def restore_binding(session: Session, previous: int | None) -> None:
    """Put the binding back after a failed operation. Touches ``session.info`` only, so it
    cannot raise and a failure handler can call it without masking the original error: the
    next transaction applies the restored binding through ``after_begin``."""
    if previous is None:
        session.info.pop(INFO_KEY, None)
    else:
        session.info[INFO_KEY] = previous


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
