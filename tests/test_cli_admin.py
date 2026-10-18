"""CLI hardening: argparse never echoes arguments, clean password prompts, honest error mapping,
the bootstrap audit event, and the ``grant-app-role`` and ``purge-expired`` commands."""

from __future__ import annotations

import getpass
import io
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import psycopg.errors
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

import app.cli
import app.repository
from app import db_roles
from app.cli import EXIT_FAILURE, EXIT_USAGE, _is_unique_violation, main
from app.db import make_engine, make_session_factory
from app.db_roles import GRANTS
from app.models import AuditEvent, User, UserSession
from app.repository import create_agency
from app.tenancy import bind_tenant
from tests.conftest import TEST_APP_DATABASE_URL_ENV, RlsDatabase, migrate

SECRET = "hunter2hunter2"  # noqa: S105 - a test value that must never be echoed
PASSWORD = "correct horse battery"  # noqa: S105
PHONE_TYPED = "12 34 56 78"
BASE = ["bootstrap-agency", "bruge-voyages", "Bruge Voyages"]
REQUIRED = ["--manager-phone", PHONE_TYPED, "--manager-name", "Aminetou"]


@pytest.fixture
def ready_db(db_url: str) -> str:
    assert main(["init-db", "--db", db_url]) == 0
    return db_url


def set_stdin(monkeypatch: pytest.MonkeyPatch, text_: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text_))


# --- argparse never echoes what was typed ----------------------------------------------------

ECHO_CASES = [
    pytest.param([*BASE, *REQUIRED, "--pa", SECRET], id="abbreviated --pa x"),
    pytest.param([*BASE, *REQUIRED, "--password-stdin=" + SECRET], id="--password-stdin=x"),
    pytest.param([*BASE, *REQUIRED, SECRET], id="stray positional"),
    pytest.param([*BASE, *REQUIRED, "--no-such-option", SECRET], id="unknown option"),
    pytest.param(["import", SECRET, "--agency"], id="option missing its value"),
    pytest.param(["init-db", SECRET], id="positional on a command without any"),
    pytest.param([SECRET], id="unknown command"),
    pytest.param(["import", "f.xlsx", "--agency", "a", "--date", SECRET], id="bad date"),
    pytest.param(["grant-app-role", "bruge_app", SECRET], id="grant-app-role extra"),
]


@pytest.mark.parametrize("argv", ECHO_CASES)
def test_a_command_line_error_never_echoes_the_offending_tokens(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == EXIT_USAGE
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert captured.out == ""
    assert "usage:" in captured.err
    assert "exit code 2" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "flag",
    [["-p", SECRET], ["-p" + SECRET], ["-p=" + SECRET], ["--password", SECRET]],
    ids=["-p x", "-pX", "-p=x", "--password x"],
)
def test_a_short_password_option_is_refused_without_echo(
    ready_db: str,
    flag: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert main([*BASE, *REQUIRED, *flag, "--db", ready_db, "--password-stdin"]) == EXIT_USAGE
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "--password-stdin" in captured.err


def test_help_still_works_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(["bootstrap-agency", "--help"])
    assert info.value.code == 0
    assert "--password-stdin" in capsys.readouterr().out


def test_help_names_password_stdin_as_the_non_interactive_path_on_windows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        main(["bootstrap-agency", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "Windows" in out and "non-interactive" in out and "--password-stdin" in out


# --- getpass ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "code", "words"),
    [
        (EOFError(), EXIT_USAGE, "end of input"),
        (KeyboardInterrupt(), 130, "cancelled"),
    ],
    ids=["EOF", "Ctrl-C"],
)
def test_the_prompt_ends_cleanly_on_eof_and_interrupt(
    ready_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: BaseException,
    code: int,
    words: str,
) -> None:
    def interrupted(prompt: str = "") -> str:
        raise error

    monkeypatch.setattr(getpass, "getpass", interrupted)
    assert main([*BASE, *REQUIRED, "--db", ready_db]) == code
    err = capsys.readouterr().err
    assert words in err
    assert "Traceback" not in err
    assert "--password-stdin" in err
    with make_session_factory(make_engine(ready_db))() as s:
        assert s.scalar(select(User.id)) is None


def test_an_interrupt_at_the_confirmation_prompt_also_ends_cleanly(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([PASSWORD])

    def second_prompt_is_interrupted(prompt: str = "") -> str:
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(getpass, "getpass", second_prompt_is_interrupted)
    assert main([*BASE, *REQUIRED, "--db", ready_db]) == 130
    assert PASSWORD not in capsys.readouterr().err


# --- only unique violations mean "already exists" -----------------------------------------------


@pytest.mark.parametrize(
    ("orig", "expected"),
    [
        (psycopg.errors.UniqueViolation("duplicate key"), True),
        (psycopg.errors.CheckViolation("check"), False),
        (psycopg.errors.ForeignKeyViolation("fk"), False),
        (psycopg.errors.NotNullViolation("null"), False),
        (sqlite3.IntegrityError("UNIQUE constraint failed: users.phone"), True),
        (sqlite3.IntegrityError("CHECK constraint failed: ck_users_phone_e164"), False),
        (sqlite3.IntegrityError("NOT NULL constraint failed: users.role"), False),
        (sqlite3.IntegrityError("FOREIGN KEY constraint failed"), False),
        (Exception("duplicate key value violates unique constraint"), False),  # not a driver error
    ],
)
def test_unique_violation_detection(orig: BaseException, expected: bool) -> None:
    assert _is_unique_violation(IntegrityError("INSERT", {}, orig)) is expected


def test_a_real_unique_violation_is_a_usage_error(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lose the race for real: the pre-check is blinded, so the database's own key says no."""
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert main([*BASE, *REQUIRED, "--db", ready_db, "--password-stdin"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(app.repository, "get_agency_by_slug", lambda *_a, **_k: None)
    set_stdin(monkeypatch, PASSWORD + "\n")
    code = main(
        [*BASE, *REQUIRED[:1], "+22287654321", *REQUIRED[2:], "--db", ready_db, "--password-stdin"]
    )
    assert code == EXIT_USAGE
    assert "already exists" in capsys.readouterr().err


def test_a_real_check_violation_is_a_database_failure_not_a_duplicate(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The phone CHECK of the schema rejects this; the message must not say "already exists"."""
    monkeypatch.setattr(app.cli, "normalize_phone", lambda _raw: "not-a-phone")
    set_stdin(monkeypatch, PASSWORD + "\n")
    code = main([*BASE, *REQUIRED, "--db", ready_db, "--password-stdin"])
    assert code == EXIT_FAILURE
    err = capsys.readouterr().err
    assert err.startswith("error: database error")
    assert "already exists" not in err
    assert PASSWORD not in err
    assert "Traceback" not in err


def test_add_agency_maps_a_non_unique_integrity_error_to_a_failure(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_a: object, **_k: object) -> None:
        raise IntegrityError("INSERT", {}, sqlite3.IntegrityError("CHECK constraint failed: x"))

    monkeypatch.setattr(app.cli, "create_agency", broken)
    assert main(["add-agency", "a", "A", "--db", ready_db]) == EXIT_FAILURE
    assert "already exists" not in capsys.readouterr().err


# --- the bootstrap audit event ------------------------------------------------------------------


def audit_rows(url: str, agency_id: int) -> list[AuditEvent]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            bind_tenant(s, agency_id)
            return list(
                s.scalars(select(AuditEvent).where(AuditEvent.event_type == "agency_bootstrapped"))
            )
    finally:
        engine.dispose()


def test_bootstrap_writes_an_audit_event_with_no_secrets(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert main([*BASE, *REQUIRED, "--db", ready_db, "--password-stdin"]) == 0
    ids = json.loads(capsys.readouterr().out)
    (event,) = audit_rows(ready_db, ids["agency_id"])
    assert event.agency_id == ids["agency_id"]
    assert event.actor_user_id is None  # nobody is logged in; the operator is named in details
    assert event.details["agency_slug"] == "bruge-voyages"
    assert event.details["manager_user_id"] == ids["user_id"]
    assert event.details["operator"]
    blob = json.dumps(event.details)
    assert PASSWORD not in blob and "argon2" not in blob and "+222" not in blob


def test_a_refused_bootstrap_writes_no_audit_event(
    ready_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_stdin(monkeypatch, "short\n")
    assert main([*BASE, *REQUIRED, "--db", ready_db, "--password-stdin"]) == EXIT_USAGE
    engine = make_engine(ready_db)
    try:
        with make_session_factory(engine)() as s:
            assert s.scalar(select(AuditEvent.id)) is None
    finally:
        engine.dispose()


def test_the_audit_event_is_written_under_rls_in_both_modes(
    rls_db: RlsDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    set_stdin(monkeypatch, PASSWORD + "\n")
    assert main([*BASE, *REQUIRED, "--db", rls_db.owner_url, "--password-stdin"]) == 0
    ids = json.loads(capsys.readouterr().out)
    (event,) = audit_rows(rls_db.app_url, ids["agency_id"])  # read back as the app connection
    assert event.details["manager_user_id"] == ids["user_id"]


# --- grant-app-role -----------------------------------------------------------------------------


def privileges(url: str, role: str, table: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                p
                for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
                if conn.execute(
                    text("SELECT has_table_privilege(:r, :t, :p)"), {"r": role, "t": table, "p": p}
                ).scalar()
            }
    finally:
        engine.dispose()


def test_grant_app_role_on_sqlite_is_a_usage_error(
    ready_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    if not ready_db.startswith("sqlite"):
        pytest.skip("SQLite only")
    assert main(["grant-app-role", "bruge_app", "--db", ready_db]) == EXIT_USAGE
    assert "PostgreSQL" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["Bad", 'x"; DROP TABLE users; --', "a b", "", "1abc"])
def test_grant_app_role_validates_the_name_without_echoing_it(
    ready_db: str, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    assert main(["grant-app-role", name, "--db", ready_db]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "APP_DB_ROLE" in err
    assert "DROP" not in err


@pytest.fixture
def pg_url(db_url: str) -> str:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    return db_url


@pytest.fixture
def app_role() -> str:
    url = os.environ.get(TEST_APP_DATABASE_URL_ENV)
    if not url:
        pytest.skip("needs TEST_APP_DATABASE_URL")
    return make_url(url).username or ""


def test_grant_app_role_repairs_a_role_created_after_the_migration(
    pg_url: str, app_role: str, capsys: pytest.CaptureFixture[str]
) -> None:
    # Single-role mode: the migration ran without the role, so it holds nothing yet.
    migrate(pg_url, app_role=None)
    assert privileges(pg_url, app_role, "tickets") == set()
    for _ in range(2):  # idempotent
        assert main(["grant-app-role", app_role, "--db", pg_url]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["role"] == app_role
        assert privileges(pg_url, app_role, "tickets") == {"SELECT", "INSERT", "UPDATE"}
    for table, wanted in GRANTS.items():
        assert privileges(pg_url, app_role, table) == set(wanted.replace(" ", "").split(","))


def test_grant_app_role_refuses_a_privileged_role(
    pg_url: str, app_role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(pg_url, app_role=None)
    monkeypatch.setattr(
        db_roles,
        "role_privilege_problem",
        lambda _c, role=None: f"role {role!r} has BYPASSRLS, so row-level security would not apply",
    )
    assert main(["grant-app-role", app_role, "--db", pg_url]) == EXIT_USAGE
    assert "BYPASSRLS" in capsys.readouterr().err
    assert privileges(pg_url, app_role, "tickets") == set()  # nothing was granted


def test_grant_app_role_refuses_a_missing_role(
    pg_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate(pg_url, app_role=None)
    assert main(["grant-app-role", "no_such_role", "--db", pg_url]) == EXIT_USAGE
    assert "does not exist" in capsys.readouterr().err


# --- purge-expired ------------------------------------------------------------------------------


def test_purge_expired_on_sqlite(ready_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    if not ready_db.startswith("sqlite"):
        pytest.skip("SQLite only")
    now = datetime.now(UTC)
    engine = make_engine(ready_db)
    try:
        with make_session_factory(engine)() as s:
            agency = create_agency(s, "a", "A")
            user = User(agency_id=agency.id, phone="+22212345678", role="manager")
            s.add(user)
            s.commit()
            for n, absolute in enumerate((now - timedelta(hours=1), now + timedelta(days=1))):
                s.add(
                    UserSession(
                        agency_id=agency.id,
                        user_id=user.id,
                        token_hash=f"{n:064x}",
                        csrf_token="c" * 43,
                        last_seen_at=now,
                        idle_expires_at=now + timedelta(hours=1),
                        absolute_expires_at=absolute,
                    )
                )
            s.commit()
    finally:
        engine.dispose()
    assert main(["purge-expired", "--db", ready_db]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "user_sessions": 1,
        "temporary_passwords": 0,
        "audit_events": 0,
        "rate_limit_buckets": 0,
        "total": 1,
    }
    assert main(["purge-expired", "--db", ready_db]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 0


def test_purge_expired_runs_with_the_app_connection_in_both_modes(
    rls_db: RlsDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = make_engine(rls_db.owner_url)
    try:
        with make_session_factory(engine)() as s:
            agency = create_agency(s, "a", "A")
            s.add(
                AuditEvent(
                    agency_id=agency.id,
                    event_type="ancient",
                    details={},
                    created_at=datetime.now(UTC) - timedelta(days=500),
                )
            )
            s.commit()
    finally:
        engine.dispose()
    assert main(["purge-expired", "--db", rls_db.app_url]) == 0
    assert json.loads(capsys.readouterr().out)["audit_events"] == 1


def test_purge_expired_without_a_schema_is_a_clean_failure(
    db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    if not db_url.startswith("sqlite"):
        pytest.skip("SQLite only")
    assert main(["purge-expired", "--db", db_url]) == EXIT_FAILURE
    assert "init-db" in capsys.readouterr().err
