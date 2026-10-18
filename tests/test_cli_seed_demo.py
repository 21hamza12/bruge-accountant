"""Step 31: ``python -m app seed-demo``."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cli import main
from app.demo import (
    DEMO_A,
    DEMO_B,
    PHONE_MANAGER_A,
    PHONE_MANAGER_B,
    PHONE_STAFF_ER,
    PHONE_STAFF_SV,
)
from app.models import ROLE_MANAGER, ROLE_STAFF, Agency, StaffSignLink, Ticket, Upload, User
from app.tenancy import lookup_scope, rebind_tenant
from tests.web_support import ORIGIN, FakeClock, WebDb, form_token, make_client, owner_session

PASSWORD = "Demo-pass-2026!"  # noqa: S105 - a throwaway test password
FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"


@pytest.fixture
def migrated(db_url: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["init-db", "--db", db_url]) == 0
    capsys.readouterr()
    return db_url


def stdin(monkeypatch: pytest.MonkeyPatch, text: str = PASSWORD + "\n") -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def seed(url: str, *extra: str, fixture: Path = FIXTURE, stdin_flag: bool = True) -> int:
    argv = ["seed-demo", "--fixture", str(fixture), "--db", url, *extra]
    if stdin_flag:
        argv.append("--password-stdin")
    return main(argv)


def agency(session: Session, slug: str) -> Agency:
    lookup_scope(session, "app.agency_slug", slug)
    return session.scalars(select(Agency).where(Agency.slug == slug)).one()


def test_it_creates_two_agencies_with_their_users_and_links(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch)
    assert seed(migrated) == 0
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert set(out) == {DEMO_A, DEMO_B}
    with owner_session(migrated) as s:
        a, b = agency(s, DEMO_A), agency(s, DEMO_B)
        rebind_tenant(s, a.id)
        users_a = {u.phone: u for u in s.scalars(select(User).where(User.agency_id == a.id))}
        links = {
            link.staff_sign: link.user_id
            for link in s.scalars(select(StaffSignLink).where(StaffSignLink.agency_id == a.id))
        }
        a_tickets = s.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.agency_id == a.id)
        )
        rebind_tenant(s, b.id)
        users_b = list(s.scalars(select(User).where(User.agency_id == b.id)))
        b_tickets = s.scalar(
            select(func.count()).select_from(Ticket).where(Ticket.agency_id == b.id)
        )
        b_uploads = s.scalars(select(Upload).where(Upload.agency_id == b.id)).all()
    assert set(users_a) == {PHONE_MANAGER_A, PHONE_STAFF_SV, PHONE_STAFF_ER}
    assert users_a[PHONE_MANAGER_A].role == ROLE_MANAGER
    assert users_a[PHONE_STAFF_SV].role == ROLE_STAFF
    assert links == {"0004SV": users_a[PHONE_STAFF_SV].id, "0009ER": users_a[PHONE_STAFF_ER].id}
    assert [(u.phone, u.role) for u in users_b] == [(PHONE_MANAGER_B, ROLE_MANAGER)]
    assert all(not u.must_change_password for u in [*users_a.values(), *users_b])
    # demo-a stays empty for the person trying the upload; demo-b holds the fixture, so the two
    # agencies have overlapping ticket numbers and isolation is visible.
    assert (a_tickets, b_tickets) == (0, 48)
    assert len(b_uploads) == 1


def test_every_demo_user_can_log_in_with_the_one_password(
    migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    assert seed(migrated) == 0
    factory = make_client(WebDb("sqlite", migrated, migrated), FakeClock())
    for phone in (PHONE_MANAGER_A, PHONE_MANAGER_B, PHONE_STAFF_SV, PHONE_STAFF_ER):
        with factory() as c:
            page = c.get("/login")
            response = c.post(
                "/login",
                data={"phone": phone, "password": PASSWORD, "csrf_token": form_token(page)},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            )
            assert response.status_code == 303, phone
            assert response.headers["location"] == "/"


def test_the_password_is_never_printed_or_stored_in_clear(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch)
    assert seed(migrated) == 0
    captured = capsys.readouterr()
    assert PASSWORD not in captured.out + captured.err
    with owner_session(migrated) as s:
        for slug in (DEMO_A, DEMO_B):
            rebind_tenant(s, agency(s, slug).id)
            hashes = [u.password_hash for u in s.scalars(select(User))]
            assert all(h and h.startswith("$argon2id$") and PASSWORD not in h for h in hashes)


def test_it_refuses_in_production_and_creates_nothing(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch)
    monkeypatch.setenv("ENV", "production")
    assert seed(migrated) == 2
    assert "production" in capsys.readouterr().err
    monkeypatch.delenv("ENV")
    with owner_session(migrated) as s:
        assert s.scalar(select(func.count()).select_from(Agency)) == 0


def test_it_refuses_a_second_run(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch)
    assert seed(migrated) == 0
    capsys.readouterr()
    stdin(monkeypatch)
    assert seed(migrated) == 2
    assert "already exist" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["", "\n", "short\n", "aaaaaaaaaaaa\n", "password123\n"])
def test_a_bad_password_is_refused_and_nothing_is_created(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], text: str
) -> None:
    stdin(monkeypatch, text)
    assert seed(migrated) == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    with owner_session(migrated) as s:
        assert s.scalar(select(func.count()).select_from(Agency)) == 0


def test_the_password_is_never_an_argument(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stdin(monkeypatch)
    assert seed(migrated, "--password", "hunter2hunter2") == 2
    err = capsys.readouterr().err
    assert "hunter2" not in err
    assert "never accepted as an argument" in err


def test_stdin_is_required(migrated: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        seed(migrated, stdin_flag=False)
    assert exc.value.code == 2


def test_a_bad_fixture_creates_nothing(
    migrated: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a workbook")
    stdin(monkeypatch)
    assert seed(migrated, fixture=bad) == 1
    assert capsys.readouterr().err.startswith("error:")
    missing = tmp_path / "nope.xlsx"
    stdin(monkeypatch)
    assert seed(migrated, fixture=missing) == 1
    with owner_session(migrated) as s:
        assert s.scalar(select(func.count()).select_from(Agency)) == 0


def test_the_production_check_does_not_need_a_valid_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # ENV=production without the production settings would fail Settings(); the refusal comes first.
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["seed-demo", "--fixture", str(FIXTURE), "--password-stdin"]) == 2
    assert "production" in capsys.readouterr().err


def test_the_end_to_end_suite_uses_the_same_demo_phones() -> None:
    from tests.e2e import test_journeys as journeys

    assert journeys.DEMO_PHONES == (
        PHONE_MANAGER_A,
        PHONE_MANAGER_B,
        PHONE_STAFF_SV,
        PHONE_STAFF_ER,
    )
