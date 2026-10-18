"""Manager-issued temporary passwords: Argon2-hashed, valid 30 minutes, usable once.

Issuing one (``issue_temporary_password``):

* clears the user's real ``password_hash`` (the old password stops working at once),
* revokes every session of the user,
* retires temporary passwords that were issued earlier and never used,
* stores only the Argon2 hash with ``expires_at = now + 30 min`` and sets ``must_change_password``,
* audits ``temp_password_issued`` (ids only, never the value).

The plaintext exists in exactly one place: the return value, which the staff route shows once in
its POST response (``Cache-Control: no-store``, never in a redirect or URL).

Using one (``consume_temporary_password``) is a single atomic
``UPDATE .. SET used_at = :now WHERE id = :id AND used_at IS NULL AND expires_at > :now
RETURNING id``: of two simultaneous logins exactly one gets a row back. The time is the injected
clock, not the database's. The user then has a session that can only reach the forced password
change page; the spent temporary password may be typed once more there as the "current password"
(``spent_temporary_matches``), because no other current password exists, but only within 30
minutes of being spent (the same limit as that session's life).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import audit
from app.auth import sessions
from app.auth.passwords import (
    generate_temporary_password,
    hash_password,
    verify_dummy,
    verify_password,
)
from app.models import TemporaryPassword, User

TEMP_PASSWORD_TTL = sessions.FORCED_CHANGE_TTL  # 30 minutes, for the password and its session


def retire_pending(db: Session, user: User, now: datetime) -> int:
    """Mark every unused temporary password of the user as used. Returns how many."""
    result = db.execute(
        update(TemporaryPassword)
        .where(
            TemporaryPassword.agency_id == user.agency_id,
            TemporaryPassword.user_id == user.id,
            TemporaryPassword.used_at.is_(None),
        )
        .values(used_at=now)
        .returning(TemporaryPassword.id)
    )
    return len(result.all())


def issue_temporary_password(
    db: Session,
    user: User,
    now: datetime,
    *,
    actor_user_id: int | None,
    ip: str | None = None,
    audit_details: dict[str, Any] | None = None,
) -> str:
    """Give ``user`` a new temporary password and return its plaintext. The caller commits.

    The hash is computed first, so the time spent does not depend on anything that follows.
    """
    plaintext = generate_temporary_password()
    hashed = hash_password(plaintext)
    retire_pending(db, user, now)
    user.password_hash = None
    user.must_change_password = True
    user.updated_at = now
    sessions.revoke_all_for_user(db, user.agency_id, user.id, now)
    db.add(
        TemporaryPassword(
            agency_id=user.agency_id,
            user_id=user.id,
            password_hash=hashed,
            created_at=now,
            expires_at=now + TEMP_PASSWORD_TTL,
        )
    )
    audit.record(
        db,
        audit.TEMP_PASSWORD_ISSUED,
        agency_id=user.agency_id,
        actor_user_id=actor_user_id,
        ip=ip,
        details={"user_id": user.id, **(audit_details or {})},
        now=now,
    )
    db.flush()
    return plaintext


def latest_id(db: Session, user: User) -> int:
    """The id of the user's newest temporary password (spent or not), 0 when there never was one.

    The re-issue form carries this number and the POST must still find it, so a replayed POST
    (a browser reload) is refused instead of silently issuing another password.
    """
    value = db.scalar(
        select(func.max(TemporaryPassword.id)).where(
            TemporaryPassword.agency_id == user.agency_id,
            TemporaryPassword.user_id == user.id,
        )
    )
    return int(value or 0)


def latest_unused(db: Session, user: User) -> TemporaryPassword | None:
    return db.scalars(
        select(TemporaryPassword)
        .where(
            TemporaryPassword.agency_id == user.agency_id,
            TemporaryPassword.user_id == user.id,
            TemporaryPassword.used_at.is_(None),
        )
        .order_by(TemporaryPassword.id.desc())
        .limit(1)
    ).first()


def consume_temporary_password(db: Session, temp_id: int, now: datetime) -> bool:
    """Atomically spend one temporary password. False when it was used or has expired."""
    row = db.execute(
        update(TemporaryPassword)
        .where(
            TemporaryPassword.id == temp_id,
            TemporaryPassword.used_at.is_(None),
            TemporaryPassword.expires_at > now,
        )
        .values(used_at=now)
        .returning(TemporaryPassword.id)
    ).first()
    return row is not None


def spent_temporary_matches(db: Session, user: User, password: str, now: datetime) -> bool:
    """True if ``password`` is the temporary password the user logged in with less than 30
    minutes ago (the same window as the session that login opened)."""
    row = db.scalars(
        select(TemporaryPassword)
        .where(
            TemporaryPassword.agency_id == user.agency_id,
            TemporaryPassword.user_id == user.id,
            TemporaryPassword.used_at.is_not(None),
            TemporaryPassword.used_at > now - TEMP_PASSWORD_TTL,
        )
        .order_by(TemporaryPassword.id.desc())
        .limit(1)
    ).first()
    db.commit()  # no database connection is held while Argon2 runs
    if row is None:
        verify_dummy()
        return False
    return verify_password(password, row.password_hash)
