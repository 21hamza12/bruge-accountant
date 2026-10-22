"""Two connections writing sales at the same moment, on a real server.

SQLite serialises writers, so this only means something on Postgres and is skipped elsewhere.
Each worker connects the way the application does (the app role, or the owner alone) and binds
the tenant first, as a request does.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import days, sales
from app.db import make_engine, make_session_factory
from app.models import Sale, SaleTicket, SaleVersion, StaffSignLink, User
from app.repository import create_agency
from app.sales import DuplicateTicket, SaleInput, TicketInput
from app.tenancy import bind_tenant, rebind_tenant
from tests.conftest import RlsDatabase

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 22)
JOIN_TIMEOUT_S = 60
ROUNDS = 3
STAFF_A, STAFF_B, MANAGER = 0, 1, 2

Work = Callable[[Session, User], Any]


def _ticket(doc: str) -> TicketInput:
    return TicketInput(
        doc_number=doc,
        airline="199",
        total_doc="21368",
        pax_name="SYNTH01/PAX",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
    )


def _sale(*docs: str, pb: str = "24000", sign: str | None = None) -> SaleInput:
    return SaleInput(
        tickets=tuple(_ticket(d) for d in docs),
        pb=pb,
        clt="Client Synth",
        payment_method="cash",
        staff_sign=sign,
    )


class Setup:
    """An agency with two staff members (signs 0000SV and 0001SV) and a manager."""

    def __init__(self, rls_db: RlsDatabase, request: pytest.FixtureRequest) -> None:
        self.db = rls_db
        self.owner = make_engine(rls_db.owner_url)
        request.addfinalizer(self.owner.dispose)
        with make_session_factory(self.owner)() as s:
            self.agency_id = create_agency(s, "agence-c", "Agence C").id
            rebind_tenant(s, self.agency_id)
            self.user_ids: list[int] = []
            for n, role in enumerate(("staff", "staff", "manager")):
                user = User(agency_id=self.agency_id, phone=f"+2221236020{n}", role=role)
                s.add(user)
                s.commit()
                self.user_ids.append(user.id)
                if role == "staff":
                    link = StaffSignLink(
                        agency_id=self.agency_id, user_id=user.id, staff_sign=f"000{n}SV"
                    )
                    s.add(link)
                    s.commit()
        # The day exists beforehand, so the requests meet on the sale, not on opening the day.
        self.run(
            [
                (
                    MANAGER,
                    lambda s, u: sales.create_sale(
                        s, u, _sale("3940000001", sign="0000SV"), now=NOW
                    ),
                )
            ]
        )

    def run(self, jobs: list[tuple[int, Work]], *, together: bool = False) -> list[Any]:
        """Run each ``(user index, work)`` in its own thread, connection and transaction (each
        commits on success); the results, or the exception raised, in the same order."""
        results: list[Any] = [None] * len(jobs)
        start = threading.Barrier(len(jobs)) if together else None

        def worker(index: int) -> None:
            who, work = jobs[index]
            engine = make_engine(self.db.app_url)
            try:
                with make_session_factory(engine)() as s:
                    bind_tenant(s, self.agency_id)
                    actor = s.get(User, self.user_ids[who])
                    assert actor is not None
                    if start is not None:
                        start.wait(timeout=JOIN_TIMEOUT_S)
                    try:
                        results[index] = work(s, actor)
                        s.commit()
                    except BaseException as exc:
                        s.rollback()
                        results[index] = exc
            finally:
                engine.dispose()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(jobs))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(JOIN_TIMEOUT_S)
        assert not any(t.is_alive() for t in threads), "a worker hung"
        return results

    def read(self) -> Session:
        s = make_session_factory(self.owner)()
        bind_tenant(s, self.agency_id)
        return s


def _count(s: Session, model: Any) -> int:
    return s.scalar(select(func.count()).select_from(model)) or 0


@pytest.mark.parametrize("round_no", range(ROUNDS))
def test_two_requests_selling_the_same_ticket_one_wins(
    rls_db: RlsDatabase, request: pytest.FixtureRequest, round_no: int
) -> None:
    setup = Setup(rls_db, request)
    meet = threading.Barrier(2)

    def both_have_passed_the_check() -> None:
        # The public seam between "already sold?" and the write: release both into the insert.
        meet.wait(timeout=JOIN_TIMEOUT_S)

    sales.after_duplicate_check = both_have_passed_the_check
    request.addfinalizer(lambda: setattr(sales, "after_duplicate_check", None))
    shared = "3940000100"
    results = setup.run(
        [
            (STAFF_A, lambda s, u: sales.create_sale(s, u, _sale(shared), now=NOW)),
            (
                MANAGER,
                lambda s, u: sales.create_sale(
                    s, u, _sale(shared, pb="25000", sign="0000SV"), now=NOW
                ),
            ),
        ]
    )
    winners = [r for r in results if isinstance(r, sales.SaleResult)]
    losers = [r for r in results if isinstance(r, BaseException)]
    assert len(winners) == 1 and len(losers) == 1, results
    assert isinstance(losers[0], DuplicateTicket)
    assert losers[0].sale_id == winners[0].sale.id  # both may see the winner's sale (sign 0000SV)
    assert losers[0].doc_number == shared
    with setup.read() as s:
        assert _count(s, Sale) == 2  # the one made by the setup, and the winner
        assert _count(s, SaleVersion) == 2  # nothing of the loser is left
        assert s.scalars(select(SaleTicket.doc_number)).all().count(shared) == 1


def test_overlapping_multi_ticket_sales_in_opposite_order_do_not_deadlock(
    rls_db: RlsDatabase, request: pytest.FixtureRequest
) -> None:
    setup = Setup(rls_db, request)
    a, b = "3940000201", "3940000202"
    results = setup.run(
        [
            (STAFF_A, lambda s, u: sales.create_sale(s, u, _sale(a, b), now=NOW)),
            (STAFF_B, lambda s, u: sales.create_sale(s, u, _sale(b, a), now=NOW)),
        ],
        together=True,
    )
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["DuplicateTicket", "SaleResult"], results  # never a deadlock error
    with setup.read() as s:
        assert _count(s, Sale) == 2
        assert sorted(s.scalars(select(SaleTicket.doc_number))) == ["3940000001", a, b]


def test_two_edits_of_one_sale_are_applied_one_after_the_other(
    rls_db: RlsDatabase, request: pytest.FixtureRequest
) -> None:
    setup = Setup(rls_db, request)
    with setup.read() as s:
        sale_id = s.scalars(select(Sale.id)).one()

    def editor(pb: str) -> Work:
        def edit(s: Session, u: User) -> Any:
            data = _sale("3940000001", pb=pb, sign="0000SV")
            return sales.edit_sale(s, u, sale_id, data, "correction du prix", now=NOW)

        return edit

    results = setup.run([(MANAGER, editor("26000")), (MANAGER, editor("27000"))], together=True)
    assert all(isinstance(r, sales.SaleResult) for r in results), results
    with setup.read() as s:
        manager = s.get(User, setup.user_ids[MANAGER])
        assert manager is not None
        history = sales.sale_history_for(s, manager, sale_id)
        assert [v.version_no for v in history] == [1, 2, 3]
        assert sorted(v.pb for v in history[1:]) == [Decimal("26000.00"), Decimal("27000.00")]
        assert s.scalars(select(Sale.current_version)).one() == 3
        view = sales.get_sale_for(s, manager, sale_id)
        assert view is not None and view.pb == history[2].pb


def test_a_void_and_an_edit_racing_leave_a_clean_history(
    rls_db: RlsDatabase, request: pytest.FixtureRequest
) -> None:
    setup = Setup(rls_db, request)
    with setup.read() as s:
        sale_id = s.scalars(select(Sale.id)).one()
    results = setup.run(
        [
            (MANAGER, lambda s, u: sales.void_sale(s, u, sale_id, "doublon confirmé", now=NOW)),
            (
                MANAGER,
                lambda s, u: sales.edit_sale(
                    s,
                    u,
                    sale_id,
                    _sale("3940000001", pb="26000", sign="0000SV"),
                    "correction",
                    now=NOW,
                ),
            ),
        ],
        together=True,
    )
    voided = [r for r in results if isinstance(r, sales.SaleView)]
    assert len(voided) == 1, results  # the void always succeeds
    with setup.read() as s:
        manager = s.get(User, setup.user_ids[MANAGER])
        assert manager is not None
        history = sales.sale_history_for(s, manager, sale_id)
        assert [v.version_no for v in history] == list(range(1, len(history) + 1))
        assert history[-1].voided is True  # nothing was saved on top of the void
        assert s.scalars(select(SaleTicket.doc_number)).all() == []
        assert sales.check_sale_consistency(s, setup.agency_id) == []


def test_a_sale_waits_for_a_day_being_closed_then_is_refused(
    rls_db: RlsDatabase, request: pytest.FixtureRequest
) -> None:
    """Deterministic: A closes the day and holds the transaction open; B's create must block on the
    day lock (at least a second), and once A commits it finds the day closed."""
    setup = Setup(rls_db, request)
    closed = threading.Event()
    release = threading.Event()

    def close_and_hold(s: Session, u: User) -> Any:
        found = days.get_day(s, setup.agency_id, TODAY)
        assert found is not None
        result = days.close_day(s, u, found, now=NOW)
        closed.set()
        assert release.wait(timeout=JOIN_TIMEOUT_S)
        return result

    waited: list[float] = []

    def create_while_closing(s: Session, u: User) -> Any:
        assert closed.wait(timeout=JOIN_TIMEOUT_S)
        threading.Timer(1.2, release.set).start()
        began = time.monotonic()
        try:
            return sales.create_sale(s, u, _sale("3940000401"), now=NOW)
        finally:
            waited.append(time.monotonic() - began)

    results = setup.run([(MANAGER, close_and_hold), (STAFF_A, create_while_closing)])
    assert not isinstance(results[0], BaseException), results
    assert isinstance(results[1], days.DayClosed), results
    assert waited and waited[0] >= 1.0, waited  # it blocked until the closing committed
    with setup.read() as s:
        found = days.get_day(s, setup.agency_id, TODAY)
        assert found is not None and found.status == "closed"
        assert _count(s, Sale) == 1


def test_a_closing_waits_for_a_sale_being_saved(
    rls_db: RlsDatabase, request: pytest.FixtureRequest
) -> None:
    """The other order: A is saving a sale (it holds the day lock shared); the closing must wait
    for A to commit and then includes the sale."""
    setup = Setup(rls_db, request)
    saving = threading.Event()
    release = threading.Event()

    def create_and_hold(s: Session, u: User) -> Any:
        result = sales.create_sale(s, u, _sale("3940000402"), now=NOW)
        saving.set()
        assert release.wait(timeout=JOIN_TIMEOUT_S)
        return result

    waited: list[float] = []

    def close_meanwhile(s: Session, u: User) -> Any:
        assert saving.wait(timeout=JOIN_TIMEOUT_S)
        found = days.get_day(s, setup.agency_id, TODAY)
        assert found is not None
        threading.Timer(1.2, release.set).start()
        began = time.monotonic()
        try:
            return days.close_day(s, u, found, now=NOW)
        finally:
            waited.append(time.monotonic() - began)

    results = setup.run([(STAFF_A, create_and_hold), (MANAGER, close_meanwhile)])
    assert all(not isinstance(r, BaseException) for r in results), results
    assert waited and waited[0] >= 1.0, waited
    with setup.read() as s:
        found = days.get_day(s, setup.agency_id, TODAY)
        assert found is not None and found.status == "closed"
        assert _count(s, Sale) == 2
