"""Password hashing (Argon2id), the password policy and temporary passwords.

Argon2id with the OWASP minimum configuration: 19 MiB of memory, 2 iterations, 1 lane. The
parameters are explicit so a library default change cannot silently weaken (or slow) logins;
``needs_rehash`` tells a successful login when a stored hash is older than these settings.
"""

from __future__ import annotations

import contextlib
import secrets
from functools import cache

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.auth.phone import InvalidPhoneError, normalize_phone, phone_digit_forms

MEMORY_COST_KIB = 19 * 1024  # 19 MiB
TIME_COST = 2
PARALLELISM = 1

MIN_LENGTH = 10
MAX_LENGTH = 128
# Longer input is never a real password; refuse it before it reaches the hasher.
MAX_VERIFY_LENGTH = 1024

# Unambiguous when read aloud or copied by hand: no 0/O/o, 1/l/I.
_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"  # no I, O
_LOWER = "abcdefghijkmnpqrstuvwxyz"  # no l, o
_DIGITS = "23456789"  # no 0, 1
TEMP_PASSWORD_ALPHABET = _UPPER + _LOWER + _DIGITS
TEMP_PASSWORD_LENGTH = 12

_HASHER = PasswordHasher(
    time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB, parallelism=PARALLELISM, type=Type.ID
)
_PHONE_TEXT_CHARS = frozenset("0123456789+ -. ")

TOO_SHORT = f"Le mot de passe doit contenir au moins {MIN_LENGTH} caractères."
TOO_LONG = f"Le mot de passe ne doit pas dépasser {MAX_LENGTH} caractères."
IS_THE_PHONE = "Le mot de passe ne doit pas être identique au numéro de téléphone."


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """True only for the right password; False for a mismatch, a bad hash or no hash at all.

    A missing hash (account without a password yet) still spends a full hash's worth of time.
    """
    if password_hash is None:
        verify_dummy()
        return False
    if len(password) > MAX_VERIFY_LENGTH:
        return False
    try:
        return _HASHER.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True when the hash was made with other parameters (or cannot be read at all)."""
    try:
        return _HASHER.check_needs_rehash(password_hash)
    except (VerificationError, InvalidHashError):
        return True


@cache
def _dummy_hash() -> str:
    # Built from the live parameters so it always costs what a real verification costs.
    return _HASHER.hash(secrets.token_urlsafe(16))


def verify_dummy() -> None:
    """Spend the time of one verification; call it when the user does not exist."""
    with contextlib.suppress(VerificationError):
        _HASHER.verify(_dummy_hash(), secrets.token_urlsafe(16))


def password_policy_error(password: str, phone: str | None = None) -> str | None:
    """A French error message when the password is refused, else None."""
    if len(password) < MIN_LENGTH:
        return TOO_SHORT
    if len(password) > MAX_LENGTH:
        return TOO_LONG
    if phone is not None and _is_the_phone(password, phone):
        return IS_THE_PHONE
    return None


def _is_the_phone(password: str, phone: str) -> bool:
    """The password is the phone number, however it is spelled (+, 00, spaces, local form)."""
    try:
        e164 = normalize_phone(phone)
    except InvalidPhoneError:
        return False
    if not set(password) <= _PHONE_TEXT_CHARS:
        return False
    digits = "".join(ch for ch in password if ch.isdigit())
    return digits in phone_digit_forms(e164)


def generate_temporary_password() -> str:
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_LENGTH))
