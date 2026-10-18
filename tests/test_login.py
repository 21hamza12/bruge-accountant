"""Step 19: login, logout and the home page, every test under row-level security."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import passwords
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
    import app.web.routes.auth as auth_routes

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
    import app.web.routes.auth as auth_routes

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


def test_one_ip_is_limited_across_many_phones_and_forwarded_for_is_ignored(env: Env) -> None:
    with env.client() as c:
        statuses = []
        for n in range(21):
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
    assert statuses == [401] * 20 + [429]


def test_rate_limit_keys_hold_no_phone(env: Env) -> None:
    with env.client() as c:
        login(c, PHONE_A, "wrong-password-1")
    with owner_session(env.db.owner_url) as s:
        keys = list(s.scalars(select(RateLimitBucket.key)))
    assert len(keys) == 2
    assert not any("2221234" in key for key in keys)


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


def test_anonymous_logout_post_is_the_401_page(env: Env) -> None:
    with env.client() as c:
        response = c.post("/logout", data={"csrf_token": "x"})
    assert response.status_code == 401
    assert "Connexion requise" in response.text


def test_home_requires_login(env: Env) -> None:
    with env.client() as c:
        response = c.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_cookie_names_follow_cookie_secure(web_db: WebDb) -> None:
    env = Env(web_db, cookie_secure=False)
    assert session_cookie_name(env.settings) == "session"
    assert login_csrf_cookie_name(env.settings) == "login_csrf"
