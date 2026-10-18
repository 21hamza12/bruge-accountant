"""CLI ``reset-password --phone``: the owner's way back in for a locked-out manager.

A manager cannot reset another manager from the web (decision: staff only); the platform owner
runs this command instead. Cross-engine tests run on SQLite and Postgres (as the owner); the RLS
tests run on Postgres in both deployment modes (``rls_db``).
"""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app import cli
from app.auth import sessions
from app.auth.passwords import hash_password, verify_password
from app.cli import main
from app.models import AuditEvent, TemporaryPassword, User, UserSession
from app.tenancy import bind_tenant, lookup_scope, rebind_tenant
from tests.conftest import RlsDatabase
from tests.test_bootstrap_agency import open_session
from tests.web_support import FakeClock, WebDb, login, make_client

PHONE = "+22212345678"
PHONE_TYPED = "12 34 56 78"
PASSWORD = "correct horse battery"  # noqa: S105
STAFF_PHONE = "+22287654321"


@pytest.fixture
def ready_db(db_url: str) -> str:
    assert main(["init-db", "--db", db_url]) == 0
    return db_url


def _bootstrap(db: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    code = main(
        [
            "bootstrap-agency",
            "bruge-voyages",
            "Bruge Voyages",
            "--manager-phone",
            PHONE,
            "--manager-name",
            "Aminetou",
            "--db",
            db,
            "--password-stdin",
        ]
    )
    assert code == 0
    return {}


class World:
    """An agency with a manager (and a staff member), and the means to look inside."""

    def __init__(self, owner_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        _bootstrap(owner_url, monkeypatch)
        self.url = owner_url
        with open_session(owner_url) as s:
            lookup_scope(s, "app.login_phone", PHONE)
            manager = s.scalars(select(User).where(User.phone == PHONE)).one()
            self.agency_id, self.manager_id = manager.agency_id, manager.id
            rebind_tenant(s, self.agency_id)
            staff = User(
                agency_id=self.agency_id,
                phone=STAFF_PHONE,
                role="staff",
                display_name="Employe",
                password_hash=hash_password("Staff-pass-2026!"),
                must_change_password=False,
            )
            s.add(staff)
            s.commit()
            self.staff_id = staff.id

    def add_session(self, user_id: int) -> None:
        with open_session(self.url) as s:
            bind_tenant(s, self.agency_id)
            user = s.get(User, user_id)
            assert user is not None
            when = datetime.now(UTC)
            policy = sessions.SessionPolicy(timedelta(hours=12), timedelta(days=7))
            sessions.create_session(s, user, when, policy)
            s.commit()

    def user(self, user_id: int) -> User:
        with open_session(self.url) as s:
            bind_tenant(s, self.agency_id)
            return s.scalars(select(User).where(User.id == user_id)).one()

    def temp_rows(self) -> list[TemporaryPassword]:
        with open_session(self.url) as s:
            bind_tenant(s, self.agency_id)
            return list(s.scalars(select(TemporaryPassword).order_by(TemporaryPassword.id)))

    def live_sessions(self, user_id: int) -> int:
        with open_session(self.url) as s:
            bind_tenant(s, self.agency_id)
            rows = s.scalars(select(UserSession).where(UserSession.user_id == user_id))
            return sum(1 for r in rows if r.revoked_at is None)

    def events(self, event_type: str) -> list[AuditEvent]:
        with open_session(self.url) as s:
            bind_tenant(s, self.agency_id)
            return list(s.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)))


def reset(db: str, phone: str = PHONE_TYPED) -> int:
    return main(["reset-password", "--phone", phone, "--db", db])


# --- the happy path -----------------------------------------------------------------------------


def test_resets_a_manager_and_prints_the_temporary_password_once(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(ready_db, monkeypatch)
    world.add_session(world.manager_id)
    assert world.live_sessions(world.manager_id) == 1
    capsys.readouterr()

    assert reset(ready_db) == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    plaintext = payload["temporary_password"]
    assert len(plaintext) == 12
    assert out.out.count(plaintext) == 1  # printed exactly once
    assert plaintext not in out.err
    assert payload["user_id"] == world.manager_id
    assert payload["role"] == "manager"
    assert payload["valid_minutes"] == 30
    assert "2221234" not in out.out  # the phone is masked in the output

    user = world.user(world.manager_id)
    assert user.password_hash is None  # the old password stopped working
    assert user.must_change_password is True
    (row,) = world.temp_rows()
    assert verify_password(plaintext, row.password_hash)
    assert row.used_at is None
    assert row.expires_at - row.created_at == timedelta(minutes=30)
    assert world.live_sessions(world.manager_id) == 0  # sessions revoked


def test_the_reset_is_audited_without_the_password(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(ready_db, monkeypatch)
    capsys.readouterr()
    assert reset(ready_db) == 0
    plaintext = json.loads(capsys.readouterr().out)["temporary_password"]
    (event,) = world.events("temp_password_issued")
    assert event.actor_user_id is None  # nobody was logged in: it was the platform owner
    assert event.agency_id == world.agency_id
    assert event.details["user_id"] == world.manager_id
    assert event.details["via"] == "cli"
    assert event.details["operator"]
    assert plaintext not in str(event.details)


def test_the_password_is_never_logged_or_stored_in_clear(
    ready_db: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World(ready_db, monkeypatch)
    capsys.readouterr()
    with caplog.at_level(logging.DEBUG):
        assert reset(ready_db) == 0
    plaintext = json.loads(capsys.readouterr().out)["temporary_password"]
    assert plaintext not in caplog.text
    assert plaintext not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    stored = [str(getattr(world.user(world.manager_id), c.key)) for c in User.__table__.columns] + [
        str(getattr(world.temp_rows()[0], c.key)) for c in TemporaryPassword.__table__.columns
    ]
    assert all(plaintext not in value for value in stored)


def test_it_also_resets_staff_and_a_second_reset_replaces_the_first(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(ready_db, monkeypatch)
    world.add_session(world.staff_id)
    capsys.readouterr()
    assert reset(ready_db, STAFF_PHONE) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["role"] == "staff"
    assert world.live_sessions(world.staff_id) == 0
    assert reset(ready_db, STAFF_PHONE) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["temporary_password"] != second["temporary_password"]
    first_row, second_row = world.temp_rows()
    assert first_row.used_at is not None  # retired
    assert second_row.used_at is None


# --- refusals -----------------------------------------------------------------------------------


def test_an_unknown_phone_is_refused_and_creates_nothing(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(ready_db, monkeypatch)
    capsys.readouterr()
    assert reset(ready_db, "+22299999999") == 2
    captured = capsys.readouterr()
    assert "no user" in captured.err
    assert captured.out == ""
    assert world.temp_rows() == []
    assert world.user(world.manager_id).password_hash is not None


def test_an_invalid_phone_is_refused(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    World(ready_db, monkeypatch)
    capsys.readouterr()
    assert reset(ready_db, "not a phone") == 2
    assert capsys.readouterr().out == ""


def test_a_deactivated_account_is_refused(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(ready_db, monkeypatch)
    with open_session(ready_db) as s:
        bind_tenant(s, world.agency_id)
        s.scalars(select(User).where(User.id == world.staff_id)).one().is_active = False
        s.commit()
    capsys.readouterr()
    assert reset(ready_db, STAFF_PHONE) == 2
    captured = capsys.readouterr()
    assert "deactivated" in captured.err
    assert captured.out == ""
    assert world.temp_rows() == []


def test_the_phone_option_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["reset-password"])
    assert exc.value.code == 2
    assert "invalid command line" in capsys.readouterr().err


def test_there_is_no_password_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["reset-password", "--help"])
    help_text = capsys.readouterr().out
    assert "--phone" in help_text
    assert "password" in help_text.lower()  # in the description
    assert "--password" not in help_text


def test_the_command_uses_the_default_database_url_like_bootstrap() -> None:
    assert "reset-password" not in cli.OWNER_COMMANDS  # run it with DATABASE_URL=$MIGRATION_...


# --- the temporary password really works through the web app ------------------------------------


def test_the_manager_can_log_in_with_it_and_is_forced_to_change_it(
    ready_db: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    World(ready_db, monkeypatch)
    capsys.readouterr()
    assert reset(ready_db) == 0
    plaintext = json.loads(capsys.readouterr().out)["temporary_password"]
    check_login(
        WebDb("sqlite" if ready_db.startswith("sqlite") else "default", ready_db, ready_db),
        plaintext,
    )


def check_login(db: WebDb, plaintext: str) -> None:
    factory = make_client(db, FakeClock(datetime.now(UTC)))
    with factory() as client:
        response = login(client, PHONE, plaintext)
        assert response.status_code == 303
        home: Any = client.get("/", follow_redirects=False)
        assert home.status_code == 303
        assert home.headers["location"] == "/password/change"
        assert login(factory(), PHONE, PASSWORD).status_code == 401  # the old password is gone


# --- Postgres, under row-level security, in both deployment modes -------------------------------


def test_reset_under_rls_in_both_modes(
    rls_db: RlsDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(rls_db.owner_url, monkeypatch)
    world.add_session(world.manager_id)
    capsys.readouterr()
    assert reset(rls_db.owner_url) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["user_id"] == world.manager_id
    assert world.live_sessions(world.manager_id) == 0
    (event,) = world.events("temp_password_issued")
    assert event.actor_user_id is None and event.details["via"] == "cli"
    # the web application, connecting as itself, accepts the new temporary password
    check_login(WebDb(rls_db.mode, rls_db.owner_url, rls_db.app_url), payload["temporary_password"])


def test_reset_under_rls_for_an_unknown_or_foreign_phone_changes_nothing(
    rls_db: RlsDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(rls_db.owner_url, monkeypatch)
    capsys.readouterr()
    assert reset(rls_db.owner_url, "+22299999999") == 2
    assert world.temp_rows() == []
    assert world.events("temp_password_issued") == []
