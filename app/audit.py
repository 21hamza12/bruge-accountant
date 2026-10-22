"""Audit events: who did what, never with a secret.

Every event goes to the ``audit_events`` table (append-only under row-level security) and to the
JSON log (``app.audit`` logger), so an event with no agency (a failed login for an unknown phone,
which no policy lets anyone read back) still shows up where operators can see it.

Rules, enforced by ``_check_details``:

* ``details`` holds ids, counts and short internal reasons. A key that looks like a credential
  (password, token, secret, hash, csrf, cookie) is a programming error and raises.
* ``now`` is the application clock (the injected one in tests); without it the database's own
  default time is used.
* Events an anonymous client can make at will (a failed CSRF check, a rate-limited request) are
  NOT written to the table per request: ``log_only`` sends them to the JSON log, and a rate
  limit writes one row per bucket window (the first rejection). Otherwise a flood of bare POSTs
  would be a flood of inserts.
* Phone numbers are masked with ``mask_phone`` before they get here.
* ``agency_id`` is NULL only when no tenant is known; then ``actor_user_id`` must be NULL too
  (a CHECK constraint says so).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
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
UPLOAD_SUCCEEDED = "upload_succeeded"
UPLOAD_FAILED = "upload_failed"
PARTNER_CREATED = "partner_created"
PARTNER_UPDATED = "partner_updated"
PARTNER_FEE_SET = "partner_fee_set"
PARTNER_DEACTIVATED = "partner_deactivated"
PARTNER_REACTIVATED = "partner_reactivated"
ALIAS_PROPOSED = "alias_proposed"
ALIAS_APPROVED = "alias_approved"
ALIAS_REJECTED = "alias_rejected"
ALIAS_REACTIVATED = "alias_reactivated"
SALE_CREATED = "sale_created"
SALE_EDITED = "sale_edited"
SALE_VOIDED = "sale_voided"
SALE_MOVED = "sale_moved"
DAY_OPENED = "day_opened"
DAY_CLOSED = "day_closed"
DAY_REOPENED = "day_reopened"
COMPARISON_RUN = "comparison_run"
COMPARISON_SUPERSEDED = "comparison_flags_superseded"
DAY_APPROVED = "day_approved"

_SECRET_KEY = re.compile(r"password|passwd|token|secret|hash|csrf|cookie|credential", re.IGNORECASE)


def _check_details(details: dict[str, Any]) -> None:
    for key in details:
        if _SECRET_KEY.search(key):
            raise ValueError(f"audit details must not carry secrets (key {key!r})")


def _log(
    event_type: str,
    agency_id: int | None,
    actor_user_id: int | None,
    ip: str | None,
    data: dict[str, Any],
) -> None:
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


def log_only(
    event_type: str,
    *,
    agency_id: int | None = None,
    actor_user_id: int | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """The JSON log line of an audit event, with no database write."""
    data = dict(details or {})
    _check_details(data)
    _log(event_type, agency_id, actor_user_id, ip, data)


def record(
    db: Session,
    event_type: str,
    *,
    agency_id: int | None = None,
    actor_user_id: int | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """Add one event to the current transaction (and log it). The caller commits."""
    data = dict(details or {})
    _check_details(data)
    event = AuditEvent(
        agency_id=agency_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        ip=ip,
        details=data,
    )
    if now is not None:
        event.created_at = now
    db.add(event)
    db.flush()
    _log(event_type, agency_id, actor_user_id, ip, data)


def refused(
    db: Session,
    action: str,
    *,
    agency_id: int,
    actor_user_id: int,
    ip: str | None = None,
    now: datetime | None = None,
) -> None:
    """A ``permission_denied`` event for a service call the actor was not allowed to make. Nothing
    but the action name is recorded (no name, phone or amount). It commits at once, like the web
    guard does: the caller is about to raise, and nothing else was written yet. Every service
    must therefore refuse before its first write (tests pin this for create, edit, void, move,
    close and reopen)."""
    record_and_commit(
        db,
        PERMISSION_DENIED,
        agency_id=agency_id,
        actor_user_id=actor_user_id,
        ip=ip,
        details={"action": action},
        now=now,
    )


def record_and_commit(
    db: Session,
    event_type: str,
    *,
    agency_id: int | None = None,
    actor_user_id: int | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> None:
    """``record`` and commit at once: for events written just before the request is refused
    (the request's own transaction would be rolled back by the error)."""
    record(
        db,
        event_type,
        agency_id=agency_id,
        actor_user_id=actor_user_id,
        ip=ip,
        details=details,
        now=now,
    )
    db.commit()
