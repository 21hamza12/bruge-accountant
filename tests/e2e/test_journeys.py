"""HTTP end-to-end journeys against a live server (``E2E_BASE_URL``); skipped when it is unset.

Black box: only httpx talks to the server. The server must have been seeded with
``python -m app seed-demo`` and the same password in ``E2E_PASSWORD``. The demo phone numbers below
are the constants of ``app/demo.py`` (a unit test keeps the two in step).

The server may run in production mode behind TLS or, as in CI and for local runs, over plain http
with ``COOKIE_SECURE=false``. Run:

    E2E_BASE_URL=http://127.0.0.1:8765 E2E_PASSWORD=... pytest tests/e2e --no-cov -rs

Every journey is safe to repeat against the same database: it uses fresh random phone numbers where
a login rate limit would otherwise remember an earlier run, and the uploads are made by a FRESH
staff member created at the start of each run, so the per-user upload budget (10 an hour) never
carries over. A run makes 3 uploads, so the agency budget (30 an hour, shared by everyone in
demo-a) allows about 10 runs an hour against one seeded database; when it is spent the suite says
so (re-seed a fresh database, or wait for the hour to end).
"""

from __future__ import annotations

import os
import re
import secrets
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx2 as httpx
import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "").rstrip("/")
PASSWORD = os.environ.get("E2E_PASSWORD", "")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="E2E_BASE_URL is not set")

PHONE_MANAGER_A = "+22290000001"
PHONE_MANAGER_B = "+22290000002"
PHONE_STAFF_SV = "+22290000003"
PHONE_STAFF_ER = "+22290000004"
DEMO_PHONES = (PHONE_MANAGER_A, PHONE_MANAGER_B, PHONE_STAFF_SV, PHONE_STAFF_ER)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "1OCT.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
REPORT_DAY = "2026-10-01"
MAX_RAW_ANSWER = 256 * 1024  # the 413 page is a few KB; never read more than this

_HIDDEN = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_BODY = re.compile(r'data-csrf-token="([^"]+)"')
_SECRET = re.compile(r'class="secret"><code>([A-Za-z0-9]+)</code>')
_ADDED = re.compile(r"Ajoutés\s*:\s*<strong>(\d+)</strong>")
_UPDATED = re.compile(r"Mis à jour\s*:\s*<strong>(\d+)</strong>")
_AGENT_CODES = re.compile(r'class="agent-code">([^<]*)<')


class Browser:
    """A cookie-keeping client that never follows redirects and sends its own Origin."""

    def __init__(self) -> None:
        origin = f"{urlsplit(BASE_URL).scheme}://{urlsplit(BASE_URL).netloc}"
        self.origin = origin
        self.http = httpx.Client(
            base_url=BASE_URL, headers={"Origin": origin}, follow_redirects=False, timeout=60
        )

    def close(self) -> None:
        self.http.close()

    def token(self, path: str = "/") -> str:
        page = self.http.get(path)
        assert page.status_code == 200, (path, page.status_code)
        match = _BODY.search(page.text)
        assert match, f"no CSRF token on {path}"
        return match.group(1)

    def login(self, phone: str, password: str) -> httpx.Response:
        page = self.http.get("/login")
        assert page.status_code == 200
        match = _HIDDEN.search(page.text)
        assert match
        return self.http.post(
            "/login", data={"phone": phone, "password": password, "csrf_token": match.group(1)}
        )

    def upload(self, data: bytes, name: str = "1OCT.xlsx") -> httpx.Response:
        return self.http.post(
            "/uploads",
            data={"csrf_token": self.token("/uploads/new")},
            files={"file": (name, data, XLSX)},
        )


def signed_in(phone: str, password: str = PASSWORD) -> Browser:
    browser = Browser()
    response = browser.login(phone, password)
    assert response.status_code == 303, f"login failed for {phone[-2:]}: {response.status_code}"
    return browser


@pytest.fixture(scope="module")
def manager_a() -> Iterator[Browser]:
    assert PASSWORD, "E2E_PASSWORD must hold the password given to seed-demo"
    browser = signed_in(PHONE_MANAGER_A)
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def manager_b() -> Iterator[Browser]:
    browser = signed_in(PHONE_MANAGER_B)
    yield browser
    browser.close()


@dataclass
class Uploaded:
    first_id: int
    second_id: int
    first: tuple[int, int]
    second: tuple[int, int]


def counts(browser: Browser, location: str) -> tuple[int, int]:
    page = browser.http.get(location)
    assert page.status_code == 200
    added, updated = _ADDED.search(page.text), _UPDATED.search(page.text)
    assert added and updated
    return int(added.group(1)), int(updated.group(1))


@pytest.fixture(scope="module")
def uploader(manager_a: Browser) -> Iterator[Browser]:
    """A fresh staff member of demo-a, so this run has its own per-user upload budget."""
    phone, _name, temporary = create_staff(manager_a)
    browser = Browser()
    assert browser.login(phone, temporary).status_code == 303
    change_password(browser, temporary)
    yield browser
    browser.close()


def upload_or_explain(browser: Browser, data: bytes, name: str = "1OCT.xlsx") -> httpx.Response:
    response = browser.upload(data, name)
    if response.status_code == 429:
        pytest.fail(
            "the upload budget of demo-a is spent (30 an hour for the agency): wait for the "
            "hour to end or seed a fresh database"
        )
    return response


@pytest.fixture(scope="module")
def uploaded(uploader: Browser) -> Uploaded:
    data = FIXTURE.read_bytes()
    first, second = upload_or_explain(uploader, data), upload_or_explain(uploader, data)
    assert first.status_code == 303, first.text[:300]
    assert second.status_code == 303
    loc1, loc2 = first.headers["location"], second.headers["location"]
    return Uploaded(
        first_id=int(loc1.rsplit("/", 1)[1]),
        second_id=int(loc2.rsplit("/", 1)[1]),
        first=counts(uploader, loc1),
        second=counts(uploader, loc2),
    )


# --- 1. login -----------------------------------------------------------------------------------


def test_1_login_home_and_logout() -> None:
    b = signed_in(PHONE_MANAGER_A)
    try:
        home = b.http.get("/")
        assert home.status_code == 200
        assert "Tableau de bord" in home.text  # the manager dashboard
        assert '<div class="sidebar">' in home.text
        out = b.http.post("/logout", data={"csrf_token": b.token("/")})
        assert out.status_code == 303
        assert out.headers["location"] == "/login"
        assert b.http.get("/", follow_redirects=False).status_code == 303  # session is gone
    finally:
        b.close()


def test_1_wrong_password_is_the_generic_401() -> None:
    b = Browser()
    try:
        response = b.login(PHONE_MANAGER_A, "definitely-not-the-password")
        assert response.status_code == 401
        assert "incorrect" in response.text
    finally:
        b.close()


# --- 2. create staff, temporary password, forced change -----------------------------------------


def create_staff(manager: Browser) -> tuple[str, str, str]:
    """A new staff member made by ``manager``: (phone, display name, the temporary password)."""
    phone = "+2223" + "".join(secrets.choice("0123456789") for _ in range(7))
    name = "E2E " + secrets.token_hex(3)
    created = manager.http.post(
        "/staff",
        data={
            "csrf_token": manager.token("/staff"),
            "display_name": name,
            "phone": phone,
            "role": "staff",
        },
    )
    assert created.status_code == 200, created.text[:300]
    assert created.headers["cache-control"] == "no-store"
    match = _SECRET.search(created.text)
    assert match, "the temporary password was not shown"
    return phone, name, match.group(1)


def change_password(staff: Browser, temporary: str) -> None:
    new_password = "E2e-new-" + secrets.token_hex(6)
    changed = staff.http.post(
        "/password/change",
        data={
            "csrf_token": staff.token("/password/change"),
            "current_password": temporary,
            "new_password": new_password,
            "confirm_password": new_password,
        },
    )
    assert changed.status_code == 303, changed.text[:300]


def test_2_create_staff_temp_password_and_forced_change(manager_a: Browser) -> None:
    phone, _name, temporary = create_staff(manager_a)
    assert temporary not in manager_a.http.get("/staff").text  # shown once, nowhere else

    staff = Browser()
    try:
        assert staff.login(phone, temporary).status_code == 303
        gate = staff.http.get("/")
        assert gate.status_code == 303
        assert gate.headers["location"] == "/password/change"
        change_password(staff, temporary)
        assert staff.http.get("/").status_code == 200
        assert staff.http.get("/staff").status_code == 403  # staff never reach manager pages
        # the temporary password is spent
        again = Browser()
        try:
            assert again.login(phone, temporary).status_code == 401
        finally:
            again.close()
    finally:
        staff.close()


# --- 3. upload and re-upload --------------------------------------------------------------------


def test_3_upload_then_reupload_counts(uploaded: Uploaded) -> None:
    assert uploaded.first in {(48, 0), (0, 48)}  # (0, 48) when the database was used before
    assert uploaded.second == (0, 48)
    assert uploaded.second_id > uploaded.first_id


def test_3_the_result_page_and_the_list(manager_a: Browser, uploaded: Uploaded) -> None:
    page = manager_a.http.get(f"/uploads/{uploaded.second_id}")
    assert "1OCT.xlsx" in page.text
    assert REPORT_DAY in page.text
    assert f'href="/days/{REPORT_DAY}"' in page.text
    assert f'href="/uploads/{uploaded.second_id}"' in manager_a.http.get("/uploads").text


def test_3_staff_see_only_their_own_uploads(uploader: Browser, uploaded: Uploaded) -> None:
    """A colleague's upload is a 404 for staff, and the manager's pages show everything."""
    other = signed_in(PHONE_STAFF_ER)
    try:
        assert other.http.get(f"/uploads/{uploaded.first_id}").status_code == 404
        assert f"/uploads/{uploaded.first_id}" not in other.http.get("/uploads").text
    finally:
        other.close()
    own = uploader.http.get(f"/uploads/{uploaded.first_id}")
    assert own.status_code == 200
    assert "Avertissements" not in own.text  # warnings are for managers
    assert f'href="/uploads/{uploaded.first_id}"' in uploader.http.get("/uploads").text


def test_3_a_file_that_is_not_a_workbook_is_a_friendly_400(uploader: Browser) -> None:
    response = upload_or_explain(uploader, b"just some text", name="notes.xlsx")
    assert response.status_code == 400
    assert "pas un classeur Excel" in response.text
    assert "Traceback" not in response.text


# --- 4. the home page and the removed questions page -------------------------------------------


def test_4_manager_sees_every_sign_and_staff_see_their_own(
    manager_a: Browser, uploaded: Uploaded
) -> None:
    page = manager_a.http.get(f"/?date={REPORT_DAY}")
    assert page.status_code == 200
    assert _AGENT_CODES.findall(page.text) == ["0004SV", "0009ER"]

    er = signed_in(PHONE_STAFF_ER)
    try:
        own = er.http.get(f"/?date={REPORT_DAY}")
        assert own.status_code == 200
        assert "14 billets dans le rapport" in own.text
        assert "0004SV" not in own.text
    finally:
        er.close()


def test_4_the_price_questions_page_is_gone(manager_a: Browser) -> None:
    assert manager_a.http.get(f"/questions?date={REPORT_DAY}").status_code == 404


# --- 5. another agency -------------------------------------------------------------------------


def staff_id_of(browser: Browser, phone_digits: str) -> int:
    """The id of a user in the manager's staff list, found by the phone shown in its row."""
    page = browser.http.get("/staff")
    row = re.search(
        rf'href="/staff/(\d+)">[^<]*</a></td>\s*<td[^>]*>{re.escape(phone_digits)}</td>', page.text
    )
    assert row, "the user is not in the staff list"
    return int(row.group(1))


def test_5_manager_b_cannot_reach_agency_a(
    manager_a: Browser, manager_b: Browser, uploaded: Uploaded
) -> None:
    a_user = staff_id_of(manager_a, PHONE_STAFF_ER)
    assert manager_b.http.get(f"/uploads/{uploaded.first_id}").status_code == 404
    assert manager_b.http.get(f"/uploads/{uploaded.second_id}").status_code == 404
    assert manager_b.http.get(f"/staff/{a_user}").status_code == 404
    token = manager_b.token("/staff")  # B's own, valid token
    for path, data in (
        (f"/staff/{a_user}/signs", {"sign": "ZZ99"}),
        (f"/staff/{a_user}/deactivate", {}),
        (f"/staff/{a_user}/temp-password", {"expected_temp_id": "None"}),
        (f"/staff/{a_user}/role", {"role": "manager"}),
    ):
        response = manager_b.http.post(path, data={"csrf_token": token, **data})
        assert response.status_code == 404, path
    assert f"/uploads/{uploaded.first_id}" not in manager_b.http.get("/uploads").text


def test_5_the_foreign_404_looks_like_a_missing_one(manager_b: Browser, uploaded: Uploaded) -> None:
    def shape(r: httpx.Response) -> str:
        return re.sub(r"<code>[0-9A-F]{8}</code>|data-[a-z-]+='[^']*'", "", r.text)

    foreign = manager_b.http.get(f"/uploads/{uploaded.first_id}")
    missing = manager_b.http.get("/uploads/999999999")
    assert foreign.status_code == missing.status_code == 404
    assert shape(foreign) == shape(missing)


def test_5_staff_cannot_open_manager_pages() -> None:
    staff = signed_in(PHONE_STAFF_SV)
    try:
        assert staff.http.get("/staff").status_code == 403
        assert staff.http.get("/staff/1").status_code == 403
    finally:
        staff.close()


# --- 6. CSRF ------------------------------------------------------------------------------------


def test_6_posts_without_a_csrf_token_are_refused(manager_a: Browser) -> None:
    assert (
        manager_a.http.post(
            "/staff", data={"display_name": "x", "phone": "+22234567890", "role": "staff"}
        ).status_code
        == 403
    )
    assert (
        manager_a.http.post(
            "/uploads", files={"file": ("1OCT.xlsx", FIXTURE.read_bytes(), XLSX)}
        ).status_code
        == 403
    )
    assert manager_a.http.post("/uploads", data={"csrf_token": "x" * 40}).status_code == 403


def test_6_a_foreign_origin_is_refused(manager_a: Browser) -> None:
    response = manager_a.http.post(
        "/uploads",
        data={"csrf_token": manager_a.token("/uploads/new")},
        files={"file": ("1OCT.xlsx", FIXTURE.read_bytes(), XLSX)},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


# --- 7. rate limit ------------------------------------------------------------------------------


def test_7_the_sixth_bad_login_is_a_429() -> None:
    phone = "+2229" + "".join(secrets.choice("0123456789") for _ in range(7))
    b = Browser()
    try:
        statuses = [b.login(phone, f"wrong-password-{i}").status_code for i in range(6)]
    finally:
        b.close()
    assert statuses == [401] * 5 + [429]


# --- 8. body limit ------------------------------------------------------------------------------


def test_8_a_declared_eleven_megabyte_post_gets_the_413_page_before_any_body() -> None:
    """Raw socket: send only the headers, and read the answer; the server must not wait for the
    body (no connection-reset race, because nothing big is written)."""
    parts = urlsplit(BASE_URL)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        pytest.skip("raw-socket variant needs plain http")
    origin = f"{parts.scheme}://{parts.netloc}"
    request = (
        f"POST /uploads HTTP/1.1\r\nHost: {parts.netloc}\r\nOrigin: {origin}\r\n"
        "Content-Type: multipart/form-data; boundary=x\r\n"
        f"Content-Length: {11 * 1024 * 1024}\r\n\r\n"
    )
    deadline = time.monotonic() + 30
    answer = b""
    with socket.create_connection((parts.hostname or "127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request.encode())
        while (
            len(answer) < MAX_RAW_ANSWER
            and time.monotonic() < deadline
            and (b"\r\n\r\n" not in answer or b"</html>" not in answer)
        ):
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            answer += chunk
    assert answer.startswith(b"HTTP/1.1 413"), answer[:80]
    assert b"Fichier trop volumineux" in answer
    assert b"traceback" not in answer.lower()


# The full 11 MB body is not sent over the wire here: whether the client sees the 413 or a reset
# connection depends on the TCP stack, so the test would be flaky. The declared-length case above
# (a real socket, headers only) and the streamed in-process case (tests/test_upload_body_limit.py)
# cover the same two code paths deterministically.


# --- 9. error pages and headers -----------------------------------------------------------------


def test_9_the_404_page_has_no_fingerprints_and_the_security_headers_are_there() -> None:
    response = httpx.get(f"{BASE_URL}/no-such-page-" + secrets.token_hex(3), timeout=30)
    assert response.status_code == 404
    assert "Page introuvable" in response.text
    headers = {k.lower(): v for k, v in response.headers.items()}
    assert "server" not in headers
    assert "x-powered-by" not in headers
    assert headers["content-security-policy"].startswith("default-src 'self'")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert "permissions-policy" in headers
    assert headers["x-request-id"]
    lowered = response.text.lower()
    for fingerprint in ("fastapi", "starlette", "uvicorn", "traceback", "python", "not found"):
        assert fingerprint not in lowered, fingerprint


def test_9_docs_and_openapi_are_not_served() -> None:
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert httpx.get(f"{BASE_URL}{path}", timeout=30).status_code == 404


def test_9_healthz_is_ok() -> None:
    response = httpx.get(f"{BASE_URL}/healthz", timeout=30)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
