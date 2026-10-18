"""Step 27: the upload page and POST /uploads, every test under row-level security."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.web.routes.uploads as upload_routes
from app.models import Ticket, Upload
from app.parsing import sandbox
from app.parsing.amadeus import MAX_FILE_BYTES, ParsedReport, ParseError, parse_report
from app.parsing.sandbox import SandboxBusy
from app.tenancy import rebind_tenant
from tests.builders import build_workbook, ticket_row
from tests.web_support import (
    PASSWORD_A,
    PASSWORD_B,
    PASSWORD_S,
    PHONE_A,
    PHONE_B,
    PHONE_STAFF_A,
    FakeClock,
    Seeded,
    WebDb,
    audit_events,
    csrf_for,
    login,
    make_client,
    owner_session,
    seed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


class Env:
    def __init__(self, web_db: WebDb, *, sandboxed: bool = False, **overrides: object) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock, **overrides)
        if not sandboxed:
            # The sandbox spawns a process (about a second); only a few tests pay for it.
            self.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: parse_report  # type: ignore[attr-defined]

    def as_user(self, phone: str, password: str) -> TestClient:
        c = self.factory()
        assert login(c, phone, password).status_code == 303
        return c

    def manager_a(self) -> TestClient:
        return self.as_user(PHONE_A, PASSWORD_A)

    def manager_b(self) -> TestClient:
        return self.as_user(PHONE_B, PASSWORD_B)

    def staff_a(self) -> TestClient:
        return self.as_user(PHONE_STAFF_A, PASSWORD_S)

    def upload(
        self,
        c: TestClient,
        data: bytes | None = None,
        filename: str = "1OCT.xlsx",
        *,
        fields: dict[str, str] | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        form = {"csrf_token": token or csrf_for(c, "/uploads/new"), **(fields or {})}
        response: httpx.Response = c.post(
            "/uploads",
            data=form,
            files={"file": (filename, fixture_bytes() if data is None else data, XLSX)},
            headers=headers,
            follow_redirects=False,
        )
        return response

    def counts(self, agency_id: int) -> tuple[int, int]:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency_id)
            tickets = s.scalar(
                select(func.count()).select_from(Ticket).where(Ticket.agency_id == agency_id)
            )
            uploads = s.scalar(
                select(func.count()).select_from(Upload).where(Upload.agency_id == agency_id)
            )
        return tickets or 0, uploads or 0

    def uploads(self, agency_id: int) -> list[Upload]:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency_id)
            return list(
                s.scalars(select(Upload).where(Upload.agency_id == agency_id).order_by(Upload.id))
            )


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


# --- the page -----------------------------------------------------------------------------------


def test_anonymous_is_sent_to_login(env: Env) -> None:
    with env.factory() as c:
        response = c.get("/uploads/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.parametrize("who", ["manager_a", "staff_a"])
def test_any_signed_in_user_sees_the_upload_form(env: Env, who: str) -> None:
    with getattr(env, who)() as c:
        response = c.get("/uploads/new")
    assert response.status_code == 200
    text = response.text
    assert 'method="post"' in text
    assert 'action="/uploads"' in text
    assert 'enctype="multipart/form-data"' in text
    assert 'type="file"' in text
    assert ".xlsx" in text
    assert 'name="csrf_token"' in text  # works without JavaScript
    assert "Envoyer un rapport" in text
    assert not re.findall(r"<script(?![^>]*src=)", text)  # no inline script


# --- success ------------------------------------------------------------------------------------


def test_a_manager_uploads_the_fixture_and_is_sent_to_the_result_page(env: Env) -> None:
    with env.manager_a() as c:
        response = env.upload(c, filename="../evil/1OCT.xlsx")
    assert response.status_code == 303
    uploads = env.uploads(env.seeded.agency_a)
    assert len(uploads) == 1
    upload = uploads[0]
    assert response.headers["location"] == f"/uploads/{upload.id}"
    assert (upload.inserted_count, upload.updated_count) == (48, 0)
    assert upload.uploaded_by_user_id == env.seeded.manager_a
    assert upload.filename == "1OCT.xlsx"  # sanitized: no path
    assert env.counts(env.seeded.agency_a) == (48, 1)
    assert env.counts(env.seeded.agency_b) == (0, 0)


def test_staff_can_upload(env: Env) -> None:
    with env.staff_a() as c:
        response = env.upload(c)
    assert response.status_code == 303
    (upload,) = env.uploads(env.seeded.agency_a)
    assert upload.uploaded_by_user_id == env.seeded.staff_a


def test_uploading_the_same_file_again_inserts_nothing_and_updates_48(env: Env) -> None:
    with env.manager_a() as c:
        assert env.upload(c).status_code == 303
        assert env.upload(c).status_code == 303
    first, second = env.uploads(env.seeded.agency_a)
    assert (first.inserted_count, first.updated_count) == (48, 0)
    assert (second.inserted_count, second.updated_count) == (0, 48)
    assert env.counts(env.seeded.agency_a) == (48, 2)  # still 48 tickets


def test_a_forged_agency_field_is_ignored(env: Env) -> None:
    forged = {
        "agency_id": str(env.seeded.agency_b),
        "agency": "agence-b",
        "uploaded_by_user_id": str(env.seeded.manager_b),
        "uploaded_by": str(env.seeded.manager_b),
    }
    with env.manager_a() as c:
        response = env.upload(c, fields=forged)
    assert response.status_code == 303
    assert env.counts(env.seeded.agency_a) == (48, 1)
    assert env.counts(env.seeded.agency_b) == (0, 0)
    (upload,) = env.uploads(env.seeded.agency_a)
    assert upload.uploaded_by_user_id == env.seeded.manager_a


def test_the_default_parser_is_the_sandbox(web_db: WebDb) -> None:
    env = Env(web_db, sandboxed=True)
    with env.manager_a() as c:
        response = env.upload(c)
    assert response.status_code == 303
    assert env.counts(env.seeded.agency_a) == (48, 1)


def test_an_xlsx_with_a_wrong_name_is_stored_under_the_fallback_name(env: Env) -> None:
    with env.manager_a() as c:
        assert env.upload(c, filename="rapport.txt").status_code == 303
    assert env.uploads(env.seeded.agency_a)[0].filename == "upload.xlsx"


def test_success_is_audited_without_cell_contents(env: Env) -> None:
    with env.manager_a() as c:
        env.upload(c, filename="1OCT.xlsx")
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "upload_succeeded")
    assert event.actor_user_id == env.seeded.manager_a
    details = event.details
    assert details["filename"] == "1OCT.xlsx"
    assert (details["inserted"], details["updated"]) == (48, 0)
    assert "upload_id" in details
    assert "DOE" not in str(details)


# --- CSRF ---------------------------------------------------------------------------------------


def test_the_token_can_travel_in_the_header_instead_of_the_field(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        response = c.post(
            "/uploads",
            files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
    assert response.status_code == 303


def test_a_post_without_a_token_is_refused_and_stores_nothing(env: Env) -> None:
    with env.manager_a() as c:
        response = c.post(
            "/uploads",
            files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
        wrong = env.upload(c, token="x" * 40)
    assert response.status_code == 403
    assert wrong.status_code == 403
    assert env.counts(env.seeded.agency_a) == (0, 0)
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "csrf_failed")) == 2


def test_a_foreign_origin_is_refused(env: Env) -> None:
    with env.manager_a() as c:
        response = env.upload(c, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_anonymous_post_is_refused_before_anything_is_read(env: Env) -> None:
    with env.factory() as c:
        response = c.post(
            "/uploads",
            data={"csrf_token": "x" * 40},
            files={"file": ("1OCT.xlsx", fixture_bytes(), XLSX)},
            follow_redirects=False,
        )
    assert response.status_code == 401
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_a_user_who_must_change_the_password_cannot_upload(env: Env) -> None:
    from tests.web_support import add_user

    add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        "+22212345611",
        password=PASSWORD_S,
        must_change=True,
    )
    c = env.factory()
    assert login(c, "+22212345611", PASSWORD_S).status_code == 303
    with c:
        page = c.get("/password/change")
        assert page.status_code == 200
        response = c.get("/uploads/new", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/password/change"


# --- rate limit ---------------------------------------------------------------------------------


def test_the_thirty_first_upload_in_an_hour_is_a_429(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        statuses = [env.upload(c, token=token).status_code for _ in range(31)]
        limited = env.upload(c, token=token)
    assert statuses == [303] * 30 + [429]
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert env.counts(env.seeded.agency_a)[1] == 30
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "rate_limited")) == 1
    # another agency has its own budget
    with env.manager_b() as b:
        assert env.upload(b).status_code == 303


def test_the_limit_is_per_agency_not_per_user(env: Env) -> None:
    with env.manager_a() as m, env.staff_a() as s:
        mt, st = csrf_for(m, "/uploads/new"), csrf_for(s, "/uploads/new")
        codes = [
            env.upload(m if i % 2 else s, token=mt if i % 2 else st).status_code for i in range(31)
        ]
    assert codes[-1] == 429
    assert codes.count(303) == 30


def test_the_window_ends_after_an_hour(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        for _ in range(31):
            env.upload(c, token=token)
        env.clock.advance(hours=1, seconds=1)
        # (the session is still valid: 12 h idle)
        assert env.upload(c, token=csrf_for(c, "/uploads/new")).status_code == 303


# --- errors -------------------------------------------------------------------------------------


def failed_events(env: Env) -> list[dict[str, object]]:
    return [
        dict(e.details)
        for e in audit_events(env.db.owner_url, env.seeded.agency_a, "upload_failed")
    ]


def test_a_file_that_is_not_an_xlsx(env: Env) -> None:
    with env.manager_a() as c:
        response = env.upload(c, b"this is just text", filename="notes.xlsx")
    assert response.status_code == 400
    assert "pas un classeur Excel" in response.text
    assert 'type="file"' in response.text  # the form is shown again
    assert env.counts(env.seeded.agency_a) == (0, 0)
    assert failed_events(env) == [{"filename": "notes.xlsx", "reason": "not_xlsx"}]


def test_an_empty_file(env: Env) -> None:
    with env.manager_a() as c:
        response = env.upload(c, b"")
    assert response.status_code == 400
    assert "Le fichier est vide" in response.text
    assert failed_events(env)[0]["reason"] == "empty"


def test_no_file_at_all(env: Env) -> None:
    with env.manager_a() as c:
        response = c.post(
            "/uploads",
            data={"csrf_token": csrf_for(c, "/uploads/new")},
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert "Choisissez un fichier" in response.text
    assert failed_events(env)[0]["reason"] == "no_file"


def test_a_parse_error_shows_the_safe_message(env: Env) -> None:
    bad = build_workbook([ticket_row()], headers=["SEQ NO", "A/L", "DOC NUMBER"])
    with env.manager_a() as c:
        response = env.upload(c, bad)
    assert response.status_code == 400
    assert "pas pu être lu" in response.text
    assert "missing required column" in response.text
    assert "Traceback" not in response.text
    assert env.counts(env.seeded.agency_a) == (0, 0)
    assert failed_events(env)[0]["reason"] == "parse_error"


def test_the_parse_error_message_is_escaped(env: Env) -> None:
    def hostile(data: bytes) -> ParsedReport:
        raise ParseError("<script>alert(1)</script>")

    env.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: hostile  # type: ignore[attr-defined]
    with env.manager_a() as c:
        response = env.upload(c)
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "alert(1)" not in str(failed_events(env))  # the audit holds the reason only


def test_a_file_just_over_the_limit_is_the_413_page_and_reads_one_byte_more(env: Env) -> None:
    big = b"PK\x03\x04" + b"0" * (MAX_FILE_BYTES - 3)  # MAX_FILE_BYTES + 1 bytes
    assert len(big) == MAX_FILE_BYTES + 1
    with env.manager_a() as c:
        response = env.upload(c, big)
    assert response.status_code == 413
    assert "Fichier trop volumineux" in response.text
    assert env.counts(env.seeded.agency_a) == (0, 0)
    assert failed_events(env)[0]["reason"] == "too_large"


def test_a_file_of_exactly_the_limit_is_read_in_full(env: Env) -> None:
    seen: list[int] = []

    def spy(data: bytes) -> ParsedReport:
        seen.append(len(data))
        raise ParseError("stop here")

    env.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: spy  # type: ignore[attr-defined]
    exact = b"PK\x03\x04" + b"0" * (MAX_FILE_BYTES - 4)
    with env.manager_a() as c:
        assert env.upload(c, exact).status_code == 400
    assert seen == [MAX_FILE_BYTES]


def test_a_busy_sandbox_is_the_503_page_with_retry_after(env: Env) -> None:
    def busy(data: bytes) -> ParsedReport:
        raise SandboxBusy

    env.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: busy  # type: ignore[attr-defined]
    with env.manager_a() as c:
        response = env.upload(c)
    assert response.status_code == 503
    assert "Service occupé" in response.text
    assert int(response.headers["retry-after"]) > 0
    assert env.counts(env.seeded.agency_a) == (0, 0)


def test_a_real_concurrent_parse_makes_the_second_upload_busy(web_db: WebDb) -> None:
    env = Env(web_db, sandboxed=True, parse_concurrency=1, parse_queue_wait_s=0)
    assert sandbox.slots(1).acquire(blocking=False)
    try:
        with env.manager_a() as c:
            response = env.upload(c)
    finally:
        sandbox.slots(1).release()
    assert response.status_code == 503
    assert response.headers["retry-after"]


def test_a_crashing_parser_never_leaks_internals(env: Env) -> None:
    def explode(data: bytes) -> ParsedReport:
        raise RuntimeError("secret /srv/app/internals.py")

    env.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: explode  # type: ignore[attr-defined]
    with env.manager_a() as c:
        response = env.upload(c)
    assert response.status_code == 500
    assert "internals" not in response.text
    assert env.counts(env.seeded.agency_a) == (0, 0)
