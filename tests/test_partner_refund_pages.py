"""F4 review fixes on the pages (28 Oct 2026): the RFND proposals are made after the comparison is
committed, in a transaction of their own (the upload and « Relancer la comparaison »), a failed
proposal never undoes the comparison, a deadlock or a serialization failure is a French
« réessayez » (the close and the comparison run), and a pending Amadeus RFND says that its amount
is Amadeus's. SQLite; made-up names and numbers only."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import flags, partner_book, partner_refunds, partners, sales
from app.errors import RETRY_LATER, is_lock_conflict
from app.models import ComparisonRun, PartnerEntry, ReportDay, User
from app.sales import SaleInput, TicketInput
from app.tenancy import rebind_tenant
from tests.builders import amadeus_line, build_amadeus_xlsx
from tests.flag_support import NOW, FlagWeb
from tests.page_support import Env
from tests.test_partner_account_pages import close_days, post, setup_partner
from tests.web_support import WebDb, csrf_for, owner_session

D19 = date(2026, 10, 19)
DOC = "1990000031"
NNBSP = " "  # app.formatting.NARROW_NBSP


class _Driver(Exception):
    """A stand-in for a driver error that carries a SQLSTATE (psycopg's ``sqlstate``)."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__("deadlock detected")
        self.sqlstate = sqlstate


def lock_conflict(sqlstate: str = "40P01") -> OperationalError:
    return OperationalError("UPDATE ...", {}, _Driver(sqlstate))


# --- recognising the refusal --------------------------------------------------------------------


@pytest.mark.parametrize("code", ["40P01", "40001"])
def test_a_deadlock_or_serialization_failure_is_a_lock_conflict(code: str) -> None:
    assert is_lock_conflict(lock_conflict(code))


@pytest.mark.parametrize(
    "exc",
    [lock_conflict("23514"), lock_conflict(""), RuntimeError("40P01"), ValueError()],
)
def test_anything_else_is_not(exc: BaseException) -> None:
    assert not is_lock_conflict(exc)


# --- the close and the comparison run answer « réessayez » --------------------------------------


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


@pytest.mark.parametrize("code", ["40P01", "40001"])
def test_the_close_maps_a_deadlock_to_try_again(
    env: Env, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    pid = setup_partner(env)
    close_days(env)
    m = env.manager_a()

    def deadlocked(*args: Any, **kwargs: Any) -> Any:
        raise lock_conflict(code)

    monkeypatch.setattr(partner_book, "close_period", deadlocked)
    path = f"/partner-accounts/{pid}/periods/2026-10-01/close"
    response = post(m, path, {}, via=path)
    assert response.status_code == 409
    assert "réessayez" in response.text and "rien n" in response.text
    assert RETRY_LATER.split(":")[0] in response.text


@pytest.fixture
def world(web_db: WebDb) -> FlagWeb:
    return FlagWeb(web_db)


def _partner_sale_on_the_19th(world: FlagWeb) -> int:
    """« Partenaire Test » with a sale of ticket ``DOC`` filed on 19 October, day then closed."""
    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        partner = partners.create_partner(s, manager, "Partenaire Test", now=NOW)
        partners.set_fee(s, partner, Decimal("300"), date(2026, 1, 1), manager, now=NOW)
        s.commit()
        ticket = TicketInput(
            doc_number=DOC, airline="199", total_doc="21368", pax_name="SYNTH01/PAX",
            pax_type="ADT", rloc="ABC123", trnc="TKTT",
        )  # fmt: skip
        data = SaleInput(
            tickets=(ticket,), ag_kind="partner", partner_id=partner.id,
            payment_method="credit", staff_sign="0004SV",
        )  # fmt: skip
        sales.create_sale(s, manager, data, day=D19, now=NOW)
        for row in s.scalars(select(ReportDay).where(ReportDay.agency_id == world.seeded.agency_a)):
            row.status, row.closed_at, row.closed_by_user_id = "closed", NOW, manager.id
        s.commit()
        return partner.id


def _refund_report() -> bytes:
    return build_amadeus_xlsx(
        [amadeus_line(doc=DOC, total=-2505, trnc="RFND")], range_raw="20OCT", current="22-OCT-2026"
    )


def _count(world: FlagWeb, model: type[ComparisonRun] | type[PartnerEntry]) -> int:
    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        query = select(func.count()).select_from(model)
        found = s.scalar(query.where(model.agency_id == world.seeded.agency_a))
        return int(found or 0)


def test_the_run_route_maps_a_deadlock_to_try_again(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _partner_sale_on_the_19th(world)
    with world.manager() as c:
        assert world.upload(c, _refund_report()).status_code == 303
        runs = _count(world, ComparisonRun)

        def deadlocked(*args: Any, **kwargs: Any) -> Any:
            raise lock_conflict("40P01")

        monkeypatch.setattr(flags, "run_comparison", deadlocked)
        response = c.post(
            "/flags/run?date=2026-10-20",
            data={"csrf_token": csrf_for(c, "/flags")},
            follow_redirects=False,
        )
    assert response.status_code == 409 and "réessayez" in response.text
    assert _count(world, ComparisonRun) == runs


def test_the_upload_proposes_after_the_comparison_is_committed(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _partner_sale_on_the_19th(world)
    seen: list[int] = []
    real = partner_refunds.propose_after_comparison

    def watching(*args: Any, **kwargs: Any) -> list[PartnerEntry]:
        seen.append(_count(world, ComparisonRun))  # read by ANOTHER connection: committed
        return real(*args, **kwargs)

    monkeypatch.setattr(partner_refunds, "propose_after_comparison", watching)
    with world.manager() as c:
        assert world.upload(c, _refund_report()).status_code == 303
    assert seen == [1]
    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        (entry,) = s.scalars(select(PartnerEntry).where(PartnerEntry.source == "amadeus"))
        assert (entry.partner_id, entry.doc_number, entry.amount) == (
            pid,
            DOC,
            Decimal("-2505.00"),
        )


def test_a_failed_proposal_keeps_the_upload_and_the_comparison(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    _partner_sale_on_the_19th(world)

    def boom(*args: Any, **kwargs: Any) -> list[PartnerEntry]:
        raise RuntimeError("broken")

    monkeypatch.setattr(partner_refunds, "propose_refunds", boom)
    with world.manager() as c:
        assert world.upload(c, _refund_report()).status_code == 303
        assert _count(world, ComparisonRun) == 1
        assert _count(world, PartnerEntry) == 0
        monkeypatch.undo()
        again = c.post(
            "/flags/run?date=2026-10-20",
            data={"csrf_token": csrf_for(c, "/flags")},
            follow_redirects=False,
        )
    assert again.status_code == 303
    assert _count(world, PartnerEntry) == 1  # « Relancer » proposed it, once


# --- the amount is Amadeus's, to be checked -----------------------------------------------------


def _amadeus_refund(env: Env, pid: int) -> None:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        s.add(
            PartnerEntry(
                agency_id=env.seeded.agency_a,
                partner_id=pid,
                entry_date=date(2026, 10, 17),
                kind="RFND",
                amount=Decimal("-2505.00"),
                reason="Remboursement proposé depuis le rapport Amadeus du 17 octobre 2026",
                source="amadeus",
                doc_number="3940000001",
                created_by_user_id=env.seeded.manager_a,
            )
        )
        s.commit()


def test_the_account_page_labels_a_pending_amadeus_amount(env: Env) -> None:
    pid = setup_partner(env)
    _amadeus_refund(env, pid)
    html = env.manager_a().get(f"/partner-accounts/{pid}").text
    row = html.split("billet 3940000001", 1)[1].split("</tr>", 1)[0]
    assert "montant Amadeus (à vérifier)" in row and f"-2{NNBSP}505" in row
    assert "Confirmer" in row


def test_the_home_alert_labels_a_pending_amadeus_amount(env: Env) -> None:
    pid = setup_partner(env)
    _amadeus_refund(env, pid)
    client: TestClient = env.manager_a()
    html = client.get("/").text
    block = html.split('id="partner-alerts"', 1)[1].split("</section>", 1)[0]
    assert "montant Amadeus (à vérifier)" in block and "billet 3940000001" in block
