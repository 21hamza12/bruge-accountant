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


# --- the pre-check names what collided, and nothing is half-created ------------------------------


def make_other_agency(url: str, *, phone: str | None = None, slug: str = "other") -> None:
    from app.auth.passwords import hash_password
    from app.repository import bootstrap_agency

    with owner_session(url) as s:
        bootstrap_agency(
            s, slug, "Other", phone or "+22290000099", "Other", hash_password(PASSWORD), "test"
        )


def agency_slugs(url: str) -> list[str]:
    from app.models import Agency

    with owner_session(url) as s:
        slugs: list[str] = []
        for slug in (DEMO_A, DEMO_B, "other"):
            if s.scalar(select(func.count()).select_from(Agency).where(Agency.slug == slug)):
                slugs.append(slug)
        return slugs


@pytest.mark.parametrize(
    "phone", [PHONE_MANAGER_A, PHONE_MANAGER_B, PHONE_STAFF_SV, PHONE_STAFF_ER]
)
def test_a_taken_demo_phone_is_named_and_nothing_is_created(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], phone: str
) -> None:
    make_other_agency(migrated, phone=phone)
    capsys.readouterr()
    stdin(monkeypatch)
    assert seed(migrated) == 2
    err = capsys.readouterr().err
    assert f"phone {phone}" in err
    assert "already" in err
    assert "nothing was changed" in err
    assert agency_slugs(migrated) == ["other"]  # no demo agency, no half-seeded state


@pytest.mark.parametrize("slug", [DEMO_A, DEMO_B])
def test_a_taken_demo_slug_is_named_and_nothing_else_is_created(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], slug: str
) -> None:
    make_other_agency(migrated, slug=slug)
    capsys.readouterr()
    stdin(monkeypatch)
    assert seed(migrated) == 2
    err = capsys.readouterr().err
    assert f"slug {slug}" in err
    assert agency_slugs(migrated) == [slug]


def test_several_collisions_are_all_reported(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    make_other_agency(migrated, slug=DEMO_B, phone=PHONE_STAFF_ER)
    capsys.readouterr()
    stdin(monkeypatch)
    assert seed(migrated) == 2
    err = capsys.readouterr().err
    assert f"slug {DEMO_B}" in err
    assert f"phone {PHONE_STAFF_ER}" in err


def test_a_failure_halfway_leaves_nothing_behind(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise OperationalError("INSERT INTO tickets", {}, Exception("disk full"))

    monkeypatch.setattr("app.demo.ingest_report", boom)  # agencies and users are already added
    stdin(monkeypatch)
    assert seed(migrated) == 1
    assert "database error" in capsys.readouterr().err
    assert agency_slugs(migrated) == []
    with owner_session(migrated) as s:
        from app.models import AuditEvent

        assert s.scalar(select(func.count()).select_from(User)) == 0
        assert s.scalar(select(func.count()).select_from(AuditEvent)) == 0
    # and the next, healthy run succeeds from a clean slate
    monkeypatch.undo()
    stdin(monkeypatch)
    assert seed(migrated) == 0


def test_the_whole_seed_is_one_transaction(migrated: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.orm import Session

    commits: list[int] = []
    real_commit = Session.commit

    def counting(self: Session) -> None:
        commits.append(1)
        real_commit(self)

    monkeypatch.setattr(Session, "commit", counting)
    stdin(monkeypatch)
    assert seed(migrated) == 0
    assert len(commits) == 1


# --- where it may run ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///demo.db",
        "sqlite://",
        "postgresql://u:p@localhost/db",
        "postgresql+psycopg://u:p@127.0.0.1:5432/db",
        "postgresql://u:p@[::1]/db",
        "postgresql:///db",  # a unix socket on this machine
        "postgresql://u:p@LOCALHOST/db",
    ],
)
def test_local_databases_are_allowed(url: str) -> None:
    from app.demo import demo_target_refusal

    assert demo_target_refusal(url, allow_remote=False) is None


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.example.com/db",
        "postgresql://u:p@10.0.0.5/db",
        "postgresql+psycopg://u:p@dpg-abc123.frankfurt-postgres.render.com/db",
        "postgresql://u:p@localhost.evil.example/db",
    ],
)
def test_remote_databases_are_refused_unless_confirmed(url: str) -> None:
    from app.demo import demo_target_refusal

    reason = demo_target_refusal(url, allow_remote=False)
    assert reason is not None
    assert "--i-know-this-is-not-production" in reason
    assert "p@" not in reason  # no credentials in the message
    assert demo_target_refusal(url, allow_remote=True) is None


def test_the_cli_refuses_a_remote_database_before_it_connects_or_reads_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_engine(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not connect")

    monkeypatch.setattr("app.cli.make_engine", no_engine)
    stdin(monkeypatch)
    url = "postgresql://demo:secret-pw@db.example.com/prod"
    assert seed(url) == 2
    err = capsys.readouterr().err
    assert "--i-know-this-is-not-production" in err
    assert "secret-pw" not in err


def test_the_flag_allows_a_remote_database_to_be_tried(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    attempted: list[str] = []

    def fake_engine(url: str, *args: Any, **kwargs: Any) -> Any:
        attempted.append("engine")
        raise OperationalError("connect", {}, Exception("could not connect"))

    monkeypatch.setattr("app.cli.make_engine", fake_engine)
    stdin(monkeypatch)
    assert seed("postgresql://demo:pw@db.example.com/prod", "--i-know-this-is-not-production") == 1
    assert attempted == ["engine"]


def test_the_flag_never_overrides_production(
    migrated: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ENV", "production")
    stdin(monkeypatch)
    assert seed(migrated, "--i-know-this-is-not-production") == 2
    assert "production" in capsys.readouterr().err
    monkeypatch.delenv("ENV")
    assert agency_slugs(migrated) == []


def test_the_flag_is_harmless_on_a_local_database(
    migrated: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdin(monkeypatch)
    assert seed(migrated, "--i-know-this-is-not-production") == 0
