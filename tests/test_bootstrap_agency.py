"""CLI ``bootstrap-agency``: the first agency and its manager, created in one transaction.

Cross-engine tests run on SQLite and on Postgres (as the owner, single-role). The RLS tests at
the end run on Postgres in both deployment modes (``rls_db``).
"""

from __future__ import annotations

import getpass
import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.passwords import TOO_SHORT, hash_password, verify_password
from app.cli import main
from app.db import make_engine, make_session_factory
from app.models import Agency, User
from app.repository import AlreadyExistsError, bootstrap_agency
from app.tenancy import bind_tenant, lookup_scope
from tests.conftest import RlsDatabase

PASSWORD = "correct horse battery"  # noqa: S105
PHONE = "+22212345678"
PHONE_TYPED = "12 34 56 78"
MANAGER = "Aminetou Mint Sidi"


@contextmanager
def open_session(url: str) -> Iterator[Session]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            yield s
    finally:
        engine.dispose()


def find_agency(url: str, slug: str) -> Agency | None:
    with open_session(url) as s:
        lookup_scope(s, "app.agency_slug", slug)
        return s.scalars(select(Agency).where(Agency.slug == slug)).one_or_none()


def find_user(url: str, phone: str) -> User | None:
    with open_session(url) as s:
        lookup_scope(s, "app.login_phone", phone)
        return s.scalars(select(User).where(User.phone == phone)).one_or_none()


def users_of(url: str, agency_id: int) -> list[User]:
    with open_session(url) as s:
        bind_tenant(s, agency_id)
        return list(s.scalars(select(User)))


@pytest.fixture
def ready_db(db_url: str) -> str:
    assert main(["init-db", "--db", db_url]) == 0
    return db_url


@pytest.fixture
def stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))


def bootstrap(
    db: str,
    *extra: str,
    slug: str = "bruge-voyages",
    name: str = "Bruge Voyages",
    phone: str = PHONE_TYPED,
    manager: str = MANAGER,
    stdin_flag: bool = True,
) -> int:
    argv = [
        "bootstrap-agency",
        slug,
        name,
        "--manager-phone",
        phone,
        "--manager-name",
        manager,
        "--db",
        db,
        *extra,
    ]
    if stdin_flag:
        argv.append("--password-stdin")
    return main(argv)


def set_stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


# --- the happy path ------------------------------------------------------------------


def test_creates_the_agency_and_a_manager_from_stdin(
    ready_db: str, stdin: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert bootstrap(ready_db) == 0
    out = capsys.readouterr()
    ids = json.loads(out.out)
    assert set(ids) == {"agency_id", "user_id"}  # ids only
    assert all(isinstance(v, int) for v in ids.values())
    agency = find_agency(ready_db, "bruge-voyages")
    assert agency is not None and agency.id == ids["agency_id"] and agency.name == "Bruge Voyages"
    user = find_user(ready_db, PHONE)
    assert user is not None and user.id == ids["user_id"]
    assert user.agency_id == agency.id
    assert user.role == "manager"
    assert user.display_name == MANAGER
    assert user.must_change_password is False
    assert user.is_active is True
    assert user.password_hash is not None and user.password_hash.startswith("$argon2id$")
    assert verify_password(PASSWORD, user.password_hash)
    assert not verify_password("wrong password!", user.password_hash)


def test_stdin_takes_one_line_and_keeps_inner_spaces(
    ready_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_stdin(monkeypatch, "my pass phrase\r\nignored second line\n")
    assert bootstrap(ready_db) == 0
    user = find_user(ready_db, PHONE)
    assert user is not None and user.password_hash is not None
    assert verify_password("my pass phrase", user.password_hash)


def test_arabic_manager_name_roundtrips(ready_db: str, stdin: None) -> None:
    assert bootstrap(ready_db, manager="أمينتو بنت سيدي") == 0
    user = find_user(ready_db, PHONE)
    assert user is not None and user.display_name == "أمينتو بنت سيدي"


def test_prompts_twice_with_getpass_when_not_reading_stdin(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([PASSWORD, PASSWORD])
    prompts: list[str] = []

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    assert bootstrap(ready_db, stdin_flag=False) == 0
    assert len(prompts) == 2
    assert all(PASSWORD not in p for p in prompts)
    user = find_user(ready_db, PHONE)
    assert user is not None and user.password_hash is not None
    assert verify_password(PASSWORD, user.password_hash)
    assert PASSWORD not in capsys.readouterr().out


def test_mismatched_confirmation_creates_nothing(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([PASSWORD, PASSWORD + "x"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(answers))
    assert bootstrap(ready_db, stdin_flag=False) == 2
    err = capsys.readouterr()
    assert "do not match" in err.err
    assert PASSWORD not in err.err + err.out
    assert find_agency(ready_db, "bruge-voyages") is None
    assert find_user(ready_db, PHONE) is None


# --- refusals (exit code 2, nothing created, the password is never echoed) ------------


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", TOO_SHORT),
        ("+22212345678", "numéro de téléphone"),
        ("22212345678", "numéro de téléphone"),
        ("", "no password"),
    ],
)
def test_password_policy_failures(
    ready_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    password: str,
    message: str,
) -> None:
    set_stdin(monkeypatch, password + ("\n" if password else ""))
    assert bootstrap(ready_db) == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out == ""
    if password:
        assert password not in captured.err
    assert find_agency(ready_db, "bruge-voyages") is None
    assert find_user(ready_db, PHONE) is None


@pytest.mark.parametrize("phone", ["abc", "1234", "+0", "", "12 34 56 7x"])
def test_invalid_phone_is_refused(
    ready_db: str, stdin: None, capsys: pytest.CaptureFixture[str], phone: str
) -> None:
    assert bootstrap(ready_db, phone=phone) == 2
    assert "téléphone" in capsys.readouterr().err
    assert find_agency(ready_db, "bruge-voyages") is None


@pytest.mark.parametrize(
    ("kwargs", "word"),
    [
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": "x_y"}, "slug"),
        ({"name": "  "}, "name"),
        ({"name": "x" * 201}, "name"),
        ({"manager": ""}, "manager"),
        ({"manager": "y" * 201}, "manager"),
    ],
)
def test_invalid_names_are_refused(
    ready_db: str,
    stdin: None,
    capsys: pytest.CaptureFixture[str],
    kwargs: dict[str, Any],
    word: str,
) -> None:
    assert bootstrap(ready_db, **kwargs) == 2
    assert word in capsys.readouterr().err
    assert find_user(ready_db, PHONE) is None


def test_duplicate_slug_is_refused_and_creates_no_user(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(ready_db) == 0
    capsys.readouterr()
    set_stdin(monkeypatch, "another secret phrase\n")
    assert bootstrap(ready_db, phone="+22287654321") == 2
    captured = capsys.readouterr()
    assert "already exists" in captured.err and "bruge-voyages" in captured.err
    assert "another secret phrase" not in captured.err + captured.out
    assert find_user(ready_db, "+22287654321") is None


def test_duplicate_phone_is_refused_and_creates_no_agency(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(ready_db) == 0
    capsys.readouterr()
    set_stdin(monkeypatch, "another secret phrase\n")
    # the same number, typed differently
    assert bootstrap(ready_db, slug="second", phone="+222 12 34 56 78") == 2
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert "another secret phrase" not in captured.err + captured.out
    assert find_agency(ready_db, "second") is None


def test_the_agency_and_the_manager_are_one_transaction(ready_db: str) -> None:
    with open_session(ready_db) as s:
        with pytest.raises(IntegrityError):  # the phone breaks the E.164 CHECK after the agency
            bootstrap_agency(s, "half", "Half", "not-a-phone", "Name", hash_password(PASSWORD))
        assert find_agency(ready_db, "half") is None


def test_the_repository_reports_which_key_is_taken(ready_db: str) -> None:
    with open_session(ready_db) as s:
        bootstrap_agency(s, "one", "One", PHONE, "A", hash_password(PASSWORD))
    with open_session(ready_db) as s, pytest.raises(AlreadyExistsError, match="slug"):
        bootstrap_agency(s, "one", "One", "+22287654321", "B", hash_password(PASSWORD))
    with open_session(ready_db) as s, pytest.raises(AlreadyExistsError, match="phone"):
        bootstrap_agency(s, "two", "Two", PHONE, "B", hash_password(PASSWORD))


# --- the password is never an argument, and never stored or printed in clear -----------


@pytest.mark.parametrize(
    "flag",
    [
        ["--password", "hunter2hunter2"],
        ["--password=hunter2hunter2"],
        ["--passw", "hunter2hunter2"],
    ],
)
def test_a_password_argument_is_refused_without_echoing_it(
    ready_db: str, stdin: None, capsys: pytest.CaptureFixture[str], flag: list[str]
) -> None:
    assert bootstrap(ready_db, *flag) == 2
    captured = capsys.readouterr()
    assert "hunter2" not in captured.err + captured.out
    assert "--password-stdin" in captured.err
    assert find_agency(ready_db, "bruge-voyages") is None


def test_help_documents_stdin_and_has_no_password_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["bootstrap-agency", "--help"])
    text = capsys.readouterr().out
    assert "--password-stdin" in text
    assert "--password " not in text and "--password=" not in text


def test_the_password_appears_nowhere(
    ready_db: str,
    stdin: None,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert bootstrap(ready_db) == 0
    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    assert PASSWORD not in everything
    agency = find_agency(ready_db, "bruge-voyages")
    assert agency is not None
    (user,) = users_of(ready_db, agency.id)
    assert user.password_hash is not None
    assert user.password_hash not in everything  # not even the hash is printed
    stored = [str(getattr(user, c.key)) for c in User.__table__.columns]
    assert all(PASSWORD not in value for value in stored)
    assert PASSWORD not in repr(agency.name)


# --- Postgres, under row-level security, in both modes ---------------------------------


def test_bootstrap_under_rls_then_the_app_connection_can_log_in_and_is_isolated(
    rls_db: RlsDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(rls_db.owner_url) == 0
    first = json.loads(capsys.readouterr().out)
    set_stdin(monkeypatch, "second agency password\n")
    assert bootstrap(rls_db.owner_url, slug="second", name="Second", phone="+22287654321") == 0
    second = json.loads(capsys.readouterr().out)
    assert first["agency_id"] != second["agency_id"]

    # What the web application does: connect as itself and look the account up by exact phone.
    user = find_user(rls_db.app_url, PHONE)
    assert user is not None and user.id == first["user_id"]
    assert user.password_hash is not None and verify_password(PASSWORD, user.password_hash)
    assert find_user(rls_db.app_url, "+22287654321") is not None
    assert find_agency(rls_db.app_url, "second") is not None
    with open_session(rls_db.app_url) as s:  # nobody is bound: nothing is visible
        assert s.scalars(select(User.id)).all() == []
        assert s.scalars(select(Agency.id)).all() == []
    assert [u.id for u in users_of(rls_db.app_url, first["agency_id"])] == [first["user_id"]]
    assert [u.id for u in users_of(rls_db.app_url, second["agency_id"])] == [second["user_id"]]


def test_duplicates_are_detected_across_agencies_under_rls(
    rls_db: RlsDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(rls_db.owner_url) == 0
    capsys.readouterr()
    # The existing manager belongs to another tenant, invisible to the new session: the narrow
    # phone lookup (and, failing that, the unique key) still catches the duplicate.
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(rls_db.owner_url, slug="second") == 2
    assert "already exists" in capsys.readouterr().err
    assert find_agency(rls_db.owner_url, "second") is None
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(rls_db.owner_url, phone="+22287654321") == 2
    assert "already exists" in capsys.readouterr().err


def test_the_app_role_cannot_bootstrap_and_nothing_is_left_behind(
    rls_db: RlsDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if rls_db.mode != "app-role":
        pytest.skip("the owner is the app connection in single-role mode")
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert bootstrap(rls_db.app_url) == 1
    captured = capsys.readouterr()
    assert "permission denied" in captured.err
    assert "MIGRATION_DATABASE_URL" in captured.err
    assert PASSWORD not in captured.err + captured.out
    assert find_agency(rls_db.owner_url, "bruge-voyages") is None


def test_no_plaintext_password_in_any_table_under_rls(
    rls_db: RlsDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    with caplog.at_level(logging.DEBUG):
        assert bootstrap(rls_db.owner_url) == 0
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err + caplog.text
    ids = json.loads(captured.out)
    with open_session(rls_db.app_url) as s:
        bind_tenant(s, ids["agency_id"])
        for user in s.scalars(select(User)):
            assert all(PASSWORD not in str(getattr(user, c.key)) for c in User.__table__.columns)
