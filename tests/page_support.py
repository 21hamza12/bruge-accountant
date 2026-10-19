"""A small environment for the page tests: two agencies, a manager and a staff member each,
the synthetic report loaded on demand, and logged-in clients."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.ingest import ingest_report
from app.models import StaffSignLink
from app.tenancy import rebind_tenant
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
    login,
    make_client,
    owner_session,
    seed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
DAY = "2026-10-01"
NNBSP = " "


def fr(amount: int) -> str:
    """An amount the way the pages print it: ``1118857`` becomes ``1 118 857`` (narrow no-break)."""
    return f"{amount:,}".replace(",", NNBSP)


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock)

    def as_user(self, phone: str, password: str) -> TestClient:
        c = self.factory()
        assert login(c, phone, password).status_code == 303
        return c

    def manager_a(self) -> TestClient:
        return self.as_user(PHONE_A, PASSWORD_A)

    def staff_a(self) -> TestClient:
        return self.as_user(PHONE_STAFF_A, PASSWORD_S)

    def manager_b(self) -> TestClient:
        return self.as_user(PHONE_B, PASSWORD_B)

    def load(self, agency_id: int | None = None, data: bytes | None = None) -> None:
        with owner_session(self.db.owner_url) as s:
            ingest_report(
                s,
                agency_id or self.seeded.agency_a,
                FIXTURE.read_bytes() if data is None else data,
                "1OCT.xlsx",
            )

    def link(self, user_id: int, sign: str, agency_id: int | None = None) -> None:
        agency = agency_id or self.seeded.agency_a
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency)
            s.add(StaffSignLink(agency_id=agency, user_id=user_id, staff_sign=sign))
            s.commit()
