"""A login flood must not starve the service: Argon2 is bounded, connections are released before
it runs, and ``/healthz`` does not share the request thread pool."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import httpx2 as httpx
import pytest
from argon2.exceptions import VerifyMismatchError

from app.auth import passwords
from app.web.cookies import login_csrf_cookie_name
from app.web.csrf import new_login_token
from tests.web_support import (
    ORIGIN,
    PASSWORD_A,
    PASSWORD_S,
    PHONE_A,
    PHONE_STAFF_A,
    FakeClock,
    WebDb,
    add_user,
    form_token,
    issue_temp,
    make_client,
    seed,
)


class GateHasher:
    """Stands in for the Argon2 hasher: tracks how many calls overlap, optionally blocks them."""

    def __init__(self, real: Any, hold: threading.Event | None = None, pause: float = 0.0) -> None:
        self.real = real
        self.hold = hold
        self.pause = pause
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.calls = 0

    def _enter(self) -> None:
        with self.lock:
            self.running += 1
            self.calls += 1
            self.peak = max(self.peak, self.running)
        if self.hold is not None:
            self.hold.wait(30)
        if self.pause:
            time.sleep(self.pause)

    def _leave(self) -> None:
        with self.lock:
            self.running -= 1

    def verify(self, *args: Any) -> bool:
        self._enter()
        try:
            return bool(self.real.verify(*args))
        finally:
            self._leave()

    def hash(self, password: str) -> str:
        self._enter()
        try:
            return str(self.real.hash(password))
        finally:
            self._leave()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


# --- the semaphore -----------------------------------------------------------------------------


def test_the_semaphore_bounds_concurrent_argon2_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    real = passwords._HASHER
    gate = GateHasher(real, pause=0.05)
    stored = real.hash("Some-pass-123456")
    monkeypatch.setattr(passwords, "_HASHER", gate)
    jobs = (
        [lambda: passwords.verify_password("Some-pass-123456", stored)] * 4
        + [passwords.verify_dummy] * 4
        + [lambda: passwords.hash_password("Another-pass-1234")] * 4
    )
    threads = [threading.Thread(target=job) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert gate.calls == 12
    assert gate.peak == passwords.ARGON2_CONCURRENCY == 2  # bounded, yet not serialised


def test_a_failed_verification_still_gives_its_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    class Mismatch:
        def verify(self, *_a: object) -> bool:
            raise VerifyMismatchError

    monkeypatch.setattr(passwords, "_HASHER", Mismatch())
    real_hash = "$argon2id$v=19$m=19456,t=2,p=1$c29tZXNhbHQ$" + "A" * 43
    for _ in range(passwords.ARGON2_CONCURRENCY * 3):  # would deadlock if a slot leaked
        assert passwords.verify_password("whatever-1234", real_hash) is False
        passwords.verify_dummy()


# --- /healthz while logins hold every Argon2 slot ----------------------------------------------


def test_healthz_answers_while_a_login_flood_holds_the_argon2_slots(
    web_db: WebDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_client(web_db, FakeClock())
    app = factory.app  # type: ignore[attr-defined]
    release = threading.Event()
    gate = GateHasher(passwords._HASHER, hold=release)
    monkeypatch.setattr(passwords, "_HASHER", gate)
    cookie = login_csrf_cookie_name(app.state.settings)
    flood = 48  # more than the 40 worker threads of the shared pool

    async def scenario() -> tuple[float, int, list[int]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}
        ) as client:
            token = new_login_token()
            client.cookies.set(cookie, token)
            logins = [
                asyncio.create_task(
                    client.post(
                        "/login",
                        data={
                            "phone": f"+2225{n:07d}",
                            "password": "wrong-password-1",
                            "csrf_token": token,
                        },
                    )
                )
                for n in range(flood)
            ]
            try:
                await asyncio.sleep(1.5)  # the logins now sit in the threads, behind the slots
                started = time.monotonic()
                health = await asyncio.wait_for(client.get("/healthz"), timeout=1.5)
                elapsed = time.monotonic() - started
            finally:
                release.set()
            done = await asyncio.gather(*logins)
        return elapsed, health.status_code, [r.status_code for r in done]

    elapsed, health_status, login_statuses = asyncio.run(scenario())
    assert health_status == 200
    assert elapsed < 1.0
    assert gate.peak <= passwords.ARGON2_CONCURRENCY
    assert set(login_statuses) <= {401, 429}


# --- no connection is held during the verification --------------------------------------------


class PoolSpy(GateHasher):
    def __init__(self, real: Any, engine: Any) -> None:
        super().__init__(real)
        self.engine = engine
        self.checked_out: list[int] = []

    def verify(self, *args: Any) -> bool:
        self.checked_out.append(int(self.engine.pool.checkedout()))
        return super().verify(*args)


@pytest.mark.parametrize(
    "case", ["right", "wrong", "unknown", "inactive", "invalid-phone", "temporary"]
)
def test_no_database_connection_is_checked_out_during_the_verification(
    web_db: WebDb, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    seeded = seed(web_db.owner_url)
    clock = FakeClock()
    factory = make_client(web_db, clock)
    app = factory.app  # type: ignore[attr-defined]
    add_user(web_db.owner_url, seeded.agency_a, "+22212345610", active=False)
    phone, password = {
        "right": (PHONE_A, PASSWORD_A),
        "wrong": (PHONE_A, "wrong-password-1"),
        "unknown": ("+22299999999", PASSWORD_A),
        "inactive": ("+22212345610", PASSWORD_S),
        "invalid-phone": ("not a phone", PASSWORD_A),
        "temporary": (PHONE_STAFF_A, ""),
    }[case]
    if case == "temporary":
        password = issue_temp(web_db, clock, seeded.agency_a, seeded.staff_a, seeded.manager_a)
    spy = PoolSpy(passwords._HASHER, app.state.engine)
    with factory() as client:
        page = client.get("/login")
        monkeypatch.setattr(passwords, "_HASHER", spy)
        response = client.post(
            "/login",
            data={"phone": phone, "password": password, "csrf_token": form_token(page)},
            follow_redirects=False,
        )
    assert response.status_code == (303 if case in ("right", "temporary") else 401)
    assert spy.checked_out, "the verification never ran"
    assert set(spy.checked_out) == {0}, spy.checked_out
