"""Step 30: the hot queries use their indexes, as the application role, under row-level security.

The statements are not re-typed here: each case runs the real function while a listener records the
SQL it sends, then asks the planner (``EXPLAIN (FORMAT JSON)``) about exactly that SQL with
``SET LOCAL enable_seqscan = off`` and the same tenant (or narrow lookup) setting bound. With
sequential scans off, a query whose plan still has a Seq Scan has no usable index at all, and one
whose plan uses another index is a different query than the one we tuned for.

``EXPLAIN`` needs raw SQL, so this file uses ``exec_driver_sql``; the code guard only scans
``app/``. The tenant is bound with ``app.tenancy`` only (no ``set_config`` outside it).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, insert
from sqlalchemy.orm import Session

from app.auth import ratelimit
from app.auth.login import find_user_by_phone
from app.auth.sessions import SessionPolicy, hash_token, lookup_session
from app.db import make_engine, make_session_factory
from app.ingest import ingest_report
from app.models import StaffSignLink, Ticket, Upload, User
from app.repository import (
    awaiting_counts_by_sign,
    get_upload,
    latest_report_date,
    recent_report_dates,
    recent_uploads,
    tickets_awaiting_price,
    tickets_for_day,
    upload_ticket_dates,
)
from app.settings import Settings
from app.tenancy import bind_tenant, lookup_scope, rebind_tenant
from tests.web_support import (
    PHONE_STAFF_A,
    FakeClock,
    Seeded,
    WebDb,
    owner_session,
    seed,
    session_cookie_for,
)

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
SQL_VERBS = ("SELECT", "INSERT", "UPDATE", "DELETE")
AGENCY_UPLOAD_INDEXES = frozenset(
    {
        "uq_uploads_agency_id_id",
        "ix_uploads_agency_uploaded_by",
        "ix_uploads_agency_report_date",
        "ix_uploads_agency_sha256",
    }
)


@dataclass(frozen=True)
class Scope:
    """How the session is set up before the query: a bound tenant or one narrow lookup key."""

    agency_id: int | None = None
    lookup: tuple[str, str] | None = None

    def apply(self, session: Session) -> None:
        if self.agency_id is not None:
            bind_tenant(session, self.agency_id)
        if self.lookup is not None:
            lookup_scope(session, *self.lookup)


@dataclass(frozen=True)
class Hot:
    name: str
    scope: Scope
    run: Callable[[Session], object]
    marker: str  # a substring of the SQL that identifies the statement to explain
    indexes: frozenset[str]  # any one of these must appear in the plan


class World:
    """Two agencies with data, a session token, and the app-role engine."""

    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        a, b = self.seeded.agency_a, self.seeded.agency_b
        with owner_session(web_db.owner_url) as s:
            self.upload_a = ingest_report(s, a, FIXTURE.read_bytes(), "a.xlsx").upload_id
        with owner_session(web_db.owner_url) as s:
            ingest_report(s, b, FIXTURE.read_bytes(), "b.xlsx")
        with owner_session(web_db.owner_url) as s:
            rebind_tenant(s, a)
            s.add(StaffSignLink(agency_id=a, user_id=self.seeded.staff_a, staff_sign="0004SV"))
            s.commit()
        self.pad_and_analyze()
        self.settings = Settings()
        self.clock = FakeClock()
        self.token = session_cookie_for(web_db, self.clock, self.settings, PHONE_STAFF_A)
        self.engine: Engine = make_engine(web_db.app_url)

    def pad_and_analyze(self) -> None:
        """Realistic volumes with fresh statistics.

        On a handful of rows every index path costs the same and the planner picks any of them,
        which proves nothing. So: many days of tickets per agency, a small agency next to a big one
        (the shape that makes ``ORDER BY id DESC LIMIT 50`` on the primary key a trap), and
        several users with two signs each. Rows are bulk-inserted as the owner.
        """
        a, b = self.seeded.agency_a, self.seeded.agency_b
        for agency_id, uploads in ((a, 60), (b, 1500)):
            with owner_session(self.db.owner_url) as s:
                rebind_tenant(s, agency_id)
                pads = [
                    Upload(
                        agency_id=agency_id,
                        filename=f"pad{i}.xlsx",
                        sha256="0" * 64,
                        report_range_raw="01OCT",
                        report_date=date(2026, 9, 1),
                        generated_on=date(2026, 9, 2),
                        ticket_count=0,
                        warnings=[],
                        inserted_count=0,
                        updated_count=0,
                    )
                    for i in range(uploads)
                ]
                s.add_all(pads)
                s.flush()
                upload_id = pads[0].id
                day0 = date(2026, 7, 1)
                rows = [
                    {
                        "agency_id": agency_id,
                        "first_upload_id": upload_id,
                        "last_upload_id": upload_id,
                        "report_date": day0 + timedelta(days=d),
                        "ticket_no": f"88{agency_id}{d:03d}{n:04d}",
                        "seq_no": str(n),
                        "airline": "199",
                        "cost": Decimal(1000 + n),
                        "passenger_name": f"PAD/{n}",
                        "pax_type": "ADT",
                        "staff_sign": f"{n % 5:04d}PD",
                        "transaction_type": "TKTT",
                        "booking_ref": f"P{d:03d}{n:04d}"[:16],
                        "flags": [],
                        "price_status": "awaiting_price",
                    }
                    for d in range(60)
                    for n in range(100)
                ]
                s.execute(insert(Ticket), rows)
                s.commit()
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, a)
            users = [
                User(
                    agency_id=a,
                    phone=f"+2223000{i:04d}",
                    role="staff",
                    display_name=f"Pad {i}",
                    password_hash=None,
                    must_change_password=False,
                )
                for i in range(30)
            ]
            s.add_all(users)
            s.flush()
            s.add_all(
                StaffSignLink(agency_id=a, user_id=u.id, staff_sign=f"P{u.id:03d}{k}")
                for u in users
                for k in range(2)
            )
            s.commit()
        engine = make_engine(self.db.owner_url)
        try:
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
                connection.exec_driver_sql("ANALYZE")
        finally:
            engine.dispose()

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def session(self) -> Iterator[Session]:
        with make_session_factory(self.engine)() as s:
            yield s


@pytest.fixture
def world(web_db: WebDb) -> Iterator[World]:
    if web_db.mode != "app-role":
        pytest.skip("the plans are checked as the separate application role on Postgres")
    w = World(web_db)
    try:
        yield w
    finally:
        w.close()


def hot_queries(w: World) -> list[Hot]:
    a, b = w.seeded.agency_a, w.seeded.agency_b
    token_hash = hash_token(w.token)
    policy = SessionPolicy.from_settings(w.settings)
    return [
        Hot(
            "session by token hash",
            Scope(lookup=("app.session_token_hash", token_hash)),
            lambda s: lookup_session(s, w.token, w.clock(), policy),
            "FROM user_sessions",
            frozenset({"uq_user_sessions_token_hash"}),
        ),
        Hot(
            "user by phone",
            Scope(lookup=("app.login_phone", PHONE_STAFF_A)),
            lambda s: find_user_by_phone(s, PHONE_STAFF_A),
            "FROM users",
            frozenset({"uq_users_phone"}),
        ),
        Hot(
            "tickets awaiting price",
            Scope(agency_id=a),
            lambda s: tickets_awaiting_price(s, a, date(2026, 10, 1)),
            "FROM tickets",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "tickets for a day (all statuses)",
            Scope(agency_id=a),
            lambda s: tickets_for_day(s, a, date(2026, 10, 1)),
            "FROM tickets",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "tickets for a day (some signs)",
            Scope(agency_id=a),
            lambda s: tickets_for_day(s, a, date(2026, 10, 1), ["0004SV"]),
            "FROM tickets",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "awaiting counts by sign",
            Scope(agency_id=a),
            lambda s: awaiting_counts_by_sign(s, a, date(2026, 10, 1)),
            "count(DISTINCT",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "latest report date",
            Scope(agency_id=a),
            lambda s: latest_report_date(s, a),
            "max(tickets.report_date)",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "latest report date (some signs)",
            Scope(agency_id=a),
            lambda s: latest_report_date(s, a, ["0009ER", "0004SV"]),
            "max(tickets.report_date)",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "recent report dates",
            Scope(agency_id=a),
            lambda s: recent_report_dates(s, a),
            "DISTINCT tickets.report_date",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "recent report dates (some signs)",
            Scope(agency_id=a),
            lambda s: recent_report_dates(s, a, staff_signs=["0009ER", "0004SV"]),
            "DISTINCT tickets.report_date",
            frozenset({"ix_tickets_agency_report_date_staff"}),
        ),
        Hot(
            "recent uploads (big agency)",
            Scope(agency_id=b),
            lambda s: recent_uploads(s, b, 50),
            "FROM uploads",
            # The big agency owns almost every row: walking the primary key backwards (or the
            # (agency_id, id) constraint) finds its newest 50 at once, with no sort.
            frozenset({"pk_uploads", "uq_uploads_agency_id_id"}),
        ),
        Hot(
            "recent uploads (small agency)",
            Scope(agency_id=a),
            lambda s: recent_uploads(s, a, 50),
            "FROM uploads",
            # Next to a big agency, walking the primary key backwards would be the trap; any index
            # that starts with agency_id is fine for sixty rows.
            AGENCY_UPLOAD_INDEXES,
        ),
        Hot(
            "recent uploads of one user (staff view)",
            Scope(agency_id=a),
            lambda s: recent_uploads(s, a, 50, uploaded_by=w.seeded.staff_a),
            "FROM uploads",
            AGENCY_UPLOAD_INDEXES,
        ),
        Hot(
            "upload by id for its owner (staff view)",
            Scope(agency_id=a),
            lambda s: get_upload(s, a, w.upload_a, uploaded_by=w.seeded.manager_a),
            "FROM uploads",
            frozenset({"uq_uploads_agency_id_id", "pk_uploads"}),
        ),
        Hot(
            "days of the tickets of one upload",
            Scope(agency_id=a),
            lambda s: upload_ticket_dates(s, a, w.upload_a),
            "FROM tickets",
            frozenset({"ix_tickets_agency_last_upload"}),
        ),
        Hot(
            "links by agency",
            Scope(agency_id=a),
            lambda s: sign_links_by_agency(s, a),
            "FROM staff_sign_links",
            frozenset({"uq_staff_sign_links_agency_staff_sign", "ix_staff_sign_links_agency_user"}),
        ),
        Hot(
            "links by user",
            Scope(agency_id=a),
            lambda s: sign_links_by_user(s, a, w.seeded.staff_a),
            "FROM staff_sign_links",
            frozenset({"ix_staff_sign_links_agency_user"}),
        ),
        Hot(
            "rate-limit upsert",
            Scope(),
            lambda s: ratelimit.hit(s, ratelimit.UPLOAD_AGENCY, str(a), w.clock()),
            "INSERT INTO rate_limit_buckets",
            frozenset({"pk_rate_limit_buckets"}),
        ),
        Hot(
            "upload by id",
            Scope(agency_id=a),
            lambda s: get_upload(s, a, w.upload_a),
            "FROM uploads",
            # The primary key is as good a unique lookup as the (agency_id, id) constraint.
            frozenset({"uq_uploads_agency_id_id", "pk_uploads"}),
        ),
    ]


def sign_links_by_agency(session: Session, agency_id: int) -> object:
    from app import accounts

    return accounts.list_staff(session, agency_id)


def sign_links_by_user(session: Session, agency_id: int, user_id: int) -> object:
    from app import accounts

    return accounts.get_signs(session, agency_id, user_id)


def record_sql(w: World, hot: Hot) -> list[tuple[str, Any]]:
    """The statements (text and parameters) the real function sends, tenant set up first."""
    seen: list[tuple[str, Any]] = []

    def listener(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if statement.lstrip().upper().startswith(SQL_VERBS) and "set_config" not in statement:
            seen.append((statement, parameters))

    event.listen(w.engine, "before_cursor_execute", listener)
    try:
        with w.session() as s:
            hot.scope.apply(s)
            hot.run(s)
            s.rollback()
    finally:
        event.remove(w.engine, "before_cursor_execute", listener)
    return [item for item in seen if hot.marker in item[0]]


def explain(w: World, hot: Hot, statement: str, parameters: Any) -> list[dict[str, Any]]:
    with w.session() as s:
        hot.scope.apply(s)
        connection = s.connection()
        connection.exec_driver_sql("SET LOCAL enable_seqscan = off")
        raw = connection.exec_driver_sql(f"EXPLAIN (FORMAT JSON) {statement}", parameters)
        document = raw.scalar_one()
        s.rollback()
    plans = json.loads(document) if isinstance(document, str) else document
    assert isinstance(plans, list)
    return plans


def walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def facts(plans: list[dict[str, Any]]) -> tuple[set[str], list[str]]:
    """(index names used or arbitrating a conflict, node types)."""
    names: set[str] = set()
    types: list[str] = []
    for node in walk(plans):
        if "Index Name" in node:
            names.add(str(node["Index Name"]))
        for arbiter in node.get("Conflict Arbiter Indexes", []):
            names.add(str(arbiter))
        if "Node Type" in node:
            types.append(str(node["Node Type"]))
    return names, types


CASE_NAMES = [
    "session by token hash",
    "user by phone",
    "tickets awaiting price",
    "tickets for a day (all statuses)",
    "tickets for a day (some signs)",
    "awaiting counts by sign",
    "latest report date",
    "latest report date (some signs)",
    "recent report dates",
    "recent report dates (some signs)",
    "recent uploads (big agency)",
    "recent uploads (small agency)",
    "links by agency",
    "links by user",
    "rate-limit upsert",
    "upload by id",
]


@pytest.mark.parametrize("name", CASE_NAMES)
def test_the_hot_query_uses_its_index(world: World, name: str) -> None:
    hot = next(h for h in hot_queries(world) if h.name == name)
    statements = record_sql(world, hot)
    assert statements, f"no SQL matching {hot.marker!r} was sent by {name!r}"
    for statement, parameters in statements:
        used, node_types = facts(explain(world, hot, statement, parameters))
        assert used & hot.indexes, (
            f"{name}: plan used {sorted(used)}, wanted one of {sorted(hot.indexes)}"
        )
        assert "Seq Scan" not in node_types, f"{name}: sequential scan in {node_types}"


def test_every_hot_query_is_covered() -> None:
    assert len(CASE_NAMES) == len(set(CASE_NAMES)) == 16
