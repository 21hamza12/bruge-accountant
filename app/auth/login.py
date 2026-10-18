"""Checking a phone and a password.

Every way to fail costs one Argon2 verification (``verify_password`` spends the dummy
verification when there is nothing to compare with), so response time does not tell an attacker
whether a phone exists, is deactivated, or has no password yet. The caller shows one generic
message; the internal ``reason`` goes to the audit event only.

Order inside ``authenticate`` (the route has already passed CSRF and the rate limits):

1. the exact-phone lookup (the narrow ``login_lookup`` policy; nothing is bound yet),
2. only when a user was found is the tenant bound to the user's agency,
3. the password is verified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password, needs_rehash, verify_dummy, verify_password
from app.models import User
from app.tenancy import bind_tenant, lookup_scope

REASON_INVALID_PHONE = "invalid_phone"
REASON_UNKNOWN_PHONE = "unknown_phone"
REASON_INACTIVE = "inactive"
REASON_NO_CREDENTIALS = "no_credentials"
REASON_BAD_PASSWORD = "bad_password"  # noqa: S105 - an internal reason code, not a secret


@dataclass(frozen=True)
class LoginResult:
    """``reason`` is None exactly when the login succeeded. ``user`` is set whenever the phone
    belongs to an account, even for a failure (the audit event records the agency)."""

    user: User | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.reason is None and self.user is not None


def _fail(reason: str, user: User | None = None) -> LoginResult:
    return LoginResult(user, reason)


def find_user_by_phone(db: Session, phone: str) -> User | None:
    lookup_scope(db, "app.login_phone", phone)
    return db.scalars(select(User).where(User.phone == phone)).one_or_none()


def authenticate(db: Session, phone: str | None, password: str, now: datetime) -> LoginResult:
    """``phone`` is the E.164 form, or None when what was typed is not a phone number."""
    if phone is None:
        verify_dummy()
        return _fail(REASON_INVALID_PHONE)
    user = find_user_by_phone(db, phone)
    if user is None:
        verify_dummy()
        return _fail(REASON_UNKNOWN_PHONE)
    bind_tenant(db, user.agency_id)  # the user is known now
    if not user.is_active:
        verify_dummy()
        return _fail(REASON_INACTIVE, user)
    if user.password_hash is not None:
        if not verify_password(password, user.password_hash):
            return _fail(REASON_BAD_PASSWORD, user)
        if needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        return LoginResult(user, None)
    return _credential_free_login(db, user, password, now)


def _credential_free_login(db: Session, user: User, password: str, now: datetime) -> LoginResult:
    """No permanent password: only a temporary one (step 23) can work."""
    verify_dummy()
    return _fail(REASON_NO_CREDENTIALS, user)
