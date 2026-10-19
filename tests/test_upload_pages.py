"""Step 28: the upload result page, the recent uploads list, and escaping everywhere."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.web.routes.uploads as upload_routes
from app.ingest import ingest_report
from app.parsing.amadeus import parse_report
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
    add_user,
    csrf_for,
    login,
    make_client,
    owner_session,
    seed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SCRIPT = "<script>alert(1)</script>"
IMG = '"><img src=x onerror=alert(1)>'


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.factory = make_client(web_db, FakeClock())
        self.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: parse_report  # type: ignore[attr-defined]

    def as_user(self, phone: str, password: str) -> TestClient:
        c = self.factory()
        assert login(c, phone, password).status_code == 303
        return c

    def upload(
        self, c: TestClient, data: bytes | None = None, name: str = "1OCT.xlsx"
    ) -> httpx.Response:
        response: httpx.Response = c.post(
            "/uploads",
            data={"csrf_token": csrf_for(c, "/uploads/new")},
            files={"file": (name, FIXTURE.read_bytes() if data is None else data, XLSX)},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        return response

    def ingest_many(self, agency_id: int, count: int) -> list[int]:
        ids = []
        for i in range(count):
            data = build_workbook([ticket_row(doc=f"39400{i:05d}")])
            with owner_session(self.db.owner_url) as s:
                ids.append(ingest_report(s, agency_id, data, f"n{i}.xlsx").upload_id)
        return ids


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def test_the_result_page_shows_the_numbers_the_uploader_and_a_link_to_the_questions(
    env: Env,
) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as c:
        location = env.upload(c).headers["location"]
        page = c.get(location)
    assert page.status_code == 200
    text = page.text
    assert "1OCT.xlsx" in text
    assert "1 octobre 2026" in text
    assert re.search(r"Ajoutés\s*:\s*<strong>48</strong>", text)
    assert re.search(r"Mis à jour\s*:\s*<strong>0</strong>", text)
    assert "Manager A" in text
    assert 'href="/questions?date=2026-10-01"' in text
    assert page.headers["cache-control"] == "no-store"


def test_a_reupload_shows_zero_inserted_and_48_updated(env: Env) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as c:
        env.upload(c)
        page = c.get(env.upload(c).headers["location"])
    assert re.search(r"Ajoutés\s*:\s*<strong>0</strong>", page.text)
    assert re.search(r"Mis à jour\s*:\s*<strong>48</strong>", page.text)


# --- staff see only their own uploads, and never the warnings -----------------------------------

PHONE_STAFF_2 = "+22212345611"
OTHER_PASSENGER = "ZEBRA/QUENTIN"


def _leaky_workbook() -> bytes:
    """A report whose warnings quote another sign's passenger (the reviewer's repro)."""
    rows = [
        ticket_row(doc="3940000011", name=f"{OTHER_PASSENGER} (INFX)", staff="0009ER"),
        ticket_row(doc="BAD-LEAK-77", name="DOE/JANE", staff="0009ER"),
        ticket_row(doc="3940000013", name="DOE/JOHN", staff="0004SV"),
    ]
    return build_workbook(rows)


def test_staff_cannot_open_a_managers_upload_or_see_its_warnings(env: Env) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as m:
        location = env.upload(m, _leaky_workbook()).headers["location"]
        manager_page = m.get(location)
    assert OTHER_PASSENGER in manager_page.text  # the manager sees the warning
    assert "BAD-LEAK-77" in manager_page.text
    with env.as_user(PHONE_STAFF_A, PASSWORD_S) as s:
        page = s.get(location)
        listing = s.get("/uploads")
    assert page.status_code == 404
    assert OTHER_PASSENGER not in page.text + listing.text
    assert "BAD-LEAK-77" not in page.text + listing.text
    assert location not in listing.text
    assert "Aucun rapport" in listing.text


def test_a_colleagues_upload_is_a_404_identical_to_a_missing_one(env: Env) -> None:
    add_user(env.db.owner_url, env.seeded.agency_a, PHONE_STAFF_2, password=PASSWORD_S)
    with env.as_user(PHONE_STAFF_2, PASSWORD_S) as other:
        location = env.upload(other, _leaky_workbook()).headers["location"]
    with env.as_user(PHONE_STAFF_A, PASSWORD_S) as s:
        colleague = s.get(location)
        missing = s.get("/uploads/987654")
        listing = s.get("/uploads")

    def shape(r: httpx.Response) -> str:
        return re.sub(r"<code>[0-9A-F]{8}</code>|data-[a-z-]+='[^']*'", "", r.text)

    assert colleague.status_code == missing.status_code == 404
    assert shape(colleague) == shape(missing)
    assert location not in listing.text


def test_staff_see_their_own_upload_with_counts_and_date_but_no_warnings(env: Env) -> None:
    with env.as_user(PHONE_STAFF_A, PASSWORD_S) as s:
        location = env.upload(s, _leaky_workbook()).headers["location"]
        page = s.get(location)
        listing = s.get("/uploads")
    assert page.status_code == 200
    text = page.text
    assert "1 octobre 2026" in text
    assert re.search(r"Ajoutés\s*:\s*<strong>2</strong>", text)
    assert "Staff A" in text
    assert 'href="/questions?date=2026-10-01"' in text
    assert OTHER_PASSENGER not in text
    assert "BAD-LEAK-77" not in text
    assert "Avertissements" not in text
    assert '<li class="warning">' not in text
    assert location in listing.text
    assert OTHER_PASSENGER not in listing.text


def test_the_list_holds_only_the_staff_members_own_uploads(env: Env) -> None:
    add_user(env.db.owner_url, env.seeded.agency_a, PHONE_STAFF_2, password=PASSWORD_S)
    with env.as_user(PHONE_A, PASSWORD_A) as m:
        mine_m = env.upload(m).headers["location"]
    with env.as_user(PHONE_STAFF_2, PASSWORD_S) as other:
        mine_2 = env.upload(other, _leaky_workbook()).headers["location"]
    with env.as_user(PHONE_STAFF_A, PASSWORD_S) as s:
        mine_s = env.upload(s, build_workbook([ticket_row(doc="3940000099")])).headers["location"]
        listing = s.get("/uploads").text
    assert mine_s in listing
    assert mine_m not in listing
    assert mine_2 not in listing


def test_the_manager_sees_every_upload_and_every_warning(env: Env) -> None:
    add_user(env.db.owner_url, env.seeded.agency_a, PHONE_STAFF_2, password=PASSWORD_S)
    with env.as_user(PHONE_STAFF_2, PASSWORD_S) as other:
        theirs = env.upload(other, _leaky_workbook()).headers["location"]
    with env.as_user(PHONE_STAFF_A, PASSWORD_S) as s:
        also = env.upload(s).headers["location"]
    with env.as_user(PHONE_A, PASSWORD_A) as m:
        listing = m.get("/uploads").text
        page = m.get(theirs)
    assert theirs in listing
    assert also in listing
    assert page.status_code == 200
    assert OTHER_PASSENGER in page.text
    assert "Avertissements" in page.text


def test_imported_uploads_without_an_uploader_are_the_managers_only(env: Env) -> None:
    with owner_session(env.db.owner_url) as s:
        upload_id = ingest_report(
            s, env.seeded.agency_a, FIXTURE.read_bytes(), "cli.xlsx"
        ).upload_id
    with env.as_user(PHONE_STAFF_A, PASSWORD_S) as staff:
        assert staff.get(f"/uploads/{upload_id}").status_code == 404
        assert f"/uploads/{upload_id}" not in staff.get("/uploads").text
    with env.as_user(PHONE_A, PASSWORD_A) as m:
        assert m.get(f"/uploads/{upload_id}").status_code == 200


# --- a re-upload under another date --------------------------------------------------------------


def test_the_result_page_shows_the_days_the_tickets_belong_to_and_warns_on_a_mismatch(
    env: Env,
) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as m:
        first = env.upload(m).headers["location"]
        with owner_session(env.db.owner_url) as s:
            second_id = ingest_report(
                s,
                env.seeded.agency_a,
                FIXTURE.read_bytes(),
                "again.xlsx",
                report_date_override=date(2026, 10, 5),
                uploaded_by=env.seeded.manager_a,
            ).upload_id
        page = m.get(f"/uploads/{second_id}")
        plain = m.get(first)
    text = page.text
    assert "5 octobre 2026" in text  # the upload's own date
    assert 'href="/questions?date=2026-10-01"' in text  # where the tickets really are
    assert re.search(r"1 octobre 2026</a> : 48 billets", text)
    assert "gardent leur jour d'origine" in text
    assert "gardent leur jour d'origine" not in plain.text  # no warning when the days agree


def test_another_agencys_upload_is_a_404_not_a_403(env: Env) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as a:
        location = env.upload(a).headers["location"]
    with env.as_user(PHONE_B, PASSWORD_B) as b:
        assert b.get(location).status_code == 404
        assert b.get("/uploads/999999").status_code == 404  # same answer as "does not exist"
        assert b.get("/uploads/99999999999999999999").status_code == 404  # outside BIGINT
        assert b.get("/uploads/abc").status_code == 400
        assert location not in b.get("/uploads").text


def test_the_404_for_a_foreign_upload_is_identical_to_a_missing_one(env: Env) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as a:
        location = env.upload(a).headers["location"]
    with env.as_user(PHONE_B, PASSWORD_B) as b:
        foreign = b.get(location)
        missing = b.get("/uploads/987654")

    def shape(r: httpx.Response) -> str:
        return re.sub(r"<code>[0-9A-F]{8}</code>|data-[a-z-]+='[^']*'", "", r.text)

    assert foreign.status_code == missing.status_code == 404
    assert shape(foreign) == shape(missing)


def test_anonymous_users_are_sent_to_login(env: Env) -> None:
    with env.factory() as c:
        for path in ("/uploads", "/uploads/1"):
            response = c.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"


def test_the_list_shows_the_newest_fifty_first(env: Env) -> None:
    ids = env.ingest_many(env.seeded.agency_a, 52)
    other = env.ingest_many(env.seeded.agency_b, 1)
    with env.as_user(PHONE_A, PASSWORD_A) as c:
        page = c.get("/uploads")
    assert page.status_code == 200
    shown = [int(i) for i in re.findall(r'href="/uploads/(\d+)"', page.text)]
    assert shown == sorted(ids, reverse=True)[:50]
    assert ids[0] not in shown
    assert ids[1] not in shown
    assert other[0] not in shown


def test_an_empty_list_has_a_friendly_message(env: Env) -> None:
    with env.as_user(PHONE_A, PASSWORD_A) as c:
        page = c.get("/uploads")
    assert page.status_code == 200
    assert "Aucun rapport" in page.text


def test_warnings_are_listed_and_capped(env: Env) -> None:
    rows = [ticket_row(doc=f"BAD{i}") for i in range(70)] + [ticket_row()]
    with env.as_user(PHONE_A, PASSWORD_A) as c:
        page = c.get(env.upload(c, build_workbook(rows)).headers["location"])
    assert page.text.count('<li class="warning">') == upload_routes.MAX_WARNINGS_SHOWN
    assert "20 autres avertissements" in page.text


# --- escaping -----------------------------------------------------------------------------------


def _hostile_workbook() -> bytes:
    rows = [
        ticket_row(doc="3940000001", name=f"{SCRIPT}(INFX)", rloc="AAAAAA"),  # pax marker warning
        ticket_row(doc="3940000002", name=f"{IMG}(INFX)", rloc="AAAAAA"),
        ticket_row(doc=SCRIPT, name="DOE/JOHN"),  # DOC NUMBER warning, row skipped
        ticket_row(doc=IMG, name="DOE/JANE"),
    ]
    return build_workbook(rows)


def _assert_escaped(text: str) -> None:
    assert SCRIPT not in text
    assert "<img" not in text
    assert not re.findall(r"<script(?![^>]*src=)", text)


def test_hostile_content_is_escaped_on_the_result_page_and_the_list(env: Env) -> None:
    name = f"{IMG}.xlsx"
    with env.as_user(PHONE_A, PASSWORD_A) as c:
        location = env.upload(c, _hostile_workbook(), name=name).headers["location"]
        show = c.get(location)
        listing = c.get("/uploads")
    for page in (show, listing):
        assert page.status_code == 200
        _assert_escaped(page.text)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in show.text  # shown, as text
    assert "&lt;img src=x" in listing.text
