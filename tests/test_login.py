"""Step 19: login, logout and the home page, every test under row-level security."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import app.web.routes.auth as auth_routes
from app.auth import passwords, ratelimit
from app.models import RateLimitBucket, UserSession
from app.settings import Settings
from app.web.cookies import login_csrf_cookie_name, session_cookie_name
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
    audit_events,
    audit_sequence,
    csrf_for,
    form_token,
    login,
    make_client,
    owner_session,
    seed,
)

GENERIC = "Numéro de téléphone ou mot de passe incorrect."
UNKNOWN_PHONE = "+22299999999"


class Env:
    def __init__(self, web_db: WebDb, **overrides: object) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock, **overrides)
        self.settings: Settings = self.factory.app.state.settings  # type: ignore[attr-defined]

    def client(self) -> TestClient:
        return self.factory()


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def _normalised(text: str) -> str:
    """A page with the per-response random parts removed, to compare two failures."""
    text = re.sub(r'name="csrf_token" value="[^"]+"', "", text)
    return re.sub(r"<code>[0-9A-F]{8}</code>", "", text)


# --- the form ---------------------------------------------------------------------------------


def test_login_page_is_french_with_security_headers_and_a_double_submit_cookie(env: Env) -> None:
    with env.client() as c:
        response = c.get("/login")
    assert response.status_code == 200
    assert "Se connecter" in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'self'")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    cookie = next(
        v for k, v in response.headers.multi_items() if k == "set-cookie" and "login_csrf" in v
    )
    assert cookie.startswith("__Host-login_csrf=")
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Domain" not in cookie
    assert form_token(response) in cookie
    assert "<script>" not in response.text
    assert " style=" not in response.text


def test_the_form_token_is_reused_while_the_cookie_is_valid(env: Env) -> None:
    with env.client() as c:
        first = form_token(c.get("/login"))
        assert form_token(c.get("/login")) == first


# --- success ----------------------------------------------------------------------------------


def test_successful_login_redirects_and_sets_a_hardened_cookie(env: Env) -> None:
    with env.client() as c:
        response = login(c, PHONE_A, PASSWORD_A)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookies = [v for k, v in response.headers.multi_items() if k == "set-cookie"]
        session_cookie = next(v for v in cookies if v.startswith("__Host-session="))
        assert "HttpOnly" in session_cookie
        assert "Secure" in session_cookie
        assert "SameSite=lax" in session_cookie
        assert "Path=/" in session_cookie
        assert "Domain" not in session_cookie
        assert any(v.startswith("__Host-login_csrf=;") or "Max-Age=0" in v for v in cookies)
        home = c.get("/")
    assert home.status_code == 200
    assert "Manager A" in home.text
    assert "Agence A" in home.text
    assert 'href="/staff"' in home.text
    assert 'action="/logout"' in home.text
    assert "Agence B" not in home.text


def test_staff_home_has_no_staff_link(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_STAFF_A, PASSWORD_S)
        home = c.get("/")
    assert home.status_code == 200
    assert "Staff A" in home.text
    assert 'href="/staff"' not in home.text


def test_every_login_issues_a_new_token_and_only_its_hash_is_stored(env: Env) -> None:
    tokens = []
    for _ in range(2):
        with env.client() as c:
            login(c, PHONE_A, PASSWORD_A)
            tokens.append(c.cookies.get(session_cookie_name(env.settings)))
    assert tokens[0] and tokens[1] and tokens[0] != tokens[1]
    with owner_session(env.db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, env.seeded.agency_a)
        rows = list(s.scalars(select(UserSession)))
    assert len(rows) == 2
    for row in rows:
        assert row.token_hash not in tokens
        assert all(t not in (row.token_hash, row.csrf_token) for t in tokens)


def test_login_binds_only_the_users_own_agency(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_B, PASSWORD_B)
        home = c.get("/")
    assert "Agence B" in home.text
    assert "Agence A" not in home.text


def test_logged_in_user_visiting_login_goes_home(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, PASSWORD_A)
        response = c.get("/login", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_phone_spellings_all_log_in(env: Env) -> None:
    for spelling in ("12 34 56 01", "+222 12 34 56 01", "0022212345601"):
        with env.client() as c:
            assert login(c, spelling, PASSWORD_A).status_code == 303


# --- failure: one generic answer --------------------------------------------------------------


def _failures(env: Env) -> dict[str, httpx.Response]:
    add_user(env.db.owner_url, env.seeded.agency_a, "+22212345610", active=False)
    add_user(env.db.owner_url, env.seeded.agency_a, "+22212345611", password=None)
    cases = {
        "wrong password": (PHONE_A, "not-the-password"),
        "unknown phone": (UNKNOWN_PHONE, PASSWORD_A),
        "inactive user": ("+22212345610", PASSWORD_S),
        "no password": ("+22212345611", PASSWORD_S),
        "not a phone": ("hello", PASSWORD_A),
        "empty": ("", ""),
    }
    out = {}
    for name, (phone, password) in cases.items():
        with env.client() as c:
            out[name] = login(c, phone, password)
    return out


def test_every_failure_gives_the_same_generic_page(env: Env) -> None:
    responses = _failures(env)
    pages = {name: _normalised(r.text) for name, r in responses.items()}
    assert {r.status_code for r in responses.values()} == {401}
    assert len(set(pages.values())) == 1, {n: len(p) for n, p in pages.items()}
    assert GENERIC in next(iter(pages.values()))
    assert "session" not in " ".join(
        v for r in responses.values() for k, v in r.headers.multi_items() if k == "set-cookie"
    ).replace("login_csrf", "")


class _CountingHasher:
    def __init__(self, real: Any) -> None:
        self.real = real
        self.verifications = 0

    def verify(self, *args: Any) -> bool:
        self.verifications += 1
        return bool(self.real.verify(*args))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


def test_each_failure_spends_exactly_one_argon2_verification(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_user(env.db.owner_url, env.seeded.agency_a, "+22212345610", active=False)
    add_user(env.db.owner_url, env.seeded.agency_a, "+22212345611", password=None)
    cases = [
        (PHONE_A, "wrong-password-1"),
        (UNKNOWN_PHONE, PASSWORD_A),
        ("+22212345610", PASSWORD_S),
        ("+22212345611", PASSWORD_S),
        ("not a phone", PASSWORD_A),
    ]
    for phone, password in cases:
        counter = _CountingHasher(passwords._HASHER)
        monkeypatch.setattr(passwords, "_HASHER", counter)
        with env.client() as c:
            page = c.get("/login")
            counter.verifications = 0
            c.post(
                "/login",
                data={"phone": phone, "password": password, "csrf_token": form_token(page)},
            )
        assert counter.verifications == 1, phone


# --- order: CSRF, then rate limit, then the lookup ---------------------------------------------


def test_a_csrf_failure_touches_neither_the_limiter_nor_the_user_table(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the user lookup ran before CSRF was checked")

    monkeypatch.setattr(auth_routes, "authenticate", boom)
    with env.client() as c:
        c.get("/login")
        for _ in range(8):
            response = c.post(
                "/login", data={"phone": PHONE_A, "password": "x", "csrf_token": "bad"}
            )
            assert response.status_code == 403
    with owner_session(env.db.owner_url) as s:
        assert s.scalars(select(RateLimitBucket)).first() is None


def test_the_rate_limit_runs_before_the_user_lookup(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with env.client() as c:
        for _ in range(5):
            assert login(c, PHONE_A, "wrong-password-x").status_code == 401

        def boom(*args: object, **kwargs: object) -> None:
            raise AssertionError("the user lookup ran although the phone is rate limited")

        monkeypatch.setattr(auth_routes, "authenticate", boom)
        response = login(c, PHONE_A, PASSWORD_A)
    assert response.status_code == 429


def test_the_sixth_bad_login_is_the_429_page(env: Env) -> None:
    with env.client() as c:
        statuses = [login(c, PHONE_A, f"wrong-password-{n}").status_code for n in range(6)]
        blocked = login(c, PHONE_A, "wrong-password-7")
    assert statuses == [401] * 5 + [429]
    assert "Trop de tentatives" in blocked.text
    assert blocked.headers["retry-after"] == str(15 * 60)
    assert blocked.headers["cache-control"] == "no-store"


def test_a_blocked_phone_stays_blocked_even_with_the_right_password_until_the_window_ends(
    env: Env,
) -> None:
    with env.client() as c:
        for n in range(5):
            login(c, PHONE_A, f"wrong-password-{n}")
        assert login(c, PHONE_A, PASSWORD_A).status_code == 429
        env.clock.advance(minutes=15)
        assert login(c, PHONE_A, PASSWORD_A).status_code == 303


def test_other_phones_are_not_blocked_by_one_phones_failures(env: Env) -> None:
    with env.client() as c:
        for n in range(6):
            login(c, PHONE_A, f"wrong-password-{n}")
        assert login(c, PHONE_B, PASSWORD_B).status_code == 303


def test_different_spellings_of_one_phone_share_one_limit(env: Env) -> None:
    spellings = ["12345601", "+22212345601", "0022212345601", "12 34 56 01", "+222 12345601"]
    with env.client() as c:
        for spelling in spellings:
            assert login(c, spelling, "wrong-password-z").status_code == 401
        assert login(c, "1234-5601", "wrong-password-z").status_code == 429


def test_one_ip_is_limited_across_many_phones_and_forwarded_for_is_ignored(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "LOGIN_IP", ratelimit.Limit("login_ip", 6, 15 * 60))
    with env.client() as c:
        statuses = []
        for n in range(7):
            phone = f"+2227{n:07d}"
            page = c.get("/login")
            response = c.post(
                "/login",
                data={
                    "phone": phone,
                    "password": "wrong-password-q",
                    "csrf_token": form_token(page),
                },
                headers={"X-Forwarded-For": f"203.0.113.{n}"},  # spoofing must not help
            )
            statuses.append(response.status_code)
    assert statuses == [401] * 6 + [429]


def test_rate_limit_keys_hold_no_phone(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, "wrong-password-1")
    with owner_session(env.db.owner_url) as s:
        keys = list(s.scalars(select(RateLimitBucket.key)))
    assert len(keys) == 2
    assert not any("2221234" in key for key in keys)


def _bucket_count(env: Env, limit: ratelimit.Limit, subject: str) -> int:
    key = ratelimit.bucket_key(limit, subject)
    with owner_session(env.db.owner_url) as s:
        rows = list(s.scalars(select(RateLimitBucket).where(RateLimitBucket.key == key)))
    return sum(r.count for r in rows)


def _fresh_login(env: Env, phone: str, password: str) -> int:
    """One login from a browser with no session (a logged-in browser never sees the form)."""
    with env.client() as browser:
        return login(browser, phone, password).status_code


def test_successful_logins_do_not_count_against_the_phone(env: Env) -> None:
    assert [_fresh_login(env, PHONE_A, PASSWORD_A) for _ in range(7)] == [303] * 7
    assert _bucket_count(env, ratelimit.LOGIN_PHONE, PHONE_A) == 0  # reset on success
    with env.client() as c:
        failures = [login(c, PHONE_A, f"wrong-password-{n}").status_code for n in range(6)]
    assert failures == [401] * 5 + [429]  # the sixth FAILURE is the first 429


def test_a_success_clears_the_failures_before_it(env: Env) -> None:
    with env.client() as c:
        for n in range(4):
            assert login(c, PHONE_A, f"wrong-password-{n}").status_code == 401
        assert login(c, PHONE_A, PASSWORD_A).status_code == 303
    with env.client() as c:
        again = [login(c, PHONE_A, f"wrong-password-x{n}").status_code for n in range(5)]
    assert again == [401] * 5


def test_successful_logins_do_not_use_up_the_ip_limit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "LOGIN_IP", ratelimit.Limit("login_ip", 3, 15 * 60))
    statuses = [_fresh_login(env, PHONE_A, PASSWORD_A) for _ in range(6)]
    assert _bucket_count(env, ratelimit.LOGIN_IP, "testclient") == 0
    with env.client() as c:
        failed = [login(c, PHONE_A, "wrong-password-1").status_code for _ in range(4)]
    assert statuses == [303] * 6
    assert failed == [401, 401, 401, 429]


def test_the_failure_count_survives_a_request_that_raises(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database went away mid-request")

    monkeypatch.setattr(auth_routes, "authenticate", boom)
    client = TestClient(
        env.factory.app,  # type: ignore[attr-defined]
        base_url="https://testserver",
        headers={"Origin": "https://testserver"},
        raise_server_exceptions=False,
    )
    with client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={"phone": PHONE_A, "password": "x" * 12, "csrf_token": form_token(page)},
        )
    assert response.status_code == 500
    assert _bucket_count(env, ratelimit.LOGIN_PHONE, PHONE_A) == 1
    assert _bucket_count(env, ratelimit.LOGIN_IP, "testclient") == 1


def test_an_ip_over_its_limit_does_not_burn_a_victims_phone_bucket(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "LOGIN_IP", ratelimit.Limit("login_ip", 1, 15 * 60))
    with env.client() as c:
        assert login(c, PHONE_A, "wrong-password-1").status_code == 401
        for _ in range(4):
            assert login(c, PHONE_A, "wrong-password-2").status_code == 429  # blocked by the IP
    assert _bucket_count(env, ratelimit.LOGIN_PHONE, PHONE_A) == 1  # only the first attempt


def test_an_unknown_client_address_skips_the_ip_limit_and_warns(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    from starlette.middleware import Middleware
    from starlette.types import ASGIApp, Receive, Scope, Send

    from app.web.main import create_app

    class NoClient:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope["client"] = None
            await self.app(scope, receive, send)

    app = create_app(
        env.settings.model_copy(), extra_middleware=[Middleware(NoClient)], clock=env.clock
    )
    with (
        caplog.at_level(logging.WARNING),
        TestClient(
            app, base_url="https://testserver", headers={"Origin": "https://testserver"}
        ) as c,
    ):
        statuses = [login(c, PHONE_A, f"wrong-password-{n}").status_code for n in range(6)]
    assert statuses == [401] * 5 + [429]  # the phone limit still applies
    with owner_session(env.db.owner_url) as s:
        keys = list(s.scalars(select(RateLimitBucket.key)))
    assert keys
    assert all(k.startswith("login_phone:") for k in keys)  # no shared "unknown" bucket
    assert any("without an address" in r.getMessage() for r in caplog.records)


def test_bare_posts_write_no_audit_rows_and_touch_no_bucket(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    before = audit_sequence(env.db.owner_url)
    with caplog.at_level(logging.INFO, logger="app.audit"), env.client() as c:
        statuses = {c.post("/login").status_code for _ in range(30)}
        statuses |= {
            c.post("/login", data={"phone": "1", "csrf_token": "x"}).status_code for _ in range(30)
        }
    assert statuses == {403}
    assert audit_sequence(env.db.owner_url) == before  # no row per bare POST
    logged = [r for r in caplog.records if r.name == "app.audit"]
    assert len(logged) == 60  # the JSON log keeps every one
    assert {r.event_type for r in logged} == {"csrf_failed"}  # type: ignore[attr-defined]
    with owner_session(env.db.owner_url) as s:
        assert s.scalars(select(RateLimitBucket)).first() is None


def test_a_flood_past_the_ip_limit_leaves_one_rate_limited_row(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "LOGIN_IP", ratelimit.Limit("login_ip", 2, 15 * 60))
    before = audit_sequence(env.db.owner_url)
    with env.client() as c:
        statuses = [login(c, UNKNOWN_PHONE, "wrong-password-1").status_code for _ in range(30)]
    assert statuses == [401, 401] + [429] * 28
    # two failed logins and ONE rate_limited row, however long the flood lasts
    assert audit_sequence(env.db.owner_url) - before == 3


# --- audit and logs ---------------------------------------------------------------------------


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "app.audit"]


def test_login_events_are_audited_with_masked_phones_and_no_secrets(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO), env.client() as c:
        login(c, PHONE_A, "wrong-password-1")
        login(c, UNKNOWN_PHONE, "another-wrong-1")
        login(c, PHONE_A, PASSWORD_A)
    success = audit_events(env.db.owner_url, env.seeded.agency_a, "login_success")
    failure = audit_events(env.db.owner_url, env.seeded.agency_a, "login_failure")
    assert len(success) == 1
    assert success[0].actor_user_id == env.seeded.manager_a
    assert success[0].details == {"phone": "+222******01"}
    assert len(failure) == 1  # the unknown phone has no agency, so no tenant can read it
    assert failure[0].details == {"reason": "bad_password", "phone": "+222******01"}
    assert failure[0].actor_user_id == env.seeded.manager_a
    records = _audit_records(caplog)
    unknown = [r for r in records if r.details.get("reason") == "unknown_phone"]  # type: ignore[attr-defined]
    assert len(unknown) == 1
    assert unknown[0].agency_id is None  # type: ignore[attr-defined]
    assert unknown[0].actor_user_id is None  # type: ignore[attr-defined]
    assert unknown[0].details["phone"] == "+222******99"  # type: ignore[attr-defined]
    everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (PASSWORD_A, "wrong-password-1", "another-wrong-1", PHONE_A, UNKNOWN_PHONE):
        assert secret not in everything


def test_an_unknown_phone_writes_an_agencyless_row_on_the_database_too(env: Env) -> None:
    before = audit_sequence(env.db.owner_url)
    with env.client() as c:
        login(c, UNKNOWN_PHONE, "another-wrong-1")
    assert audit_sequence(env.db.owner_url) == before + 1


def test_rate_limited_is_audited(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO), env.client() as c:
        for n in range(6):
            login(c, PHONE_A, f"wrong-password-{n}")
    events = [r for r in _audit_records(caplog) if r.event_type == "rate_limited"]  # type: ignore[attr-defined]
    assert len(events) == 1
    assert events[0].details == {"scope": "phone", "phone": "+222******01"}  # type: ignore[attr-defined]


# --- logout -----------------------------------------------------------------------------------


def test_logout_revokes_the_session_and_clears_the_cookie(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, PASSWORD_A)
        token = c.cookies.get(session_cookie_name(env.settings))
        csrf = csrf_for(c)
        response = c.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert "Max-Age=0" in response.headers["set-cookie"]
        c.cookies.set(session_cookie_name(env.settings), token or "")
        assert c.get("/", follow_redirects=False).status_code == 303
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "logout")) == 1
    with owner_session(env.db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, env.seeded.agency_a)
        assert all(r.revoked_at is not None for r in s.scalars(select(UserSession)))


def test_logout_without_csrf_is_a_403_and_keeps_the_session(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, PASSWORD_A)
        assert c.post("/logout").status_code == 403
        assert c.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
        assert c.get("/").status_code == 200


def test_logout_is_post_only(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, PASSWORD_A)
        assert c.get("/logout").status_code == 405


def test_anonymous_logout_is_idempotent_and_clears_the_cookies(env: Env) -> None:
    with env.client() as c:
        response = c.post("/logout", data={"csrf_token": "x"}, follow_redirects=False)
        again = c.post("/logout", follow_redirects=False)
    for r in (response, again):
        assert r.status_code == 303
        assert r.headers["location"] == "/login"
        cleared = [v for k, v in r.headers.multi_items() if k == "set-cookie"]
        assert any(v.startswith("__Host-session=") and "Max-Age=0" in v for v in cleared)


def test_a_stale_logout_after_the_session_ended_goes_to_the_login_page(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, PASSWORD_A)
        csrf = csrf_for(c)
        env.clock.advance(days=8)  # past the absolute expiry
        response = c.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_401_and_403_pages_link_to_the_way_out(env: Env) -> None:
    with env.client() as c:
        unauthorised = c.post("/password/change", data={"csrf_token": "x"})
        login(c, PHONE_STAFF_A, PASSWORD_S)
        forbidden = c.get("/staff")
    assert unauthorised.status_code == 401
    assert '<a href="/login">Se connecter</a>' in unauthorised.text
    assert forbidden.status_code == 403
    assert '<a href="/">Retour à l\'accueil</a>' in forbidden.text


def _drop_cookie(c: TestClient, name: str) -> str:
    """Remove every cookie of that name from the jar; return the value it had (or "")."""
    value = ""
    for cookie in list(c.cookies.jar):
        if cookie.name == name:
            value = cookie.value or ""
            c.cookies.jar.clear(cookie.domain, cookie.path, cookie.name)
    return value


def _cookie_header(env: Env, **cookies: str) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def _login_with_stale_cookie(env: Env, stale: str, phone: str, password: str) -> httpx.Response:
    """A login from a browser that still sends ``stale`` as its session cookie."""
    login_cookie = login_csrf_cookie_name(env.settings)
    with env.client() as c:
        page = c.get("/login")
        token = form_token(page)
        response: httpx.Response = c.post(
            "/login",
            data={"phone": phone, "password": password, "csrf_token": token},
            headers=_cookie_header(
                env, **{login_cookie: token, session_cookie_name(env.settings): stale}
            ),
            follow_redirects=False,
        )
    return response


def _session_cookie(response: httpx.Response, env: Env) -> str:
    name = session_cookie_name(env.settings)
    for k, v in response.headers.multi_items():
        if k == "set-cookie" and v.startswith(f"{name}="):
            return v.split(";")[0].split("=", 1)[1]
    raise AssertionError("no session cookie was set")


def _home_status(env: Env, session: str) -> int:
    with env.client() as c:
        response = c.get(
            "/",
            headers=_cookie_header(env, **{session_cookie_name(env.settings): session}),
            follow_redirects=False,
        )
    return int(response.status_code)


def test_login_revokes_the_session_the_browser_presented(env: Env) -> None:
    with env.client() as c:
        first = login(c, PHONE_A, PASSWORD_A)
    old = _session_cookie(first, env)
    assert _home_status(env, old) == 200
    second = _login_with_stale_cookie(env, old, PHONE_A, PASSWORD_A)
    assert second.status_code == 303
    new = _session_cookie(second, env)
    assert new != old
    assert _home_status(env, old) == 303  # the presented session is dead
    assert _home_status(env, new) == 200


def test_login_with_a_cookie_of_another_agency_still_works(env: Env) -> None:
    with env.client() as c:
        other = _session_cookie(login(c, PHONE_B, PASSWORD_B), env)
    response = _login_with_stale_cookie(env, other, PHONE_A, PASSWORD_A)
    assert response.status_code == 303  # no TenantBindingError
    assert _home_status(env, _session_cookie(response, env)) == 200
    assert _home_status(env, other) == 200  # another agency's session is left alone


def test_home_requires_login(env: Env) -> None:
    with env.client() as c:
        response = c.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_cookie_names_follow_cookie_secure(web_db: WebDb) -> None:
    env = Env(web_db, cookie_secure=False)
    assert session_cookie_name(env.settings) == "session"
    assert login_csrf_cookie_name(env.settings) == "login_csrf"
