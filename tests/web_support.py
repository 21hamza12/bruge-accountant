"""Helpers for the login / account tests: a fake clock, seeded agencies and users, HTTP helpers."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache

import httpx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.auth.passwords import hash_password
from app.db import make_engine, make_session_factory
from app.models import ROLE_STAFF, User
from app.repository import bootstrap_agency, create_agency
from app.settings import Settings
from app.tenancy import rebind_tenant
from app.web.main import create_app

T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)
ORIGIN = "https://testserver"
PASSWORD_A = "Alpha-pass-2026!"  # noqa: S105
PASSWORD_B = "Bravo-pass-2026!"  # noqa: S105
PASSWORD_S = "Staff-pass-2026!"  # noqa: S105
NEW_PASSWORD = "Brand-new-pass-77"  # noqa: S105
PHONE_A = "+22212345601"
PHONE_B = "+22212345602"
PHONE_STAFF_A = "+22212345603"


class FakeClock:
    """A settable clock: call it for "now", ``advance`` to move it."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@cache
def hashed(password: str) -> str:
    return hash_password(password)


@dataclass(frozen=True)
class WebDb:
    """``mode``: sqlite, app-role (separate non-owner role) or single-role (owner only)."""

    mode: str
    owner_url: str
    app_url: str


@dataclass(frozen=True)
class Seeded:
    agency_a: int
    agency_b: int
    manager_a: int
    manager_b: int
    staff_a: int


@contextmanager
def owner_session(url: str) -> Iterator[Session]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def add_user(
    owner_url: str,
    agency_id: int,
    phone: str,
    *,
    role: str = ROLE_STAFF,
    password: str | None = PASSWORD_S,
    display_name: str = "Employe",
    must_change: bool = False,
    active: bool = True,
) -> int:
    with owner_session(owner_url) as s:
        rebind_tenant(s, agency_id)
        user = User(
            agency_id=agency_id,
            phone=phone,
            role=role,
            display_name=display_name,
            password_hash=hashed(password) if password else None,
            must_change_password=must_change,
            is_active=active,
        )
        s.add(user)
        s.commit()
        return user.id


def seed(owner_url: str) -> Seeded:
    """Agency A (manager + one staff member) and agency B (manager)."""
    with owner_session(owner_url) as s:
        a = bootstrap_agency(s, "agence-a", "Agence A", PHONE_A, "Manager A", hashed(PASSWORD_A))
        b = bootstrap_agency(s, "agence-b", "Agence B", PHONE_B, "Manager B", hashed(PASSWORD_B))
    staff = add_user(owner_url, a.agency_id, PHONE_STAFF_A, display_name="Staff A")
    return Seeded(a.agency_id, b.agency_id, a.user_id, b.user_id, staff)


def make_agency(owner_url: str, slug: str) -> int:
    with owner_session(owner_url) as s:
        return create_agency(s, slug, slug.upper()).id


def make_client(db: WebDb, clock: FakeClock, **overrides: object) -> Callable[[], TestClient]:
    """A factory (so one test can hold several browsers) of clients on an app built for ``db``."""
    settings = Settings(database_url=SecretStr(db.app_url), **overrides)  # type: ignore[arg-type]
    app = create_app(settings, clock=clock)

    def factory() -> TestClient:
        return TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN})

    factory.app = app  # type: ignore[attr-defined]
    return factory


_HIDDEN = re.compile(r'name="csrf_token"\s+value="([^"]+)"')
_BODY_TOKEN = re.compile(r'data-csrf-token="([^"]+)"')


def form_token(response: httpx.Response) -> str:
    match = _HIDDEN.search(response.text)
    assert match, "no csrf_token hidden field in the page"
    return match.group(1)


def page_token(response: httpx.Response) -> str:
    match = _BODY_TOKEN.search(response.text)
    assert match, "no data-csrf-token on the page"
    return match.group(1)


def login(client: TestClient, phone: str, password: str, *, follow: bool = False) -> httpx.Response:
    page = client.get("/login")
    assert page.status_code == 200
    response: httpx.Response = client.post(
        "/login",
        data={"phone": phone, "password": password, "csrf_token": form_token(page)},
        follow_redirects=follow,
    )
    return response


def csrf_for(client: TestClient, path: str = "/") -> str:
    """The synchronizer token of a logged-in client, read from any page that carries it."""
    page = client.get(path)
    assert page.status_code == 200, page.status_code
    return page_token(page)
