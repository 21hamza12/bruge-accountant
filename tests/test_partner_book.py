"""app/partner_book.py: the partner account service of phase F, slice F2 (SQLite).

Sales are written through ``app.sales`` itself, so the account reads exactly what the sales
service stores. Report days are closed by setting their status (the close gate is tested
elsewhere). Names are made up ("Partenaire Test"); the numbers are BADI-style."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import partner_book, partners, sales
from app.db import make_engine, make_session_factory
from app.models import (
    AuditEvent,
    Partner,
    PartnerEntry,
    PartnerEntryDecision,
    PartnerPayment,
    PartnerPeriod,
    ReportDay,
    User,
)
from app.partner_book import (
    AccountConflict,
    AccountForbidden,
    AccountNotFound,
    PartnerAccountError,
)
from app.repository import create_agency
from app.sales import SaleInput, TicketInput
from app.tenancy import rebind_tenant

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # the 1-15 Oct period is over, 16-31 is not
OCT_1, OCT_3, OCT_15, OCT_16, OCT_20 = (
    date(2026, 10, 1),
    date(2026, 10, 3),
    date(2026, 10, 15),
    date(2026, 10, 16),
    date(2026, 10, 20),
)
SIGN = "0004SV"
KEYS = iter(f"cle-de-test-{n:06d}-abcdef" for n in range(1, 100_000))


def key() -> str:
    return next(KEYS)


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
        self.manager = self._user(self.agency_id, "+22212370001", "manager")
        self.manager2 = self._user(self.agency_id, "+22212370003", "manager")
        self.accountant = self._user(self.agency_id, "+22212370004", "accountant")
        self.staff = self._user(self.agency_id, "+22212370005", "staff")
        self.other_agency_id = create_agency(session, "agence-q", "Agence Q").id
        self.other_manager = self._user(self.other_agency_id, "+22212370002", "manager")
        self.partner = self._partner(self.manager, "Partenaire Test")
        self.second = self._partner(self.manager, "Partenaire Deux")
        self.foreign = self._partner(self.other_manager, "Partenaire Test")

    def _user(self, agency_id: int, phone: str, role: str) -> User:
        user = User(agency_id=agency_id, phone=phone, role=role)
        self.db.add(user)
        self.db.commit()
        return user

    def _partner(self, actor: User, name: str) -> Partner:
        partner = partners.create_partner(self.db, actor, name, now=NOW)
        partners.set_fee(self.db, partner, Decimal("300"), date(2026, 1, 1), actor, now=NOW)
        self.db.commit()
        return partner

    def sell(self, data: SaleInput, day: date) -> int:
        result = sales.create_sale(self.db, self.manager, data, day=day, now=NOW)
        self.db.commit()
        return result.sale.id

    def set_days(self, status: str, start: date = OCT_1, end: date = OCT_15) -> None:
        for row in self.db.scalars(
            select(ReportDay).where(
                ReportDay.agency_id == self.agency_id, ReportDay.day >= start, ReportDay.day <= end
            )
        ):
            row.status = status
            row.closed_at = NOW if status == "closed" else None
            row.closed_by_user_id = self.manager.id if status == "closed" else None
        self.db.commit()

    def line(self, kind: str = "ADM", amount: str = "1500", **kw: Any) -> PartnerEntry:
        actor = kw.pop("actor", self.accountant)
        partner_id = kw.pop("partner_id", self.partner.id)
        kw.setdefault("reason", "pénalité de la compagnie")
        kw.setdefault("request_key", key())
        kw.setdefault("entry_date", OCT_3.isoformat())
        written = partner_book.add_line(
            self.db, actor, partner_id, kind=kind, amount=amount, now=NOW, **kw
        )
        self.db.commit()
        row: PartnerEntry = written.row
        return row

    def pay(self, amount: str = "80000", **kw: Any) -> PartnerPayment:
        actor = kw.pop("actor", self.accountant)
        kw.setdefault("method", "bankily")
        kw.setdefault("received_by_user_id", self.accountant.id)
        kw.setdefault("request_key", key())
        kw.setdefault("paid_on", OCT_3.isoformat())
        written = partner_book.record_payment(
            self.db, actor, self.partner.id, amount=amount, now=NOW, **kw
        )
        self.db.commit()
        row: PartnerPayment = written.row
        return row

    def decide(self, entry: PartnerEntry, decision: str, actor: User, note: str = "") -> Any:
        written = partner_book.decide_line(
            self.db, actor, self.partner.id, entry.id, decision=decision, note=note, now=NOW
        )
        self.db.commit()
        return written

    def close(self, start: date = OCT_1, actor: User | None = None, **kw: Any) -> Any:
        kw.setdefault("request_key", key())
        written = partner_book.close_period(
            self.db, actor or self.accountant, self.partner.id, start, now=kw.pop("now", NOW), **kw
        )
        self.db.commit()
        return written

    def opening(self, amount: str, day: date = OCT_1) -> None:
        self.db.add(
            PartnerEntry(
                agency_id=self.agency_id,
                partner_id=self.partner.id,
                entry_date=day,
                kind="OPENING",
                amount=Decimal(amount),
                reason="solde du classeur au passage",
                source="manual",
                created_by_user_id=self.manager.id,
            )
        )
        self.db.commit()

    def amadeus_refund(self, amount: str = "-2505", day: date = OCT_3) -> PartnerEntry:
        row = PartnerEntry(
            agency_id=self.agency_id,
            partner_id=self.partner.id,
            entry_date=day,
            kind="RFND",
            amount=Decimal(amount),
            reason="remboursement vu dans le rapport",
            source="amadeus",
            doc_number="3940000099",
            created_by_user_id=self.manager.id,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def account(self, today: date | None = None) -> partner_book.Account:
        moment = today or NOW.date()
        return partner_book.get_account(self.db, self.manager, self.partner.id, moment)

    def entries(self, kind: str | None = None) -> list[PartnerEntry]:
        query = select(PartnerEntry).where(PartnerEntry.agency_id == self.agency_id)
        if kind is not None:
            query = query.where(PartnerEntry.kind == kind)
        return list(self.db.scalars(query.order_by(PartnerEntry.id)))

    def events(self, event_type: str) -> list[AuditEvent]:
        return list(
            self.db.scalars(
                select(AuditEvent)
                .where(AuditEvent.event_type == event_type)
                .order_by(AuditEvent.id)
            )
        )


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


def badi_period(w: World) -> int:
    """The BADI-style first half of October: an opening of 50 000, a sale of two tickets
    (21 368 + 10 000, fee 300 each), an ADM of 1 500, a refund of 2 505, an adjustment of +200
    approved and one of -999 rejected, and a payment of 80 000. Every day then closed."""
    w.opening("50000")
    sale_id = w.sell(
        partner_sale(w.partner, ticket("3940000001"), ticket("3940000002", "10000")), OCT_3
    )
    w.line("ADM", "1500")
    w.line("RFND", "2505", reason="remboursement du billet 3940000005")
    plus = w.line("AJUSTEMENT", "200", direction="plus", reason="frais oubliés le 3")
    minus = w.line("AJUSTEMENT", "999", direction="moins", reason="geste commercial")
    w.decide(plus, "approve", w.manager)
    w.decide(minus, "reject", w.manager, note="pas de geste prévu")
    w.pay("80000")
    w.set_days("closed")
    return sale_id


# --- adding a line --------------------------------------------------------------------------


def test_a_line_takes_its_sign_from_its_kind(w: World) -> None:
    assert w.line("ADM", "1 500").amount == Decimal("1500.00")
    assert w.line("RFND", "2505,50").amount == Decimal("-2505.50")
    assert w.line("CHANGEMENT", "500").amount == Decimal("500.00")
    assert w.line("AJUSTEMENT", "200", direction="plus").amount == Decimal("200.00")
    assert w.line("AJUSTEMENT", "200", direction="moins").amount == Decimal("-200.00")
    [event, *_] = w.events("partner_entry_added")
    assert event.details == {
        "partner_id": w.partner.id,
        "entry_id": w.entries()[0].id,
        "kind": "ADM",
        "amount": "1500.00",
        "entry_date": OCT_3.isoformat(),
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "OPENING"}, partner_book.BAD_KIND),
        ({"kind": "CORRECTION"}, partner_book.BAD_KIND),
        ({"amount": "0"}, partner_book.ZERO_AMOUNT),
        ({"amount": "-5"}, "négatif"),
        ({"amount": "1.5e3"}, "pas valide"),
        ({"kind": "AJUSTEMENT"}, partner_book.BAD_DIRECTION),
        ({"reason": "     "}, "motif"),
        ({"reason": "a    b"}, "motif"),  # padded to five characters: collapsed to "a b"
        ({"entry_date": "2026-10-28"}, partner_book.FUTURE_DATE),
        ({"entry_date": "28/10/2026"}, partner_book.BAD_DATE),
        ({"request_key": "court"}, partner_book.BAD_REQUEST_KEY),
        ({"request_key": ""}, partner_book.BAD_REQUEST_KEY),
    ],
)
def test_a_bad_line_is_refused_in_french(w: World, kwargs: dict[str, str], message: str) -> None:
    kind = kwargs.pop("kind", "ADM")
    amount = kwargs.pop("amount", "1500")
    with pytest.raises(PartnerAccountError, match=re.escape(message)):
        w.line(kind, amount, **kwargs)
    w.db.rollback()
    assert w.entries() == []


def test_staff_may_not_add_a_line_and_the_refusal_is_audited(w: World) -> None:
    with pytest.raises(AccountForbidden):
        w.line(actor=w.staff)
    w.db.rollback()
    assert w.entries() == []
    [event] = w.events("permission_denied")
    assert event.actor_user_id == w.staff.id and event.details == {"action": "add_partner_line"}


def test_an_inactive_accountant_may_not_write(w: World) -> None:
    w.accountant.is_active = False
    w.db.commit()
    with pytest.raises(AccountForbidden):
        w.line()
    with pytest.raises(AccountForbidden):
        w.pay()


def test_another_agencys_partner_is_not_found(w: World) -> None:
    with pytest.raises(AccountNotFound):
        w.line(partner_id=w.foreign.id)
    with pytest.raises(AccountNotFound):
        partner_book.get_account(w.db, w.manager, w.foreign.id, NOW.date())
    with pytest.raises(AccountNotFound):
        partner_book.record_payment(
            w.db, w.other_manager, w.partner.id, amount="10", method="cash",
            received_by_user_id=w.other_manager.id, request_key=key(), now=NOW,
        )  # fmt: skip
    with pytest.raises(AccountNotFound):
        partner_book.close_period(
            w.db, w.other_manager, w.partner.id, OCT_1, request_key=key(), now=NOW
        )


def test_the_same_request_key_writes_one_line(w: World) -> None:
    same = key()
    first = w.line(request_key=same)
    again = partner_book.add_line(
        w.db, w.accountant, w.partner.id, kind="ADM", amount="9999",
        reason="double clic du formulaire", request_key=same, now=NOW,
    )  # fmt: skip
    assert again.created is False and again.row.id == first.id
    assert len(w.entries()) == 1 and len(w.events("partner_entry_added")) == 1


def test_a_request_key_of_another_partner_is_refused(w: World) -> None:
    same = key()
    w.line(request_key=same)
    with pytest.raises(PartnerAccountError, match="autre compte"):
        w.line(request_key=same, partner_id=w.second.id)


# --- deciding a line: four eyes ---------------------------------------------------------------


def test_only_a_manager_who_did_not_enter_it_decides_an_adjustment(w: World) -> None:
    by_accountant = w.line("AJUSTEMENT", "200", direction="plus")
    with pytest.raises(AccountForbidden, match="gérant"):
        w.decide(by_accountant, "approve", w.accountant)
    by_manager = w.line("AJUSTEMENT", "300", direction="plus", actor=w.manager)
    with pytest.raises(AccountForbidden, match="autre gérant"):
        w.decide(by_manager, "approve", w.manager)
    actions = [e.details["action"] for e in w.events("permission_denied")]
    assert actions == ["decide_partner_adjustment", "decide_own_partner_adjustment"]
    w.decide(by_accountant, "approve", w.manager)
    w.decide(by_manager, "approve", w.manager2)
    decided = w.db.scalars(select(PartnerEntryDecision).order_by(PartnerEntryDecision.id)).all()
    assert [(d.entry_id, d.decided_by_user_id) for d in decided] == [
        (by_accountant.id, w.manager.id),
        (by_manager.id, w.manager2.id),
    ]
    assert w.events("partner_entry_decided")[0].details["decision"] == "approve"


def test_a_decision_is_taken_once(w: World) -> None:
    line = w.line("AJUSTEMENT", "200", direction="plus")
    first = w.decide(line, "approve", w.manager)
    assert first.created
    again = w.decide(line, "approve", w.manager)  # the same click twice
    assert again.created is False and again.row.id == first.row.id
    with pytest.raises(AccountConflict, match="déjà été traitée"):
        w.decide(line, "reject", w.manager2, note="pas d'accord du tout")


def test_a_rejection_needs_a_reason_and_a_decision_must_fit(w: World) -> None:
    line = w.line("AJUSTEMENT", "200", direction="plus")
    with pytest.raises(PartnerAccountError, match="motif"):
        w.decide(line, "reject", w.manager, note="   ")
    with pytest.raises(PartnerAccountError, match="ne convient pas"):
        w.decide(line, "confirm", w.manager)
    manual = w.line("RFND", "100")
    with pytest.raises(PartnerAccountError, match="aucune décision"):
        w.decide(manual, "confirm", w.manager)


@pytest.mark.parametrize("who", ["accountant", "manager"])
def test_the_accountant_or_the_manager_confirms_an_amadeus_refund(w: World, who: str) -> None:
    refund = w.amadeus_refund()
    assert w.account().current is not None
    assert [e.pending for e in w.account().periods[0].entries] == [True]
    w.decide(refund, "confirm", getattr(w, who))
    [entry] = w.account().periods[0].entries
    assert entry.counted and not entry.pending
    assert w.account().periods[0].totals is not None
    assert w.account().periods[0].totals.entries_total == Decimal("-2505.00")  # type: ignore[union-attr]


def test_staff_may_not_decide(w: World) -> None:
    refund = w.amadeus_refund()
    with pytest.raises(AccountForbidden):
        w.decide(refund, "confirm", w.staff)


# --- payments ---------------------------------------------------------------------------------


def test_a_payment_is_recorded_by_the_accountant_with_its_receiver(w: World) -> None:
    paid = w.pay("80 000", reference=" CHQ 1234 ", received_by_user_id=w.manager.id)
    assert (paid.amount, paid.method, paid.reference) == (
        Decimal("80000.00"),
        "bankily",
        "CHQ 1234",
    )
    assert (paid.received_by_user_id, paid.recorded_by_user_id) == (w.manager.id, w.accountant.id)
    [event] = w.events("partner_payment_recorded")
    assert event.details["amount"] == "80000.00" and "reference" not in event.details


def test_staff_may_not_record_a_payment(w: World) -> None:
    with pytest.raises(AccountForbidden):
        w.pay(actor=w.staff)
    w.db.rollback()
    assert list(w.db.scalars(select(PartnerPayment))) == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"method": "credit"}, partner_book.BAD_METHOD),
        ({"method": "bitcoin"}, partner_book.BAD_METHOD),
        ({"amount": "0"}, partner_book.ZERO_AMOUNT),
        ({"received_by_user_id": None}, partner_book.BAD_RECEIVER),
        ({"reference": "x" * 65}, "64"),
        ({"paid_on": "2026-11-01"}, partner_book.FUTURE_DATE),
    ],
)
def test_a_bad_payment_is_refused(w: World, kwargs: dict[str, Any], message: str) -> None:
    amount = kwargs.pop("amount", "1000")
    with pytest.raises(PartnerAccountError, match=re.escape(message)):
        w.pay(amount, **kwargs)


def test_the_receiver_is_an_active_user_of_the_agency(w: World) -> None:
    with pytest.raises(PartnerAccountError, match="utilisateurs actifs"):
        w.pay(received_by_user_id=w.other_manager.id)
    w.staff.is_active = False
    w.db.commit()
    with pytest.raises(PartnerAccountError, match="utilisateurs actifs"):
        w.pay(received_by_user_id=w.staff.id)


def test_the_same_payment_form_posted_twice_records_once(w: World) -> None:
    same = key()
    first = w.pay(request_key=same)
    second = w.pay(request_key=same)
    assert first.id == second.id
    assert len(list(w.db.scalars(select(PartnerPayment)))) == 1


# --- closing ----------------------------------------------------------------------------------


def test_closing_freezes_the_exact_totals_and_a_snapshot(w: World) -> None:
    sale_id = badi_period(w)
    closed = w.close(actor=w.accountant)
    row: PartnerPeriod = closed.row
    assert (row.status, row.start_date, row.end_date) == ("closed", OCT_1, OCT_15)
    assert row.opening_balance == Decimal("50000.00")
    assert row.sales_total == Decimal("31968.00")  # 21 368 + 10 000 + 2 x 300
    assert row.entries_total == Decimal("-805.00")  # 1 500 - 2 505 + 200 (the -999 is rejected)
    assert row.payments_total == Decimal("80000.00")
    assert row.closing_balance == Decimal("1163.00")
    assert row.closed_by_user_id == w.accountant.id and row.closed_at is not None
    snapshot = row.snapshot or {}
    assert snapshot["closing"] == "1163.00"
    assert [s["sale_id"] for s in snapshot["sales"]] == [sale_id]
    assert [t["doc_number"] for t in snapshot["sales"][0]["tickets"]] == [
        "3940000001",
        "3940000002",
    ]
    assert [(e["kind"], e["counted"]) for e in snapshot["entries"]] == [
        ("ADM", True), ("RFND", True), ("AJUSTEMENT", True), ("AJUSTEMENT", False),
    ]  # fmt: skip
    assert [p["amount"] for p in snapshot["payments"]] == ["80000.00"]
    [event] = w.events("partner_period_closed")
    assert event.details["closing"] == "1163.00" and event.details["period_id"] == row.id
    # The next period opens on the closed balance.
    acc = w.account()
    assert [p.closed for p in acc.periods] == [True, False]
    assert acc.periods[1].opening == Decimal("1163.00") and acc.balance == Decimal("1163.00")


def test_a_closed_period_is_never_recomputed(w: World) -> None:
    sale_id = badi_period(w)
    w.close()
    w.set_days("open")
    edited = partner_sale(w.partner, ticket("3940000001", "30000"), ticket("3940000002", "10000"))
    sales.edit_sale(w.db, w.manager, sale_id, edited, "coût corrigé après envoi", now=NOW)
    w.db.commit()
    first = w.account().periods[0]
    assert first.closed and first.closing == Decimal("1163.00")
    assert first.row is not None and first.row.sales_total == Decimal("31968.00")


def test_closing_twice_with_the_same_form_returns_the_closed_period(w: World) -> None:
    badi_period(w)
    same = key()
    first = w.close(request_key=same)
    again = w.close(request_key=same)
    assert again.created is False and again.row.id == first.row.id
    with pytest.raises(AccountConflict, match="déjà clôturée"):
        w.close()
    assert len(w.events("partner_period_closed")) == 1


def test_closing_is_refused_while_a_day_is_not_closed(w: World) -> None:
    badi_period(w)
    w.set_days("approved", OCT_3, OCT_3)
    with pytest.raises(AccountConflict, match="3 octobre 2026"):
        w.close()


def test_closing_is_refused_while_a_line_waits(w: World) -> None:
    badi_period(w)
    w.line("AJUSTEMENT", "50", direction="plus", reason="oubli du 5 octobre")
    with pytest.raises(AccountConflict, match="attendent"):
        w.close()
    w.db.rollback()
    w.amadeus_refund()
    with pytest.raises(AccountConflict, match="attendent"):
        w.close()


def test_closing_is_refused_before_the_period_is_over(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_20)
    with pytest.raises(AccountConflict, match="pas terminée"):
        w.close(OCT_16)


def test_an_earlier_open_period_must_be_closed_first(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), date(2026, 9, 20))
    w.sell(partner_sale(w.partner, ticket("3940000002")), OCT_3)
    w.set_days("closed", date(2026, 9, 1), OCT_15)
    with pytest.raises(AccountConflict, match="précédente"):
        w.close(OCT_1)
    w.close(date(2026, 9, 16))
    w.close(OCT_1)
    assert [p.closed for p in w.account().periods] == [True, True, False]


def test_a_period_that_does_not_exist_is_not_found(w: World) -> None:
    badi_period(w)
    with pytest.raises(AccountNotFound):
        w.close(date(2026, 10, 2))  # not the start of a half-month
    with pytest.raises(AccountNotFound):
        w.close(date(2026, 8, 1))  # before the partner's first period


def test_staff_may_not_close(w: World) -> None:
    badi_period(w)
    with pytest.raises(AccountForbidden):
        w.close(actor=w.staff)
    w.db.rollback()
    assert list(w.db.scalars(select(PartnerPeriod))) == []


def test_a_closed_period_refuses_backdated_lines_and_payments_in_french(w: World) -> None:
    badi_period(w)
    w.close()
    with pytest.raises(PartnerAccountError) as refused:
        w.line(entry_date=OCT_15.isoformat())
    assert str(refused.value).startswith("Cette date tombe dans une période déjà clôturée")
    assert "15 octobre 2026" in str(refused.value)
    with pytest.raises(PartnerAccountError, match="déjà clôturée"):
        w.line(entry_date=date(2026, 9, 1).isoformat())  # a half-month that never had a row
    with pytest.raises(PartnerAccountError, match="déjà clôturée"):
        w.pay(paid_on=OCT_3.isoformat())
    w.line(entry_date=OCT_16.isoformat(), reason="ADM du 3 octobre reçu en retard")


def test_nothing_is_dated_before_the_opening_balance(w: World) -> None:
    w.opening("50000", date(2026, 10, 5))
    with pytest.raises(PartnerAccountError, match="solde d'ouverture"):
        w.line(entry_date=date(2026, 9, 30).isoformat())
    w.line(entry_date=OCT_1.isoformat())  # the opening's half-month is the first period


# --- corrections after a close ----------------------------------------------------------------


def _corrections(w: World) -> list[tuple[int, Decimal, int | None, str]]:
    return [(e.partner_id, e.amount, e.sale_id, e.source) for e in w.entries("CORRECTION")]


def _closed_with_a_sale(w: World) -> int:
    sale_id = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)  # P.B 21 668
    w.set_days("closed")
    w.close()
    w.set_days("open")  # the day is reopened to correct the sale
    return sale_id


def test_an_edit_after_the_close_writes_the_difference_into_the_open_period(w: World) -> None:
    sale_id = _closed_with_a_sale(w)
    edited = partner_sale(w.partner, ticket("3940000001", "22000"))
    sales.edit_sale(w.db, w.manager, sale_id, edited, "coût corrigé après envoi", now=NOW)
    w.db.commit()
    assert _corrections(w) == [(w.partner.id, Decimal("632.00"), sale_id, "system")]
    [line] = w.entries("CORRECTION")
    assert line.entry_date == NOW.date()
    assert line.reason.startswith(f"Vente #{sale_id} modifiée après la clôture")
    acc = w.account()
    assert acc.periods[0].closing == Decimal("21668.00")  # the statement sent stays true
    assert acc.balance == Decimal("22300.00")  # 21 668 + 632
    [event] = w.events("partner_correction_added")
    assert event.details == {
        "partner_id": w.partner.id,
        "entry_id": line.id,
        "sale_id": sale_id,
        "amount": "632.00",
        "closed_start": OCT_1.isoformat(),
    }


def test_a_void_after_the_close_takes_the_sale_back(w: World) -> None:
    sale_id = _closed_with_a_sale(w)
    sales.void_sale(w.db, w.manager, sale_id, "vente en double", now=NOW)
    w.db.commit()
    assert _corrections(w) == [(w.partner.id, Decimal("-21668.00"), sale_id, "system")]
    assert w.account().balance == Decimal("0.00")


def test_a_move_out_of_the_closed_period_counts_once(w: World) -> None:
    sale_id = _closed_with_a_sale(w)
    sales.move_sale_day(w.db, w.manager, sale_id, OCT_20, "trouvé dans le rapport du 20", now=NOW)
    w.db.commit()
    assert _corrections(w) == [(w.partner.id, Decimal("-21668.00"), sale_id, "system")]
    acc = w.account()
    assert [line.sale_id for line in acc.periods[1].sales] == [sale_id]
    assert acc.balance == Decimal("21668.00")  # counted once: in the open period, live


def test_a_move_into_the_closed_period_is_a_correction(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)
    late = w.sell(partner_sale(w.partner, ticket("3940000002", "1000")), OCT_20)
    w.set_days("closed")
    w.close()
    w.set_days("open")
    sales.move_sale_day(w.db, w.manager, late, OCT_3, "émis le 3 en réalité", now=NOW)
    w.db.commit()
    assert _corrections(w) == [(w.partner.id, Decimal("1300.00"), late, "system")]
    assert w.account().balance == Decimal("22968.00")


def test_a_change_of_partner_after_the_close_corrects_the_first_partner(w: World) -> None:
    sale_id = _closed_with_a_sale(w)
    moved = partner_sale(w.second, ticket("3940000001"))
    sales.edit_sale(w.db, w.manager, sale_id, moved, "mauvais partenaire", now=NOW)
    w.db.commit()
    # The second partner has no closed period: it reads the sale live.
    assert _corrections(w) == [(w.partner.id, Decimal("-21668.00"), sale_id, "system")]
    second = partner_book.get_account(w.db, w.manager, w.second.id, NOW.date())
    assert second.balance == Decimal("21668.00")


def test_a_sale_added_late_in_a_closed_period_is_a_correction(w: World) -> None:
    _closed_with_a_sale(w)
    late = w.sell(partner_sale(w.partner, ticket("3940000002", "1000")), OCT_3)
    assert _corrections(w) == [(w.partner.id, Decimal("1300.00"), late, "system")]


def test_no_correction_while_the_period_is_open(w: World) -> None:
    sale_id = w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_20)
    sales.void_sale(w.db, w.manager, sale_id, "vente en double", now=NOW)
    w.db.commit()
    assert _corrections(w) == []


def test_a_direct_sale_is_never_corrected(w: World) -> None:
    _closed_with_a_sale(w)
    direct = SaleInput(
        ag_kind="direct", pb="24000", clt="Client Test", payment_method="cash", staff_sign=SIGN,
        tickets=(ticket("3940000003"),),
    )  # fmt: skip
    w.sell(direct, OCT_3)
    assert _corrections(w) == []


def test_a_correction_only_points_at_a_sale_of_its_partner(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    sale_id = _closed_with_a_sale(w)
    # As if the second partner's day were in a closed period too: the sale never named that
    # partner, so no correction may point at it. Refused, nothing written.
    monkeypatch.setattr(partner_book, "_covered", lambda *_a: (OCT_1, OCT_15))
    share = partner_book.SaleShare(w.second.id, OCT_3, Decimal("100"))
    with pytest.raises(RuntimeError, match="not a sale of partner"):
        partner_book.correct_after_close(
            w.db, w.manager, sale_id, None, share, action=partner_book.ACTION_EDITED, now=NOW
        )
    w.db.rollback()
    assert _corrections(w) == []


# --- the list of every partner ----------------------------------------------------------------


def test_the_list_puts_overdue_accounts_first_with_the_oldest_unpaid_amount(w: World) -> None:
    w.sell(partner_sale(w.second, ticket("3940000009", "1000")), OCT_20)  # open period only
    w.sell(partner_sale(w.partner, ticket("3940000001")), date(2026, 9, 3))
    w.set_days("closed", date(2026, 9, 1), OCT_15)
    w.close(date(2026, 9, 1))
    w.close(date(2026, 9, 16))
    w.close(OCT_1)
    w.pay("1000", paid_on=OCT_20.isoformat())
    rows = partner_book.account_rows(w.db, w.manager, NOW.date())
    assert [r.partner.id for r in rows] == [w.partner.id, w.second.id]
    first = rows[0]
    assert first.balance == Decimal("20668.00")
    assert first.oldest is not None
    assert (first.oldest.start, first.oldest.amount) == (date(2026, 9, 1), Decimal("20668.00"))
    assert first.oldest.due == date(2026, 9, 30) and first.oldest.level == "overdue"
    assert first.oldest.age_days == 42
    assert rows[1].oldest is None and rows[1].open_total == Decimal("1300.00")


def test_staff_get_no_list(w: World) -> None:
    with pytest.raises(AccountForbidden):
        partner_book.account_rows(w.db, w.staff, NOW.date())


# --- the database's own refusal -----------------------------------------------------------------


@pytest.fixture
def migrated(db_url: str) -> Iterator[Session]:
    """A database built by the migrations, so migration 0017's triggers are there."""
    from tests.conftest import migrate

    migrate(db_url)
    engine = make_engine(db_url)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def test_a_refusal_by_the_database_is_a_french_message_not_a_crash(
    migrated: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(migrated)
    rebind_tenant(migrated, w.agency_id)
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)
    w.set_days("closed")
    w.close()
    # As if the period had closed between the application's check and the insert.
    monkeypatch.setattr(partner_book, "_check_date", lambda *a, **k: None)
    with pytest.raises(AccountConflict) as refused:
        w.line(entry_date=OCT_3.isoformat())
    assert str(refused.value) == partner_book.PERIOD_JUST_CLOSED
    migrated.rollback()
    with pytest.raises(AccountConflict):
        w.pay(paid_on=OCT_3.isoformat())
