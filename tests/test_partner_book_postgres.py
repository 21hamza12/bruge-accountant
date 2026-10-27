"""Partner accounts on a real Postgres server, connected the way the application connects (the
separate app role, or the owner alone in single-role mode), under FORCE row-level security.

Skipped without a server (``TEST_DATABASE_URL``): these run in CI. They cover what SQLite cannot:
the advisory locks of migration 0017 and of the close, the ``FOR SHARE`` / ``FOR UPDATE`` row
locks under the policies and grants, the plpgsql triggers raising ``check_violation``, and the
``closing_reconciles`` CHECK.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app import partner_book, partners, sales
from app.db import make_engine, make_session_factory
from app.models import Partner, PartnerEntry, PartnerEntryDecision, PartnerPeriod, ReportDay, User
from app.repository import create_agency
from app.sales import SaleInput, TicketInput
from app.tenancy import bind_tenant, rebind_tenant
from tests.conftest import RlsDatabase

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
OCT_1, OCT_3, OCT_16 = date(2026, 10, 1), date(2026, 10, 3), date(2026, 10, 16)
WAIT_S = 20


@dataclass(frozen=True)
class World:
    db: RlsDatabase
    agency_id: int
    manager: int
    manager2: int
    accountant: int
    partner: int
    sale: int


def _sale(partner_id: int, cost: str) -> SaleInput:
    return SaleInput(
        ag_kind="partner",
        partner_id=partner_id,
        payment_method="credit",
        staff_sign="0004SV",
        tickets=(
            TicketInput(
                doc_number="3940000001",
                airline="199",
                total_doc=cost,
                pax_name="SYNTH01/PAX",
                pax_type="ADT",
                rloc="ABC123",
                trnc="TKTT",
            ),
        ),
    )


def _set_days(url: str, agency_id: int, status: str, closer: int) -> None:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency_id)
            for row in s.scalars(select(ReportDay).where(ReportDay.agency_id == agency_id)):
                row.status = status
                row.closed_at = NOW if status == "closed" else None
                row.closed_by_user_id = closer if status == "closed" else None
            s.commit()
    finally:
        engine.dispose()


@pytest.fixture
def world(rls_db: RlsDatabase) -> World:
    """An agency (two managers, an accountant), « Partenaire Test » with a fee of 300 and one
    sale of 21 368 on 3 October, every day closed. Built by the owner."""
    engine = make_engine(rls_db.owner_url)
    try:
        with make_session_factory(engine)() as s:
            agency_id = create_agency(s, "agence-pg", "Agence PG").id
            rebind_tenant(s, agency_id)
            users = [
                User(agency_id=agency_id, phone=phone, role=role)
                for phone, role in (
                    ("+22212380001", "manager"),
                    ("+22212380002", "manager"),
                    ("+22212380003", "accountant"),
                )
            ]
            s.add_all(users)
            s.commit()
            manager = users[0]
            partner = partners.create_partner(s, manager, "Partenaire Test", now=NOW)
            partners.set_fee(s, partner, Decimal("300"), date(2026, 1, 1), manager, now=NOW)
            s.commit()
            sale = sales.create_sale(s, manager, _sale(partner.id, "21368"), day=OCT_3, now=NOW)
            s.commit()
            manager_id, manager2_id, accountant_id = (u.id for u in users)
            built = World(
                rls_db, agency_id, manager_id, manager2_id, accountant_id, partner.id, sale.sale.id
            )
    finally:
        engine.dispose()
    _set_days(rls_db.owner_url, agency_id, "closed", built.manager)
    return built


@pytest.fixture
def app_engine(world: World) -> Iterator[Engine]:
    engine = make_engine(world.db.app_url)
    yield engine
    engine.dispose()


def _session(engine: Engine, agency_id: int) -> Session:
    s = make_session_factory(engine)()
    bind_tenant(s, agency_id)
    return s


def _user(s: Session, user_id: int) -> User:
    return s.scalars(select(User).where(User.id == user_id)).one()


def test_the_whole_flow_as_the_application_role(world: World, app_engine: Engine) -> None:
    with _session(app_engine, world.agency_id) as s:
        accountant, manager = _user(s, world.accountant), _user(s, world.manager)
        closed = partner_book.close_period(
            s, accountant, world.partner, OCT_1, request_key=partner_book.new_request_key(), now=NOW
        )
        s.commit()
        assert closed.row.closing_balance == Decimal("21668.00")
        line = partner_book.add_line(
            s, accountant, world.partner, kind="AJUSTEMENT", amount="200", direction="plus",
            reason="frais oubliés le 3", request_key=partner_book.new_request_key(),
            entry_date=OCT_16.isoformat(), now=NOW,
        )  # fmt: skip
        s.commit()
        partner_book.decide_line(
            s, manager, world.partner, line.row.id, decision="approve", now=NOW
        )
        partner_book.record_payment(
            s, accountant, world.partner, amount="10000", method="virement",
            received_by_user_id=accountant.id, request_key=partner_book.new_request_key(),
            paid_on=OCT_16.isoformat(), now=NOW,
        )  # fmt: skip
        s.commit()
        # A backdated line: refused by the application, in French.
        with pytest.raises(partner_book.PartnerAccountError, match="déjà clôturée"):
            partner_book.add_line(
                s, accountant, world.partner, kind="ADM", amount="10",
                reason="pénalité en retard", request_key=partner_book.new_request_key(),
                entry_date=OCT_3.isoformat(), now=NOW,
            )  # fmt: skip
        s.rollback()
    # The day is reopened and the sale corrected: a CORRECTION in the open period.
    _set_days(world.db.owner_url, world.agency_id, "open", world.manager)
    with _session(app_engine, world.agency_id) as s:
        sales.edit_sale(
            s, _user(s, world.manager), world.sale, _sale(world.partner, "22000"),
            "coût corrigé après envoi", now=NOW,
        )  # fmt: skip
        s.commit()
        [correction] = s.scalars(select(PartnerEntry).where(PartnerEntry.kind == "CORRECTION"))
        assert (correction.amount, correction.sale_id) == (Decimal("632.00"), world.sale)
        partner = s.get(Partner, world.partner)
        assert partner is not None
        acc = partner_book.account(s, world.agency_id, partner, NOW.date())
        assert acc.balance == Decimal("12500.00")  # 21 668 + 200 + 632 - 10 000
        assert s.scalar(select(func.count()).select_from(PartnerEntryDecision)) == 1


def test_the_database_refuses_a_line_in_a_closed_period_in_french(
    world: World, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _session(app_engine, world.agency_id) as s:
        accountant = _user(s, world.accountant)
        partner_book.close_period(
            s, accountant, world.partner, OCT_1, request_key=partner_book.new_request_key(), now=NOW
        )
        s.commit()
        monkeypatch.setattr(partner_book, "_check_date", lambda *_a, **_k: None)
        with pytest.raises(partner_book.AccountConflict) as refused:
            partner_book.add_line(
                s, accountant, world.partner, kind="ADM", amount="10", reason="pénalité en retard",
                request_key=partner_book.new_request_key(), entry_date=OCT_3.isoformat(), now=NOW,
            )  # fmt: skip
        assert str(refused.value) == partner_book.PERIOD_JUST_CLOSED


def test_a_close_waits_for_a_line_in_flight_and_counts_it(
    world: World, app_engine: Engine, request: pytest.FixtureRequest
) -> None:
    """Two sessions: a line dated in the period is flushed (the trigger holds the partner's
    SHARED advisory lock) but not committed; the close (EXCLUSIVE) waits for it, and its totals
    include the line once it commits."""
    holder = _session(app_engine, world.agency_id)
    request.addfinalizer(holder.close)
    partner_book.add_line(
        holder, _user(holder, world.accountant), world.partner, kind="ADM", amount="1500",
        reason="pénalité de la compagnie", request_key=partner_book.new_request_key(),
        entry_date=OCT_3.isoformat(), now=NOW,
    )  # fmt: skip
    outcome: dict[str, Any] = {}

    def close() -> None:
        with _session(app_engine, world.agency_id) as s:
            try:
                written = partner_book.close_period(
                    s, _user(s, world.manager), world.partner, OCT_1,
                    request_key=partner_book.new_request_key(), now=NOW,
                )  # fmt: skip
                s.commit()
                outcome["closing"] = written.row.closing_balance
            except BaseException as exc:
                outcome["error"] = exc

    closer = threading.Thread(target=close)
    closer.start()
    watcher = make_engine(world.db.owner_url)
    request.addfinalizer(watcher.dispose)
    deadline = time.monotonic() + WAIT_S
    with watcher.connect() as conn:
        while time.monotonic() < deadline:
            waiting = conn.execute(
                text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
            ).scalar()
            conn.commit()
            if waiting:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the close never waited for the line in flight")
    assert closer.is_alive()
    holder.commit()
    closer.join(WAIT_S)
    assert not closer.is_alive(), "the close hung"
    assert "error" not in outcome, outcome
    assert outcome["closing"] == Decimal("23168.00")  # 21 668 + the 1 500 in flight
    with _session(app_engine, world.agency_id) as s:
        [period] = s.scalars(select(PartnerPeriod))
        assert period.status == "closed" and period.entries_total == Decimal("1500.00")
