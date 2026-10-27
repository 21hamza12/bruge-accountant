"""app/partner_accounts.py: the ticket lines of one partner over a date range (SQLite).

The sales are written through ``app.sales`` itself, so a line is exactly what the sales service
stores: the current version of an active partner sale, on the day it is filed under."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app import partner_ledger, partners, sales
from app.models import Partner, User
from app.partner_accounts import PartnerSaleLine, partner_sale_lines
from app.repository import create_agency
from app.sales import SaleInput, TicketInput

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
OCT_1, OCT_15, OCT_16 = date(2026, 10, 1), date(2026, 10, 15), date(2026, 10, 16)
SIGN = "0004SV"


def ticket(doc: str, cost: str = "21368") -> TicketInput:
    return TicketInput(
        doc_number=doc,
        airline="199",
        total_doc=cost,
        pax_name="SYNTH01/PAX",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
    )


def partner_sale(partner: Partner, *tickets: TicketInput) -> SaleInput:
    return SaleInput(
        ag_kind="partner",
        partner_id=partner.id,
        payment_method="credit",
        staff_sign=SIGN,
        tickets=tickets,
    )


class World:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.agency_id = create_agency(session, "agence-p", "Agence P").id
        self.manager = self._user(self.agency_id, "+22212370001")
        self.other_agency_id = create_agency(session, "agence-q", "Agence Q").id
        self.other_manager = self._user(self.other_agency_id, "+22212370002")
        self.partner = self._partner(self.manager, "Partenaire Test")
        self.second = self._partner(self.manager, "Partenaire Deux")
        self.foreign = self._partner(self.other_manager, "Partenaire Test")

    def _user(self, agency_id: int, phone: str) -> User:
        user = User(agency_id=agency_id, phone=phone, role="manager")
        self.db.add(user)
        self.db.commit()
        return user

    def _partner(self, actor: User, name: str) -> Partner:
        partner = partners.create_partner(self.db, actor, name, now=NOW)
        partners.set_fee(self.db, partner, Decimal("300"), date(2026, 1, 1), actor, now=NOW)
        self.db.commit()
        return partner

    def sell(self, data: SaleInput, day: date, actor: User | None = None) -> int:
        result = sales.create_sale(self.db, actor or self.manager, data, day=day, now=NOW)
        self.db.commit()
        return result.sale.id

    def lines(self, start: date = OCT_1, end: date = OCT_15, **kw: Any) -> list[PartnerSaleLine]:
        partner_id = kw.pop("partner_id", self.partner.id)
        agency_id = kw.pop("agency_id", self.agency_id)
        return partner_sale_lines(self.db, agency_id, partner_id, start, end)


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


def test_a_partner_sale_is_one_line_with_its_tickets_fee_and_pb(w: World) -> None:
    sale_id = w.sell(
        partner_sale(w.partner, ticket("3940000001"), ticket("3940000002", "10000")), OCT_1
    )
    [line] = w.lines()
    assert (line.sale_id, line.day, line.version_no) == (sale_id, OCT_1, 1)
    assert [t.doc_number for t in line.tickets] == ["3940000001", "3940000002"]
    assert [t.total_doc for t in line.tickets] == [Decimal("21368.00"), Decimal("10000.00")]
    assert line.fee == Decimal("300.00")
    assert line.cost_total == Decimal("31368.00")
    assert line.pb == Decimal("31968.00")  # 31 368 + 2 x 300


def test_the_lines_feed_the_balance(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    w.sell(partner_sale(w.partner, ticket("3940000002", "10000")), OCT_15)
    totals = partner_ledger.compute_period(Decimal("0.00"), w.lines(), (), ())
    assert totals.sales_total == Decimal("31968.00")


def test_the_range_is_inclusive_on_both_ends(w: World) -> None:
    first = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    last = w.sell(partner_sale(w.partner, ticket("3940000002")), OCT_15)
    w.sell(partner_sale(w.partner, ticket("3940000003")), OCT_16)
    w.sell(partner_sale(w.partner, ticket("3940000004")), date(2026, 9, 30))
    assert [line.sale_id for line in w.lines()] == [first, last]
    assert [line.day for line in w.lines(OCT_15, OCT_15)] == [OCT_15]


def test_lines_come_in_day_then_sale_order(w: World) -> None:
    later = w.sell(partner_sale(w.partner, ticket("3940000001")), date(2026, 10, 9))
    earlier = w.sell(partner_sale(w.partner, ticket("3940000002")), date(2026, 10, 2))
    same_day = w.sell(partner_sale(w.partner, ticket("3940000003")), date(2026, 10, 9))
    assert [line.sale_id for line in w.lines()] == [earlier, later, same_day]


def test_a_voided_sale_is_left_out(w: World) -> None:
    kept = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    voided = w.sell(partner_sale(w.partner, ticket("3940000002")), OCT_1)
    sales.void_sale(w.db, w.manager, voided, "saisie en double", now=NOW)
    w.db.commit()
    assert [line.sale_id for line in w.lines()] == [kept]


def test_an_edited_sale_is_read_at_its_current_version(w: World) -> None:
    sale_id = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    edited = partner_sale(w.partner, ticket("3940000001", "20000"), ticket("3940000002", "5000"))
    sales.edit_sale(w.db, w.manager, sale_id, edited, "coût corrigé", now=NOW)
    w.db.commit()
    [line] = w.lines()
    assert line.version_no == 2
    assert [t.doc_number for t in line.tickets] == ["3940000001", "3940000002"]
    assert line.pb == Decimal("25600.00")  # 25 000 + 2 x 300


def test_a_sale_moved_to_another_partner_leaves_the_first_one(w: World) -> None:
    sale_id = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    moved = partner_sale(w.second, ticket("3940000001"))
    sales.edit_sale(w.db, w.manager, sale_id, moved, "mauvais partenaire", now=NOW)
    w.db.commit()
    assert w.lines() == []
    assert [line.sale_id for line in w.lines(partner_id=w.second.id)] == [sale_id]


def test_a_sale_turned_direct_leaves_the_partner(w: World) -> None:
    sale_id = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    direct = SaleInput(
        ag_kind="direct", pb="24000", clt="Client Test", payment_method="cash", staff_sign=SIGN,
        tickets=(ticket("3940000001"),),
    )  # fmt: skip
    sales.edit_sale(w.db, w.manager, sale_id, direct, "vente directe", now=NOW)
    w.db.commit()
    assert w.lines() == []


def test_a_sale_moved_to_another_day_follows_its_day(w: World) -> None:
    sale_id = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    sales.move_sale_day(w.db, w.manager, sale_id, OCT_16, "trouvé dans le rapport du 16", now=NOW)
    w.db.commit()
    assert w.lines() == []
    [line] = w.lines(OCT_16, date(2026, 10, 31))
    assert (line.sale_id, line.day) == (sale_id, OCT_16)


def test_direct_sales_and_other_partners_are_left_out(w: World) -> None:
    own = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_1)
    w.sell(partner_sale(w.second, ticket("3940000002")), OCT_1)
    w.sell(
        SaleInput(
            ag_kind="direct", pb="24000", clt="Client Test", payment_method="cash", staff_sign=SIGN,
            tickets=(ticket("3940000003"),),
        ),
        OCT_1,
    )  # fmt: skip
    assert [line.sale_id for line in w.lines()] == [own]


def test_another_agency_is_never_read(w: World) -> None:
    w.sell(partner_sale(w.foreign, ticket("3940000001")), OCT_1, actor=w.other_manager)
    assert w.lines() == []
    # Its partner's id with this agency's id finds nothing either (no cross-tenant read).
    assert w.lines(partner_id=w.foreign.id) == []
    assert len(w.lines(agency_id=w.other_agency_id, partner_id=w.foreign.id)) == 1
    assert w.lines(agency_id=w.other_agency_id) == []


def test_a_reversed_range_is_refused(w: World) -> None:
    with pytest.raises(ValueError, match="range"):
        w.lines(OCT_15, OCT_1)
