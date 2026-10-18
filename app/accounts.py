"""Staff account management for one agency (what a manager can do).

Every function takes the agency id of the logged-in manager and filters on it explicitly, on top
of row-level security: on SQLite there is no RLS, and a forgotten filter must not turn into an
IDOR there either. A user or sign that belongs to another agency is simply "not found"
(``None``), which the routes turn into a 404 - the same answer as for an id that does not exist.

Errors the manager can fix are raised as :class:`AccountError` with a French message. Two
messages are deliberately uninformative:

* ``DUPLICATE_PHONE``: phones are unique across the whole platform, so a specific "this number is
  registered" would let a manager probe other agencies' staff. Creating an account also spends
  the same Argon2 time whether or not the phone was taken.
* the last active manager cannot be deactivated or demoted (``LAST_MANAGER``), so an agency can
  never lock itself out. The managers are read ``FOR UPDATE`` first, so two managers removing
  each other at the same moment cannot both succeed.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.auth import sessions, temp_passwords
from app.auth.passwords import verify_dummy
from app.auth.phone import InvalidPhoneError, mask_phone, normalize_phone
from app.models import ROLE_MANAGER, ROLE_STAFF, StaffSignLink, User

ROLES = (ROLE_MANAGER, ROLE_STAFF)
MAX_NAME_LENGTH = 100
SIGN_PATTERN = re.compile(r"[A-Z0-9]{2,32}")

NAME_REQUIRED = f"Le nom est obligatoire ({MAX_NAME_LENGTH} caractères au maximum)."
BAD_ROLE = "Choisissez un rôle valide."
DUPLICATE_PHONE = "Impossible de créer ce compte avec ces informations."
BAD_SIGN = "Le code agent doit contenir de 2 à 32 lettres ou chiffres."
DUPLICATE_SIGN = "Ce code agent est déjà associé à un utilisateur."
LAST_MANAGER = "Impossible : il doit rester au moins un responsable actif dans l'agence."


class AccountError(Exception):
    """A mistake the manager can correct; ``str(error)`` is a French message."""


@dataclass(frozen=True)
class StaffRow:
    user: User
    signs: list[StaffSignLink]


def normalize_staff_sign(raw: str) -> str:
    """Trim and upper-case; the result is 2 to 32 letters or digits, or AccountError.

    The Amadeus parser stores the "AS" column trimmed (and Amadeus writes it in capitals), so a
    sign linked here is the exact string ``tickets.staff_sign`` will hold.
    """
    sign = unicodedata.normalize("NFKC", raw).strip().upper()
    if SIGN_PATTERN.fullmatch(sign) is None:
        raise AccountError(BAD_SIGN)
    return sign


def clean_display_name(raw: str) -> str:
    """NFC, single spaces, and no invisible format characters.

    Unicode category Cf (zero-width space and joiners, bidirectional overrides and isolates, the
    BOM) is removed: it lets one name look like another, or reorder the text around it, in a
    page that lists staff. Control characters (Cc) are refused outright.
    """
    name = unicodedata.normalize("NFC", raw)
    name = "".join(c for c in name if unicodedata.category(c) != "Cf")
    name = " ".join(name.split())
    if (
        not name
        or len(name) > MAX_NAME_LENGTH
        or any(unicodedata.category(c) == "Cc" for c in name)
    ):
        raise AccountError(NAME_REQUIRED)
    return name


def is_unique_violation(exc: IntegrityError) -> bool:
    orig = exc.orig
    if getattr(orig, "sqlstate", None) == "23505":
        return True
    return isinstance(orig, sqlite3.IntegrityError) and "UNIQUE constraint failed" in str(orig)


# --- reading ----------------------------------------------------------------------------------


def list_staff(db: Session, agency_id: int) -> list[StaffRow]:
    users = list(
        db.scalars(
            select(User)
            .where(User.agency_id == agency_id)
            .order_by(User.is_active.desc(), User.display_name, User.id)
        )
    )
    signs: dict[int, list[StaffSignLink]] = {}
    for link in db.scalars(
        select(StaffSignLink)
        .where(StaffSignLink.agency_id == agency_id)
        .order_by(StaffSignLink.staff_sign)
    ):
        signs.setdefault(link.user_id, []).append(link)
    return [StaffRow(u, signs.get(u.id, [])) for u in users]


def user_query(agency_id: int, user_id: int, *, lock: bool = False) -> Select[Any]:
    statement = select(User).where(User.agency_id == agency_id, User.id == user_id)
    return statement.with_for_update() if lock else statement


def get_user(db: Session, agency_id: int, user_id: int, *, lock: bool = False) -> User | None:
    """The user of this agency, or None. ``lock`` takes ``FOR UPDATE`` (a no-op on SQLite): two
    managers acting on the same account then run one after the other."""
    return db.scalars(user_query(agency_id, user_id, lock=lock)).one_or_none()


def get_signs(db: Session, agency_id: int, user_id: int) -> list[StaffSignLink]:
    return list(
        db.scalars(
            select(StaffSignLink)
            .where(StaffSignLink.agency_id == agency_id, StaffSignLink.user_id == user_id)
            .order_by(StaffSignLink.staff_sign)
        )
    )


# --- creating -----------------------------------------------------------------------------------


def create_user(
    db: Session,
    manager: User,
    *,
    display_name: str,
    phone: str,
    role: str,
    now: datetime,
    ip: str | None,
) -> tuple[User, str]:
    """Create an account and issue its first temporary password. Returns (user, plaintext).

    The caller commits. A taken phone (on any agency) raises ``AccountError(DUPLICATE_PHONE)``.
    """
    name = clean_display_name(display_name)
    if role not in ROLES:
        raise AccountError(BAD_ROLE)
    try:
        e164 = normalize_phone(phone)
    except InvalidPhoneError as exc:
        raise AccountError(str(exc)) from None
    user = User(
        agency_id=manager.agency_id,
        phone=e164,
        role=role,
        display_name=name,
        password_hash=None,
        must_change_password=True,
    )
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        verify_dummy()  # same time as a successful creation, which hashes a password
        raise AccountError(DUPLICATE_PHONE) from None
    plaintext = temp_passwords.issue_temporary_password(
        db, user, now, actor_user_id=manager.id, ip=ip
    )
    audit.record(
        db,
        audit.USER_CREATED,
        agency_id=manager.agency_id,
        actor_user_id=manager.id,
        ip=ip,
        details={"user_id": user.id, "role": role, "phone": mask_phone(e164)},
        now=now,
    )
    return user, plaintext


# --- signs --------------------------------------------------------------------------------------


def link_sign(
    db: Session, manager: User, user: User, raw_sign: str, now: datetime, ip: str | None
) -> None:
    sign = normalize_staff_sign(raw_sign)
    link = StaffSignLink(agency_id=user.agency_id, user_id=user.id, staff_sign=sign)
    try:
        with db.begin_nested():
            db.add(link)
            db.flush()
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        raise AccountError(DUPLICATE_SIGN) from None
    audit.record(
        db,
        audit.SIGN_LINKED,
        agency_id=manager.agency_id,
        actor_user_id=manager.id,
        ip=ip,
        details={"user_id": user.id, "sign": sign},
        now=now,
    )


def unlink_sign(
    db: Session, manager: User, user: User, sign_id: int, now: datetime, ip: str | None
) -> bool:
    """Remove one link of this user. False when there is no such link (a 404)."""
    link = db.scalars(
        select(StaffSignLink).where(
            StaffSignLink.agency_id == user.agency_id,
            StaffSignLink.user_id == user.id,
            StaffSignLink.id == sign_id,
        )
    ).one_or_none()
    if link is None:
        return False
    sign = link.staff_sign
    db.execute(delete(StaffSignLink).where(StaffSignLink.id == link.id))
    audit.record(
        db,
        audit.SIGN_UNLINKED,
        agency_id=manager.agency_id,
        actor_user_id=manager.id,
        ip=ip,
        details={"user_id": user.id, "sign": sign},
        now=now,
    )
    return True


# --- deactivating and changing roles -------------------------------------------------------------


def _would_leave_no_manager(db: Session, user: User) -> bool:
    """True if ``user`` is the only active manager of the agency (managers locked first)."""
    if user.role != ROLE_MANAGER or not user.is_active:
        return False
    managers = db.scalars(
        select(User)
        .where(
            User.agency_id == user.agency_id, User.role == ROLE_MANAGER, User.is_active.is_(True)
        )
        .order_by(User.id)
        .with_for_update()
    ).all()
    return all(m.id == user.id for m in managers)


def deactivate_user(db: Session, manager: User, user: User, now: datetime, ip: str | None) -> None:
    if _would_leave_no_manager(db, user):
        raise AccountError(LAST_MANAGER)
    user.is_active = False
    user.updated_at = now
    sessions.revoke_all_for_user(db, user.agency_id, user.id, now)
    temp_passwords.retire_pending(db, user, now)
    audit.record(
        db,
        audit.USER_DEACTIVATED,
        agency_id=manager.agency_id,
        actor_user_id=manager.id,
        ip=ip,
        details={"user_id": user.id},
        now=now,
    )


def change_role(
    db: Session, manager: User, user: User, role: str, now: datetime, ip: str | None
) -> None:
    if role not in ROLES:
        raise AccountError(BAD_ROLE)
    if role == user.role:
        return
    if role != ROLE_MANAGER and _would_leave_no_manager(db, user):
        raise AccountError(LAST_MANAGER)
    previous = user.role
    user.role = role
    user.updated_at = now
    audit.record(
        db,
        audit.USER_ROLE_CHANGED,
        agency_id=manager.agency_id,
        actor_user_id=manager.id,
        ip=ip,
        details={"user_id": user.id, "from": previous, "to": role},
        now=now,
    )
