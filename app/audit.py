"""Audit events: who did what, never with a secret.

Every event goes to the ``audit_events`` table (append-only under row-level security) and to the
JSON log (``app.audit`` logger), so an event with no agency (a failed login for an unknown phone,
which no policy lets anyone read back) still shows up where operators can see it.

Rules, enforced by ``_check_details``:

* ``details`` holds ids, counts and short internal reasons. A key that looks like a credential
  (password, token, secret, hash, csrf, cookie) is a programming error and raises.
* Phone numbers are masked with ``mask_phone`` before they get here.
* ``agency_id`` is NULL only when no tenant is known; then ``actor_user_id`` must be NULL too
  (a CHECK constraint says so).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent

log = logging.getLogger("app.audit")

LOGIN_SUCCESS = "login_success"
LOGIN_FAILURE = "login_failure"
LOGOUT = "logout"
RATE_LIMITED = "rate_limited"
CSRF_FAILED = "csrf_failed"
PERMISSION_DENIED = "permission_denied"
PASSWORD_CHANGED = "password_changed"  # noqa: S105 - an event name, not a secret
TEMP_PASSWORD_ISSUED = "temp_password_issued"  # noqa: S105 - an event name
USER_CREATED = "user_created"
USER_DEACTIVATED = "user_deactivated"
USER_ROLE_CHANGED = "user_role_changed"
SIGN_LINKED = "staff_sign_linked"
SIGN_UNLINKED = "staff_sign_unlinked"

_SECRET_KEY = re.compile(r"password|passwd|token|secret|hash|csrf|cookie|credential", re.IGNORECASE)


def _check_details(details: dict[str, Any]) -> None:
    for key in details:
        if _SECRET_KEY.search(key):
            raise ValueError(f"audit details must not carry secrets (key {key!r})")


def record(
    db: Session,
    event_type: str,
    *,
    agency_id: int | None = None,
    actor_user_id: int | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add one event to the current transaction (and log it). The caller commits."""
    data = dict(details or {})
    _check_details(data)
    db.add(
        AuditEvent(
            agency_id=agency_id,
            actor_user_id=actor_user_id,
            event_type=event_type,
            ip=ip,
            details=data,
        )
    )
    db.flush()
    log.info(
        "audit",
        extra={
            "event_type": event_type,
            "agency_id": agency_id,
            "actor_user_id": actor_user_id,
            "ip": ip,
            "details": data,
        },
    )


def record_and_commit(
    db: Session,
    event_type: str,
    *,
    agency_id: int | None = None,
    actor_user_id: int | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """``record`` and commit at once: for events written just before the request is refused
    (the request's own transaction would be rolled back by the error)."""
    record(db, event_type, agency_id=agency_id, actor_user_id=actor_user_id, ip=ip, details=details)
    db.commit()
