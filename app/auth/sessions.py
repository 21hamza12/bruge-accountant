"""Server-side sessions: the cookie holds a random token, the database only its sha256.

* Token: ``secrets.token_urlsafe(32)`` (256 bits). Only ``sha256(token)`` is stored, so a leaked
  database cannot be replayed as cookies. Every session also carries its own CSRF secret.
* Lookup: ``lookup_scope`` opens the narrow ``session_lookup`` row-level security policy for this
  one hash, and only when the session is alive and its user active is the tenant bound
  (``bind_tenant``, same transaction). A dead or unknown token binds nothing, so the request's
  database session stays unscoped and sees zero rows.
* Expiry: idle (sliding) and absolute (hard ceiling), both measured with the injected clock.
  ``last_seen_at`` is only written when it is at least ``TOUCH_INTERVAL`` old, so a page view does
  not cost a write; the idle deadline therefore slides in steps of at most five minutes.
* A "must change the password" session (opened with a temporary password) has an absolute expiry
  of ``FORCED_CHANGE_TTL`` after login and no idle slide beyond that.
* Rotation: a new token is issued at login and at password change (``create_session``), never a
  reused one; the old sessions are then revoked.
* Revocation is a timestamp (``revoked_at``): instant, and the purge job removes the row later.

The functions never commit except ``lookup_session`` when it has to write ``last_seen_at``;
callers commit their own work.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import User, UserSession
from app.settings import Settings
from app.tenancy import bind_tenant, lookup_scope, unbind_tenant

TOUCH_INTERVAL = timedelta(minutes=5)
# A session opened while the user still has to change the password (after a temporary password)
# lives this long in absolute terms, and never slides beyond it: it exists only to reach the
# password-change page. Same length as the temporary password itself.
FORCED_CHANGE_TTL = timedelta(minutes=30)
MAX_TOKEN_LENGTH = 128  # a real token is 43 characters; anything longer is not worth a query


@dataclass(frozen=True)
class SessionPolicy:
    idle: timedelta
    absolute: timedelta

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionPolicy:
        return cls(
            idle=timedelta(hours=settings.session_idle_hours),
            absolute=timedelta(days=settings.session_absolute_days),
        )


@dataclass(frozen=True)
class NewSession:
    token: str  # shown to the browser once; never stored
    row: UserSession


@dataclass(frozen=True)
class AuthenticatedSession:
    user: User
    session: UserSession


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


def create_session(db: Session, user: User, now: datetime, policy: SessionPolicy) -> NewSession:
    """Start a session for ``user`` and bind the request's database session to the user's agency.

    Raises ``TenantBindingError`` when the database session is already bound to another agency.
    The caller commits.
    """
    bind_tenant(db, user.agency_id)
    absolute = now + policy.absolute
    if user.must_change_password:
        absolute = min(absolute, now + FORCED_CHANGE_TTL)
    token = new_token()
    row = UserSession(
        agency_id=user.agency_id,
        user_id=user.id,
        token_hash=hash_token(token),
        csrf_token=secrets.token_urlsafe(32),
        created_at=now,
        last_seen_at=now,
        idle_expires_at=min(now + policy.idle, absolute),
        absolute_expires_at=absolute,
    )
    db.add(row)
    db.flush()
    return NewSession(token, row)


def _alive(row: UserSession, now: datetime) -> bool:
    return row.revoked_at is None and now < row.idle_expires_at and now < row.absolute_expires_at


def touch_session(db: Session, row: UserSession, now: datetime, policy: SessionPolicy) -> bool:
    """Record activity if the last write is old enough. True when it wrote (the caller commits)."""
    if now - row.last_seen_at < TOUCH_INTERVAL:
        return False
    row.last_seen_at = now
    row.idle_expires_at = min(now + policy.idle, row.absolute_expires_at)
    db.flush()
    return True


def lookup_session(
    db: Session, token: str, now: datetime, policy: SessionPolicy
) -> AuthenticatedSession | None:
    """The live session and its active user for a cookie token, with the tenant bound; else None."""
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return None
    token_hash = hash_token(token)
    lookup_scope(db, "app.session_token_hash", token_hash)
    row = db.scalars(select(UserSession).where(UserSession.token_hash == token_hash)).first()
    if row is None or not _alive(row, now):
        return None
    bind_tenant(db, row.agency_id)  # same transaction as the lookup; the user is known now
    user = db.scalars(
        select(User).where(User.agency_id == row.agency_id, User.id == row.user_id)
    ).one_or_none()
    if user is None or not user.is_active:
        unbind_tenant(db)  # nobody is logged in: leave the request session unscoped
        return None
    if touch_session(db, row, now, policy):
        db.commit()
    return AuthenticatedSession(user, row)


def revoke_session(db: Session, row: UserSession, now: datetime) -> None:
    if row.revoked_at is None:
        row.revoked_at = now
        db.flush()


def revoke_by_token(db: Session, token: str, agency_id: int, now: datetime) -> bool:
    """Revoke the live session of one agency that a cookie token names. True when one was.

    Used at login to retire the session the browser still presented. Row-level security limits
    this to the agency the request is bound to, so a cookie of another agency is left alone (it
    simply expires).
    """
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return False
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.agency_id == agency_id,
            UserSession.token_hash == hash_token(token),
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(UserSession.id)
    )
    return result.first() is not None


def revoke_all_for_user(
    db: Session,
    agency_id: int,
    user_id: int,
    now: datetime,
    *,
    except_session_id: int | None = None,
) -> int:
    """Revoke every live session of one user (optionally sparing one). Returns how many."""
    statement = (
        update(UserSession)
        .where(
            UserSession.agency_id == agency_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
        .returning(UserSession.id)
    )
    if except_session_id is not None:
        statement = statement.where(UserSession.id != except_session_id)
    revoked = len(db.execute(statement).all())
    # Rows already loaded in this session would otherwise still show the old value.
    for obj in list(db.identity_map.values()):
        if isinstance(obj, UserSession):
            db.expire(obj)
    return revoked
