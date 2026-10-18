"""Argon2id hashing, the password policy and temporary passwords."""

from __future__ import annotations

import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from argon2 import PasswordHasher, Type

from app.auth import passwords
from app.auth.common_passwords import COMMON_PASSWORDS
from app.auth.passwords import (
    MAX_VERIFY_LENGTH,
    TEMP_PASSWORD_ALPHABET,
    TEMP_PASSWORD_LENGTH,
    generate_temporary_password,
    hash_password,
    needs_rehash,
    password_policy_error,
    verify_dummy,
    verify_password,
)

PASSWORD = "correct horse battery"  # noqa: S105 - a test value, not a credential


@pytest.fixture(scope="module")
def good_hash() -> str:
    return hash_password(PASSWORD)


# --- hashing -------------------------------------------------------------------------


def test_uses_argon2id_with_the_owasp_parameters(good_hash: str) -> None:
    assert good_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")


def test_the_hash_never_contains_the_password(good_hash: str) -> None:
    assert PASSWORD not in good_hash


def test_salts_differ_between_hashes(good_hash: str) -> None:
    assert hash_password(PASSWORD) != good_hash


def test_verify_accepts_the_right_password(good_hash: str) -> None:
    assert verify_password(PASSWORD, good_hash) is True


@pytest.mark.parametrize("wrong", ["", "x", PASSWORD + " ", PASSWORD.upper(), "correct horse"])
def test_verify_rejects_a_wrong_password(good_hash: str, wrong: str) -> None:
    assert verify_password(wrong, good_hash) is False


@pytest.mark.parametrize(
    "bad_hash",
    [
        "",
        "not a hash",
        "$argon2id$garbage",
        "$2b$12$abcdefghijklmnopqrstuuabcdefghijklmnopqrstuvwxyz0123",
        "\x00",
    ],
)
def test_verify_is_false_for_an_invalid_hash(bad_hash: str) -> None:
    assert verify_password(PASSWORD, bad_hash) is False


def test_verify_is_false_when_the_user_has_no_password() -> None:
    assert verify_password(PASSWORD, None) is False


def test_verify_never_raises_on_odd_input(good_hash: str) -> None:
    assert verify_password("é" * 50 + "\U0001f600", good_hash) is False
    assert verify_password("a" * 100_000, good_hash) is False


def test_unicode_passwords_round_trip() -> None:
    pw = "mot de passe très sûr \U0001f511 كلمة السر"
    assert verify_password(pw, hash_password(pw)) is True


def test_a_tampered_hash_does_not_verify(good_hash: str) -> None:
    tampered = good_hash[:-4] + ("AAAA" if not good_hash.endswith("AAAA") else "BBBB")
    assert verify_password(PASSWORD, tampered) is False


# --- rehash --------------------------------------------------------------------------


def test_current_parameters_need_no_rehash(good_hash: str) -> None:
    assert needs_rehash(good_hash) is False


def test_weaker_parameters_need_a_rehash() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1, type=Type.ID).hash(PASSWORD)
    assert needs_rehash(weak) is True
    assert verify_password(PASSWORD, weak) is True  # still verifies, so a login can upgrade it


def test_argon2i_hash_needs_a_rehash() -> None:
    other = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.I).hash(
        PASSWORD
    )
    assert needs_rehash(other) is True


def test_an_unparseable_hash_needs_a_rehash() -> None:
    assert needs_rehash("garbage") is True


# --- unknown users take the same time ------------------------------------------------


class SpyHasher:
    """Wraps the real hasher and records every verify call (hash, password)."""

    def __init__(self, real: PasswordHasher) -> None:
        self._real = real
        self.verify_calls: list[tuple[str, str]] = []

    def verify(self, password_hash: str, password: str) -> bool:
        self.verify_calls.append((password_hash, password))
        return self._real.verify(password_hash, password)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> SpyHasher:
    hasher = SpyHasher(passwords._HASHER)
    monkeypatch.setattr(passwords, "_HASHER", hasher)
    return hasher


def _failure_cases(good_hash: str) -> list[tuple[str, str | None]]:
    return [
        ("wrong password!", good_hash),  # the normal failure
        (PASSWORD, None),  # no hash yet
        (PASSWORD, ""),  # bad stored hashes
        (PASSWORD, "not a hash"),
        (PASSWORD, "$argon2id$garbage"),
        (PASSWORD, "\x00"),
        ("a" * (MAX_VERIFY_LENGTH + 1), good_hash),  # over-long
        ("bad \ud800 surrogate", good_hash),  # not encodable
    ]


def test_every_failure_path_spends_exactly_one_verification(spy: SpyHasher, good_hash: str) -> None:
    for password, stored in _failure_cases(good_hash):
        spy.verify_calls.clear()
        assert verify_password(password, stored) is False
        assert len(spy.verify_calls) == 1, (password[:12], stored)


def test_the_success_path_spends_one_verification_too(spy: SpyHasher, good_hash: str) -> None:
    assert verify_password(PASSWORD, good_hash) is True
    assert len(spy.verify_calls) == 1


def test_the_dummy_costs_a_real_verification_and_never_sees_the_input(
    spy: SpyHasher, good_hash: str
) -> None:
    long_password = "a" * (MAX_VERIFY_LENGTH + 1)
    verify_password(long_password, good_hash)
    ((hash_used, password_used),) = spy.verify_calls
    assert password_used != long_password  # the hasher never receives the oversized input
    assert hash_used.startswith("$argon2id$v=19$m=19456,t=2,p=1$")  # same cost as a real hash


def test_timing_smoke_test_failure_paths_cost_about_one_verify(good_hash: str) -> None:
    """One generous wall-clock check; the spy tests above are the precise ones."""
    verify_dummy()

    def best_of(fn: Callable[[], object], runs: int = 3) -> float:
        best = float("inf")
        for _ in range(runs):
            start = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - start)
        return best

    real = best_of(lambda: verify_password("wrong password!", good_hash))
    cases = [(PASSWORD, None), ("a" * 5000, good_hash), (PASSWORD, "garbage")]
    for password, stored in cases:
        elapsed = best_of(lambda p=password, h=stored: verify_password(p, h))  # type: ignore[misc]
        assert real / 5 < elapsed < real * 5


def test_dummy_verify_never_raises() -> None:
    verify_dummy()
    verify_dummy()


def test_the_dummy_hash_is_built_at_import_not_on_the_first_login() -> None:
    root = Path(__file__).resolve().parent.parent
    code = "import app.auth.passwords as p; print(p._dummy_hash.cache_info().currsize)"
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "1"


def test_the_length_guard_is_what_stops_oversized_input() -> None:
    """Without the guard the right (huge) password would verify: only the guard makes it fail."""
    exact = "a" * MAX_VERIFY_LENGTH
    too_long = "a" * (MAX_VERIFY_LENGTH + 1)
    assert verify_password(exact, hash_password(exact)) is True
    assert verify_password(too_long, hash_password(too_long)) is False


# --- strings that cannot be UTF-8 -----------------------------------------------------

SURROGATES = ["\ud800", "abc\udfffdef" * 3, "a" * 20 + "\ud83d"]


@pytest.mark.parametrize("bad", SURROGATES)
def test_surrogates_never_verify_and_never_raise(good_hash: str, bad: str) -> None:
    assert verify_password(bad, good_hash) is False
    assert verify_password(bad, None) is False


@pytest.mark.parametrize("bad", SURROGATES)
def test_surrogates_cannot_be_hashed(bad: str) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        hash_password(bad)


@pytest.mark.parametrize("bad", SURROGATES)
def test_surrogates_are_refused_by_the_policy_in_french(bad: str) -> None:
    message = password_policy_error(bad)
    assert message is not None
    assert "caractères" in message
    assert bad not in message


def test_real_astral_characters_are_not_surrogates() -> None:
    pw = "clé \U0001f511 secrète"
    assert password_policy_error(pw) is None
    assert verify_password(pw, hash_password(pw)) is True


# --- policy --------------------------------------------------------------------------


def test_a_good_password_passes() -> None:
    assert password_policy_error("abcdefghjk") is None
    assert password_policy_error("ab" * 64) is None
    assert password_policy_error(PASSWORD, "+22212345678") is None


@pytest.mark.parametrize("pw", ["", "a" * 9, "123456789"])
def test_too_short_is_refused_in_french(pw: str) -> None:
    message = password_policy_error(pw)
    assert message is not None
    assert "10" in message
    assert "caractères" in message


def test_too_long_is_refused_in_french() -> None:
    message = password_policy_error("a" * 129)
    assert message is not None
    assert "128" in message


def test_length_counts_characters_not_bytes() -> None:
    assert password_policy_error("éè" * 5) is None
    assert password_policy_error("éè" * 4 + "é") == passwords.TOO_SHORT
    assert password_policy_error("\U0001f600\U0001f601" * 5) is None


@pytest.mark.parametrize(
    "pw",
    [
        "+22212345678",
        "22212345678",
        "0022212345678",
        "+222 12 34 56 78",
        "+222-12-34-56-78",
        "00222 12 34 56 78",
        "222.12.34.56.78",
    ],
)
def test_password_equal_to_the_phone_in_any_form_is_refused(pw: str) -> None:
    for phone in ("+22212345678", "12 34 56 78", "00222 12 34 56 78"):
        message = password_policy_error(pw, phone)
        assert message is not None
        assert "téléphone" in message


def test_the_local_form_is_refused_even_when_padded_to_length() -> None:
    # 8 digits are already too short, so this documents that length is checked first.
    message = password_policy_error("12345678", "+22212345678")
    assert message is not None


def test_a_password_containing_the_phone_among_other_text_is_allowed() -> None:
    assert password_policy_error("x+22212345678x", "+22212345678") is None


def test_other_countries_phone_forms() -> None:
    assert password_policy_error("+33612345678", "+33 6 12 34 56 78") is not None
    assert password_policy_error("0033612345678", "+33612345678") is not None


def test_an_unparseable_phone_does_not_block_a_password() -> None:
    assert password_policy_error(PASSWORD, "not a phone") is None


# --- weak passwords -------------------------------------------------------------------


@pytest.mark.parametrize("pw", ["aaaaaaaaaa", "0000000000", "z" * 16, "é" * 12, "a" * 128])
def test_a_single_repeated_character_is_refused_in_french(pw: str) -> None:
    message = password_policy_error(pw)
    assert message is not None
    assert "répété" in message


def test_two_alternating_characters_are_not_a_single_repeat() -> None:
    assert password_policy_error("abababababab") is None


@pytest.mark.parametrize(
    "pw", ["password12", "Password12", "PASSWORD12", "PaSsWoRd12", "1234567890", "qwertyuiop"]
)
def test_common_passwords_are_refused_in_any_case(pw: str) -> None:
    message = password_policy_error(pw)
    assert message is not None
    assert "courant" in message


def test_the_denylist_is_small_lowercase_and_long_enough_to_matter() -> None:
    assert 200 <= len(COMMON_PASSWORDS) <= 2000
    assert all(entry == entry.casefold() for entry in COMMON_PASSWORDS)
    assert all(len(entry) >= 10 for entry in COMMON_PASSWORDS)  # shorter ones fail on length


def test_a_phrase_that_merely_contains_a_common_word_is_allowed() -> None:
    assert password_policy_error("correct horse battery staple") is None
    assert password_policy_error("my password is long enough") is None


def test_length_is_still_checked_before_the_denylist() -> None:
    assert password_policy_error("password1") == passwords.TOO_SHORT


# --- temporary passwords -------------------------------------------------------------


def test_temporary_password_shape() -> None:
    pw = generate_temporary_password()
    assert len(pw) == TEMP_PASSWORD_LENGTH == 12
    assert set(pw) <= set(TEMP_PASSWORD_ALPHABET)
    assert password_policy_error(pw) is None


def test_alphabet_has_no_ambiguous_characters() -> None:
    assert not set("0O1lIo") & set(TEMP_PASSWORD_ALPHABET)
    assert len(set(TEMP_PASSWORD_ALPHABET)) == len(TEMP_PASSWORD_ALPHABET) >= 50
    assert TEMP_PASSWORD_ALPHABET.isascii()
    assert TEMP_PASSWORD_ALPHABET.isalnum()


def test_temporary_passwords_are_random() -> None:
    generated = {generate_temporary_password() for _ in range(500)}
    assert len(generated) == 500
    seen = set("".join(generated))
    assert len(seen) > 40  # draws from the whole alphabet


def test_temporary_password_uses_the_secrets_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets, "choice", lambda seq: seq[0])
    assert generate_temporary_password() == TEMP_PASSWORD_ALPHABET[0] * 12


def test_temporary_password_verifies_after_hashing() -> None:
    pw = generate_temporary_password()
    assert verify_password(pw, hash_password(pw)) is True
