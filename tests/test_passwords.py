"""Argon2id hashing, the password policy and temporary passwords."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable

import pytest
from argon2 import PasswordHasher, Type

from app.auth import passwords
from app.auth.passwords import (
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


def _best_of(fn: Callable[[], object], runs: int = 4) -> float:
    best = float("inf")
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def test_dummy_verify_costs_about_as_much_as_a_real_verify(good_hash: str) -> None:
    verify_dummy()  # warm up (builds the dummy hash once)
    real = _best_of(lambda: verify_password("wrong password!", good_hash))
    dummy = _best_of(verify_dummy)
    # Generous bounds: the point is "same order of magnitude", not equality.
    assert real / 5 < dummy < real * 5
    assert dummy > 0.001  # it really hashes, it does not return immediately


def test_dummy_verify_never_raises() -> None:
    verify_dummy()
    verify_dummy()


def test_missing_password_hash_also_spends_the_dummy_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []
    monkeypatch.setattr(passwords, "verify_dummy", lambda: called.append(True))
    assert verify_password(PASSWORD, None) is False
    assert called == [True]


# --- policy --------------------------------------------------------------------------


def test_a_good_password_passes() -> None:
    assert password_policy_error("a" * 10) is None
    assert password_policy_error("a" * 128) is None
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
    assert password_policy_error("é" * 10) is None
    assert password_policy_error("é" * 9) is not None
    assert password_policy_error("\U0001f600" * 10) is None


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
