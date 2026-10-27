"""app/sales.py: sales of one or more tickets with one price, their versions and edit rights.

The pure part (``validate``) is tested on its own; the rest runs on SQLite through the service
functions. The race on a duplicate ticket number needs Postgres: tests/test_sales_concurrency.py.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.orm import Session

from app import days, partners, sales
from app.formatting import money_fr
from app.models import (
    AuditEvent,
    Sale,
    SaleTicket,
    SaleVersion,
    StaffSignLink,
    User,
)
from app.repository import create_agency
from app.sales import (
    CarriedFee,
    DuplicateTicket,
    FieldError,
    InvalidSale,
    PartnerContext,
    SaleError,
    SaleForbidden,
    SaleInput,
    SaleNotFound,
    TicketInput,
    ValidatedSale,
    ValidationContext,
)

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 22)
YESTERDAY = date(2026, 10, 21)
D1, D2, D3, D4 = "3940000001", "3940000002", "3940000003", "3940000004"


def ticket(doc: str = D1, cost: Any = "21368", **kw: Any) -> TicketInput:
    fields: dict[str, Any] = {
        "doc_number": doc,
        "airline": "199",
        "total_doc": cost,
        "pax_name": "SYNTH01/PAX",
        "pax_type": "ADT",
        "rloc": "ABC123",
        "trnc": "TKTT",
    }
    fields.update(kw)
    return TicketInput(**fields)


def direct(*tickets: TicketInput, pb: Any = "24000", **kw: Any) -> SaleInput:
    fields: dict[str, Any] = {
        "ag_kind": "direct",
        "pb": pb,
        "clt": "Client Synth",
        "payment_method": "cash",
    }
    fields.update(kw)
    return SaleInput(tickets=tickets or (ticket(),), **fields)


def partner_sale(partner_id: int, *tickets: TicketInput, **kw: Any) -> SaleInput:
    fields: dict[str, Any] = {"ag_kind": "partner", "partner_id": partner_id}
    fields.update({"payment_method": "credit", **kw})
    return SaleInput(tickets=tickets or (ticket(),), **fields)


def ctx(fee: str | None = "500", *, active: bool = True, **kw: Any) -> ValidationContext:
    partner = PartnerContext(
        partner_id=7,
        name="BADI",
        is_active=active,
        fee=None if fee is None else Decimal(fee),
        fee_id=None if fee is None else 70,
        carried=kw.pop("carried", None),
    )
    return ValidationContext(day=TODAY, partner=partner, **kw)


def ok(data: SaleInput, context: ValidationContext | None = None) -> ValidatedSale:
    result = sales.validate(data, context or ValidationContext(day=TODAY))
    assert isinstance(result, ValidatedSale), result
    return result


def errors(data: SaleInput, context: ValidationContext | None = None) -> dict[str, str]:
    result = sales.validate(data, context or ctx())
    assert isinstance(result, list), result
    assert all(isinstance(e, FieldError) for e in result)
    return {e.field: e.message for e in result}


# --- validate: partner price ----------------------------------------------------------------


def test_partner_pb_is_cost_plus_the_fee_of_the_day() -> None:
    v = ok(partner_sale(7, ticket(cost="21368")), ctx("500"))
    assert v.pb == Decimal("21868.00")
    assert (v.fee_applied, v.partner_fee_id, v.fee_override_reason) == (Decimal("500.00"), 70, None)
    assert v.ag_label == "BADI"
    assert v.cost_total == Decimal("21368.00")


def test_a_two_ticket_partner_sale_is_the_sum_of_costs_plus_the_fee_twice() -> None:
    v = ok(partner_sale(7, ticket(D1, "21368"), ticket(D2, "10000.50", airline="TU")), ctx("500"))
    assert v.cost_total == Decimal("31368.50")
    assert v.pb == Decimal("32368.50")  # 31368.50 + 2 x 500
    assert len(v.tickets) == 2


def test_a_typed_price_never_changes_a_partner_price() -> None:
    v = ok(partner_sale(7, ticket(cost="1000"), pb="1"), ctx("300"))
    assert v.pb == Decimal("1300.00")


def test_a_zero_fee_is_a_fee() -> None:
    v = ok(partner_sale(7, ticket(cost="1000")), ctx("0"))
    assert v.pb == Decimal("1000.00") and v.fee_applied == Decimal("0.00")


def test_an_override_fee_needs_a_reason_and_replaces_the_fee_per_ticket() -> None:
    data = partner_sale(7, ticket(D1, "1000"), ticket(D2, "2000"), fee_override="150")
    assert "fee_override_reason" in errors(data, ctx("500"))
    assert "fee_override_reason" in errors(replace(data, fee_override_reason="  abc "), ctx("500"))
    v = ok(replace(data, fee_override_reason="  client fidèle depuis 2019 "), ctx("500"))
    assert v.pb == Decimal("3300.00")  # 3000 + 2 x 150
    assert (v.fee_applied, v.partner_fee_id) == (Decimal("150.00"), None)
    assert v.fee_override_reason == "client fidèle depuis 2019"


def test_an_override_works_when_the_partner_has_no_fee_at_all() -> None:
    data = partner_sale(
        7, ticket(cost="1000"), fee_override="100", fee_override_reason="accord oral"
    )
    assert ok(data, ctx(None)).pb == Decimal("1100.00")


def test_an_invalid_override_amount_is_refused() -> None:
    for bad in ("-1", "abc", 1.5, "1,500"):
        data = partner_sale(7, fee_override=bad, fee_override_reason="une raison")
        assert "fee_override" in errors(data, ctx("500")), bad


def test_a_partner_without_a_fee_is_refused_and_named() -> None:
    found = errors(partner_sale(7), ctx(None))
    assert set(found) == {"partner_id"}
    assert "BADI" in found["partner_id"]


def test_an_inactive_or_unknown_partner_is_refused() -> None:
    assert "partner_id" in errors(partner_sale(7), ctx("500", active=False))
    assert "partner_id" in errors(partner_sale(7), ValidationContext(day=TODAY, partner=None))
    assert "partner_id" in errors(partner_sale(None), ctx("500"))  # type: ignore[arg-type]


def test_a_direct_sale_cannot_name_a_partner() -> None:
    assert "partner_id" in errors(direct(partner_id=7), ctx())


def test_the_fee_carried_from_the_previous_version_is_kept_on_an_edit() -> None:
    carried = CarriedFee(fee=Decimal("300"), fee_id=None, override_reason="accord du gérant")
    v = ok(partner_sale(7, ticket(cost="2000")), ctx("500", carried=carried))
    assert v.pb == Decimal("2300.00")
    assert v.fee_override_reason == "accord du gérant"
    reset = ok(
        replace(partner_sale(7, ticket(cost="2000")), reset_fee=True), ctx("500", carried=carried)
    )
    assert reset.pb == Decimal("2500.00") and reset.fee_override_reason is None


# --- validate: direct price ----------------------------------------------------------------


def test_a_direct_sale_takes_the_typed_price() -> None:
    v = ok(direct(pb="24 000,50"))
    assert v.pb == Decimal("24000.50")
    assert (v.ag_kind, v.ag_label, v.partner_id) == ("direct", "BRUGE", None)
    assert v.fee_applied is None and v.partner_fee_id is None


def test_a_direct_sale_needs_a_client_and_a_price() -> None:
    assert set(errors(direct(clt=None))) == {"clt"}
    assert set(errors(direct(clt="   "))) == {"clt"}
    assert set(errors(direct(pb=None))) == {"pb"}
    assert set(errors(direct(pb=""))) == {"pb"}
    assert set(errors(direct(clt=None, pb=None))) == {"clt", "pb"}


@pytest.mark.parametrize("bad", ["-1", "abc", "1,500", "1.234", 1.5, True, "1e3", "٣٠٠"])
def test_a_bad_typed_price_is_refused(bad: Any) -> None:
    assert "pb" in errors(direct(pb=bad))


def test_a_price_of_zero_is_allowed() -> None:
    assert ok(direct(pb="0")).pb == Decimal("0.00")


def test_a_price_below_the_cost_is_a_warning_not_an_error() -> None:
    v = ok(direct(ticket(cost="21368"), pb="20000"))
    assert v.pb == Decimal("20000.00")
    assert len(v.warnings) == 1
    assert money_fr(Decimal("20000")) in v.warnings[0]
    assert money_fr(Decimal("21368")) in v.warnings[0]
    assert ok(direct(ticket(cost="21368"), pb="21368")).warnings == ()
    assert ok(direct(ticket(cost="21368"), pb="21369")).warnings == ()


def test_the_warning_compares_with_the_cost_of_all_the_tickets() -> None:
    two = direct(ticket(D1, "10000"), ticket(D2, "10000"), pb="15000")
    assert len(ok(two).warnings) == 1
    assert ok(replace(two, pb="20000")).warnings == ()


def test_amounts_are_decimals_and_floats_are_refused_everywhere() -> None:
    v = ok(direct(ticket(cost=Decimal("100.10")), ticket(D2, cost=200), pb=Decimal("300.30")))
    assert v.cost_total == Decimal("300.10")
    assert all(isinstance(t.total_doc, Decimal) for t in v.tickets)
    assert isinstance(v.pb, Decimal) and isinstance(v.cost_total, Decimal)
    assert "tickets[0].total_doc" in errors(direct(ticket(cost=1000.5)))
    assert "pb" in errors(direct(pb=24000.0))


# --- validate: tickets ----------------------------------------------------------------------


def test_a_ticket_may_cost_zero_as_the_airline_495_ones_do() -> None:
    v = ok(direct(ticket(cost="0", airline="495"), pb="5000"))
    assert v.cost_total == Decimal("0.00") and v.warnings == ()


def test_a_negative_cost_is_refused() -> None:
    assert "tickets[0].total_doc" in errors(direct(ticket(cost="-5")))


@pytest.mark.parametrize(
    "doc",
    [
        "",
        "1234567",
        "123456789",  # 9
        "12345678901",  # 11
        "123456789012",  # 12
        "12345678901234",  # 14
        "1234567890123456",
        "39400000AB",
        "3940 0000x1",
        "٣٩٤٠٠٠٠٠٠١",
        None,
    ],
)
def test_a_bad_ticket_number_is_refused(doc: Any) -> None:
    found = errors(direct(ticket(doc=doc)))
    assert "tickets[0].doc_number" in found
    assert "10 chiffres" in found["tickets[0].doc_number"]


def test_ticket_numbers_may_be_typed_with_spaces_dashes_or_slashes() -> None:
    assert ok(direct(ticket(doc=" 394-0000 001 "))).tickets[0].doc_number == D1
    assert ok(direct(ticket(doc="394/0000/001"))).tickets[0].doc_number == D1


def test_the_airline_prefix_is_cut_off_and_must_match_the_airline() -> None:
    for typed in ("495-3940281465", "4953940281465", "495 3940281465", "495/3940281465"):
        v = ok(direct(ticket(doc=typed, airline="495")))
        assert v.tickets[0].doc_number == "3940281465", typed
    assert ok(direct(ticket(doc="3940281465", airline="495"))).tickets[0].doc_number == "3940281465"
    found = errors(direct(ticket(doc="495-3940281465", airline="199")))
    assert found["tickets[0].doc_number"] == "Le préfixe ne correspond pas à la compagnie (A/L)."
    # an airline that is not a 3-digit code cannot match a prefix either
    assert (
        "préfixe"
        in errors(direct(ticket(doc="4953940281465", airline="TU")))["tickets[0].doc_number"]
    )


def test_the_prefixed_and_the_plain_form_of_a_number_are_the_same_ticket_in_a_sale() -> None:
    both = (ticket("3940281465", airline="495"), ticket("495-3940281465", airline="495"))
    found = errors(direct(*both))
    assert "tickets[1].doc_number" in found


def test_the_same_number_twice_in_one_sale_is_refused() -> None:
    found = errors(direct(ticket(D1), ticket(D1)))
    assert "tickets[1].doc_number" in found


def test_a_sale_needs_at_least_one_ticket_and_at_most_the_cap() -> None:
    assert "tickets" in errors(SaleInput(tickets=(), ag_kind="direct", pb="1", clt="C"))
    many = [ticket(f"{3940000000 + n}") for n in range(sales.MAX_TICKETS_PER_SALE + 1)]
    assert "tickets" in errors(direct(*many))
    assert ok(direct(*many[:-1], pb="999999")).cost_total == Decimal("21368") * len(many[:-1])


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"trnc": "XXXX"}, "trnc"),
        ({"trnc": ""}, "trnc"),
        ({"pax_type": "ADULT"}, "pax_type"),
        ({"pax_type": ""}, "pax_type"),
        ({"pax_name": "   "}, "pax_name"),
        ({"pax_name": "A" * 200}, "pax_name"),
        ({"airline": ""}, "airline"),
        ({"airline": "TOOLONGCODE"}, "airline"),
        ({"rloc": ""}, "rloc"),
        ({"rloc": "AB"}, "rloc"),
        ({"rloc": "ABC 123 456 789 012"}, "rloc"),
    ],
)
def test_each_ticket_field_is_checked(kw: dict[str, Any], field: str) -> None:
    assert f"tickets[0].{field}" in errors(direct(ticket(**kw)))


def test_the_errors_name_the_ticket_that_has_them() -> None:
    found = errors(direct(ticket(D1), ticket(D2, trnc="NOPE"), ticket(D3, cost="x")))
    assert set(found) == {"tickets[1].trnc", "tickets[2].total_doc"}


def test_ticket_text_is_cleaned_and_codes_are_upper_cased() -> None:
    v = ok(
        direct(ticket(pax_name="  smith/john‮  mr ", rloc=" abc123 ", airline=" tk ", trnc="tktt"))
    )
    t = v.tickets[0]
    assert (t.pax_name, t.rloc, t.airline, t.trnc) == ("smith/john mr", "ABC123", "TK", "TKTT")


def test_every_ticket_type_is_accepted() -> None:
    for trnc in ("TKTT", "EMDA", "EMDS", "RFND", "CANX"):
        assert ok(direct(ticket(trnc=trnc))).tickets[0].trnc == trnc


def test_positions_follow_the_order_entered() -> None:
    v = ok(direct(ticket(D3), ticket(D1), ticket(D2), pb="99999"))
    assert [(t.position, t.doc_number) for t in v.tickets] == [(1, D3), (2, D1), (3, D2)]


# --- validate: client, phone, payment -------------------------------------------------------


def test_the_phone_is_optional_and_normalised() -> None:
    assert ok(direct()).ap is None
    assert ok(direct(ap="12 34 56 78")).ap == "+22212345678"
    assert ok(direct(ap="+33 6 12 34 56 78")).ap == "+33612345678"
    assert ok(direct(ap="   ")).ap is None


def test_a_bad_phone_is_refused() -> None:
    assert "ap" in errors(direct(ap="12345"))
    assert "ap" in errors(direct(ap=12345678))


def test_a_client_who_refused_gives_no_phone() -> None:
    v = ok(direct(client_refused_phone=True))
    assert v.client_refused_phone is True and v.ap is None
    assert "ap" in errors(direct(client_refused_phone=True, ap="12345678"))


def test_a_partner_sale_does_not_need_a_client() -> None:
    v = ok(partner_sale(7), ctx())
    assert v.clt is None
    assert ok(partner_sale(7, clt="  Mme Synth  "), ctx()).clt == "Mme Synth"


@pytest.mark.parametrize(
    "method", ["cash", "bankily", "masrvi", "sedad", "virement", "cheque", "credit"]
)
def test_every_payment_method_is_accepted(method: str) -> None:
    assert ok(direct(payment_method=method)).payment_method == method


@pytest.mark.parametrize("method", ["", None, "CASH", "paypal", "chèque"])
def test_an_unknown_payment_method_is_refused(method: Any) -> None:
    assert "payment_method" in errors(direct(payment_method=method))


def test_the_client_name_is_cleaned_and_capped() -> None:
    assert ok(direct(clt="  A​B \n C  ")).clt == "AB C"
    assert "clt" in errors(direct(clt="x" * 500))


def test_an_unknown_kind_of_sale_is_refused() -> None:
    assert "ag_kind" in errors(direct(ag_kind="other"))


def test_several_mistakes_are_reported_together() -> None:
    found = errors(direct(ticket(doc="x"), clt="", pb="-1", payment_method="nope", ap="1"))
    assert set(found) == {"tickets[0].doc_number", "clt", "pb", "payment_method", "ap"}


# --- the three pure helpers -----------------------------------------------------------------


def test_pb_per_line_gives_the_full_price_on_the_first_ticket_and_zero_on_the_others() -> None:
    v = ok(direct(ticket(D1, "10000"), ticket(D2, "11000", airline="TU"), pb="24000"))
    assert sales.pb_per_line(v) == [(D1, Decimal("24000.00")), (D2, Decimal("0.00"))]
    assert sum(pb for _, pb in sales.pb_per_line(v)) == v.pb


def test_pb_per_line_of_a_single_ticket_and_of_three() -> None:
    assert sales.pb_per_line(ok(direct(pb="5"))) == [(D1, Decimal("5.00"))]
    three = ok(direct(ticket(D3), ticket(D1), ticket(D2), pb="30000"))
    assert sales.pb_per_line(three) == [
        (D3, Decimal("30000.00")),
        (D1, Decimal("0.00")),
        (D2, Decimal("0.00")),
    ]


def test_cost_and_profit() -> None:
    v = ok(direct(ticket(D1, "10000"), ticket(D2, "11000.25"), pb="24000"))
    assert sales.sale_cost(v) == Decimal("21000.25")
    assert sales.sale_profit(v) == Decimal("2999.75")
    loss = ok(direct(ticket(cost="5000"), pb="4000"))
    assert sales.sale_profit(loss) == Decimal("-1000.00")


def test_for_a_partner_sale_the_profit_is_the_fee_times_the_tickets() -> None:
    v = ok(partner_sale(7, ticket(D1, "1000"), ticket(D2, "2000")), ctx("500"))
    assert sales.sale_profit(v) == Decimal("1000.00")


# --- the service ----------------------------------------------------------------------------


class World:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.agency_id = create_agency(session, "agence-v", "Agence V").id
        self.manager = self._user("+22212360001", "manager")
        self.accountant = self._user("+22212360002", "accountant")
        self.staff = self._user("+22212360003", "staff", "0004SV")
        self.staff2 = self._user("+22212360004", "staff", "0006AC")
        self.unlinked = self._user("+22212360005", "staff")
        self.two_signs = self._user("+22212360006", "staff", "0007AA", "0008BB")
        other = create_agency(session, "agence-w", "Agence W").id
        self.other_agency_id = other
        self.other_manager = self._user("+22212360007", "manager", agency_id=other)
        self.badi = partners.create_partner(session, self.manager, "BADI", now=NOW)
        partners.set_fee(
            session, self.badi, Decimal("500"), date(2026, 1, 1), self.manager, now=NOW
        )
        self.sahel = partners.create_partner(session, self.manager, "SAHEL", now=NOW)  # no fee

    def _user(self, phone: str, role: str, *signs: str, agency_id: int | None = None) -> User:
        user = User(agency_id=agency_id or self.agency_id, phone=phone, role=role)
        self.db.add(user)
        self.db.commit()
        for sign in signs:
            self.db.add(StaffSignLink(agency_id=user.agency_id, user_id=user.id, staff_sign=sign))
        self.db.commit()
        return user

    def create(self, actor: User, data: SaleInput, **kw: Any) -> sales.SaleResult:
        kw.setdefault("now", NOW)
        return sales.create_sale(self.db, actor, data, **kw)

    def edit(
        self, actor: User, sale_id: int, data: SaleInput, reason: str | None = None, **kw: Any
    ) -> sales.SaleResult:
        kw.setdefault("now", NOW)
        return sales.edit_sale(self.db, actor, sale_id, data, reason, **kw)

    def void(
        self, actor: User, sale_id: int, reason: str | None = "saisie en double", **kw: Any
    ) -> Any:
        kw.setdefault("now", NOW)
        return sales.void_sale(self.db, actor, sale_id, reason, **kw)

    def history(self, sale_id: int) -> list[SaleVersion]:
        return sales.sale_history_for(self.db, self.manager, sale_id)

    def audit(self, event_type: str) -> list[AuditEvent]:
        query = select(AuditEvent).where(AuditEvent.event_type == event_type)
        return list(self.db.scalars(query.order_by(AuditEvent.id)))

    def count(self, model: Any) -> int:
        return self.db.scalar(select(func.count()).select_from(model)) or 0

    def numbers(self) -> set[str]:
        return set(self.db.scalars(select(SaleTicket.doc_number)))

    def close(self, day: date = TODAY) -> None:
        found = days.get_day(self.db, self.agency_id, day)
        assert found is not None
        days.close_day(self.db, self.manager, found, now=NOW)


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


def snapshot(rows: list[SaleVersion]) -> list[tuple[Any, ...]]:
    return [
        (
            r.version_no,
            r.pb,
            r.cost_total,
            r.clt,
            r.ap,
            r.payment_method,
            r.reason,
            r.voided,
            r.actor_user_id,
            repr(r.tickets),
            r.fee_applied,
        )
        for r in rows
    ]


# --- creating -------------------------------------------------------------------------------


def test_a_staff_member_creates_a_sale_on_a_linked_sign(w: World) -> None:
    result = w.create(w.staff, direct(ticket(D1, "21368"), pb="24000", ap="12 34 56 78"))
    s = result.sale
    assert (s.agency_id, s.staff_sign, s.status, s.version_no) == (
        w.agency_id,
        "0004SV",
        "active",
        1,
    )
    assert (s.ag_kind, s.ag_label, s.pb, s.cost_total) == (
        "direct",
        "BRUGE",
        Decimal("24000.00"),
        Decimal("21368.00"),
    )
    assert (s.clt, s.ap, s.payment_method, s.day) == ("Client Synth", "+22212345678", "cash", TODAY)
    assert [(t.doc_number, t.position, t.total_doc) for t in s.tickets] == [
        (D1, 1, Decimal("21368.00"))
    ]
    assert result.warnings == ()
    assert w.count(Sale) == 1 and w.count(SaleTicket) == 1 and w.count(SaleVersion) == 1
    assert w.numbers() == {D1}
    loaded = sales.get_sale_for(w.db, w.manager, s.id)
    assert loaded is not None and loaded.pb == Decimal("24000.00")


def test_creating_opens_the_day(w: World) -> None:
    assert days.get_day(w.db, w.agency_id, TODAY) is None
    w.create(w.staff, direct())
    opened = days.get_day(w.db, w.agency_id, TODAY)
    assert opened is not None and opened.status == "open"


def test_a_price_below_the_cost_is_stored_and_returned_as_a_warning(w: World) -> None:
    result = w.create(w.staff, direct(ticket(cost="21368"), pb="20000"))
    assert len(result.warnings) == 1
    assert result.sale.pb == Decimal("20000.00")
    assert sales.sale_profit(result.sale) == Decimal("-1368.00")


def test_a_partner_sale_uses_the_fee_valid_on_the_day_of_the_sale(w: World) -> None:
    partners.set_fee(w.db, w.badi, Decimal("300"), date(2026, 10, 15), w.manager, now=NOW)
    today = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale
    assert today.pb == Decimal("1300.00") and today.fee_applied == Decimal("300.00")
    assert today.ag_label == "BADI" and today.partner_id == w.badi.id
    past = w.create(
        w.manager,
        partner_sale(w.badi.id, ticket(D2, "1000"), staff_sign="0004SV"),
        day=date(2026, 10, 10),
    ).sale
    assert past.pb == Decimal("1500.00") and past.day == date(2026, 10, 10)


def test_a_two_ticket_partner_sale_through_the_service(w: World) -> None:
    s = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"), ticket(D2, "2000"))).sale
    assert s.pb == Decimal("4000.00")  # 3000 + 2 x 500
    assert sales.sale_profit(s) == Decimal("1000.00")


def test_a_partner_sale_with_an_override_stores_the_reason(w: World) -> None:
    s = w.create(
        w.staff,
        partner_sale(
            w.badi.id, ticket(cost="1000"), fee_override="100", fee_override_reason="geste"
        ),
    )
    assert s.sale.pb == Decimal("1100.00")
    bad = partner_sale(w.badi.id, ticket(D2, "1000"), fee_override="100")
    with pytest.raises(InvalidSale) as info:
        w.create(w.staff, bad)
    assert [e.field for e in info.value.errors] == ["fee_override_reason"]


def test_a_partner_without_a_fee_is_refused_by_name(w: World) -> None:
    with pytest.raises(InvalidSale) as info:
        w.create(w.staff, partner_sale(w.sahel.id))
    assert "SAHEL" in info.value.errors[0].message
    assert w.count(Sale) == 0


def test_the_partner_of_another_agency_is_unknown(w: World) -> None:
    theirs = partners.create_partner(w.db, w.other_manager, "OTHER", now=NOW)
    with pytest.raises(InvalidSale) as info:
        w.create(w.staff, partner_sale(theirs.id))
    assert info.value.errors[0].field == "partner_id"


def test_a_deactivated_partner_is_refused(w: World) -> None:
    partners.deactivate_partner(w.db, w.manager, w.badi, now=NOW)
    with pytest.raises(InvalidSale):
        w.create(w.staff, partner_sale(w.badi.id))


def test_one_rm_price_for_two_tickets_is_one_sale(w: World) -> None:
    """A layover on two airlines: two tickets, one price, one sale."""
    data = direct(
        ticket(D1, "10000", airline="TK"),
        ticket(D2, "11000", airline="TU", rloc="DEF456"),
        pb="24000",
    )
    s = w.create(w.staff, data).sale
    assert w.count(Sale) == 1 and w.count(SaleTicket) == 2 and w.count(SaleVersion) == 1
    assert [t.airline for t in s.tickets] == ["TK", "TU"]
    assert s.pb == Decimal("24000.00") and s.cost_total == Decimal("21000.00")
    assert sales.pb_per_line(s) == [(D1, Decimal("24000.00")), (D2, Decimal("0.00"))]
    assert sales.sale_profit(s) == Decimal("3000.00")


def test_the_version_snapshot_holds_every_ticket_with_money_as_text(w: World) -> None:
    sale_id = w.create(
        w.staff, direct(ticket(D1, "10000.50"), ticket(D2, "0", airline="495"), pb="20000")
    ).sale.id
    (row,) = w.history(sale_id)
    assert row.tickets == [
        {
            "doc_number": D1,
            "airline": "199",
            "total_doc": "10000.50",
            "pax_name": "SYNTH01/PAX",
            "pax_type": "ADT",
            "rloc": "ABC123",
            "trnc": "TKTT",
            "position": 1,
        },
        {
            "doc_number": D2,
            "airline": "495",
            "total_doc": "0.00",
            "pax_name": "SYNTH01/PAX",
            "pax_type": "ADT",
            "rloc": "ABC123",
            "trnc": "TKTT",
            "position": 2,
        },
    ]
    assert isinstance(row.pb, Decimal) and not isinstance(row.pb, float)


def test_special_characters_are_stored_as_text_and_do_not_break_anything(w: World) -> None:
    clt = "Robert'); DROP TABLE sales;-- <b>é😀</b>"
    s = w.create(w.staff, direct(clt=clt)).sale
    assert s.clt == clt
    assert w.count(Sale) == 1


def test_the_creation_is_audited_without_names_or_phones(w: World) -> None:
    s = w.create(w.staff, direct(ticket(D1, "21368"), ap="12 34 56 78", clt="Zorglub Synth")).sale
    (event,) = w.audit("sale_created")
    assert event.actor_user_id == w.staff.id and event.agency_id == w.agency_id
    assert event.details["sale_id"] == s.id and event.details["version_no"] == 1
    assert event.details["pb"] == "24000.00" and event.details["cost_total"] == "21368.00"
    assert event.details["ticket_count"] == 1
    text = repr(event.details)
    assert "Zorglub" not in text and "22212345678" not in text and "SYNTH01" not in text


def test_an_unknown_source_is_refused(w: World) -> None:
    with pytest.raises(SaleError):
        w.create(w.staff, direct(), source="telepathy")
    assert w.count(Sale) == 0


# --- who may create -------------------------------------------------------------------------


def test_staff_with_no_linked_sign_is_told_to_ask_the_manager(w: World) -> None:
    with pytest.raises(SaleForbidden, match="Demandez au gérant de lier votre signe"):
        w.create(w.unlinked, direct())
    assert w.count(Sale) == 0 and w.count(SaleTicket) == 0


def test_staff_cannot_create_for_a_sign_that_is_not_theirs(w: World) -> None:
    with pytest.raises(SaleForbidden):
        w.create(w.staff, direct(staff_sign="0006AC"))
    with pytest.raises(SaleForbidden):
        w.create(w.staff, direct(staff_sign="0099ZZ"))
    assert w.count(Sale) == 0


def test_staff_with_two_signs_must_choose_one_of_them(w: World) -> None:
    with pytest.raises(InvalidSale) as info:
        w.create(w.two_signs, direct())
    assert info.value.errors[0].field == "staff_sign"
    assert w.create(w.two_signs, direct(staff_sign="0008bb")).sale.staff_sign == "0008BB"


def test_staff_cannot_pick_a_day_and_nobody_files_in_the_future(w: World) -> None:
    # owner, 26 Oct 2026: the day follows the tickets' issue date (tests/test_sale_day.py); a
    # staff member can no longer name a day of their own choosing
    with pytest.raises(SaleForbidden, match="émission"):
        w.create(w.staff, direct(), day=YESTERDAY)
    with pytest.raises(days.DayError, match="futur"):
        w.create(w.manager, direct(staff_sign="0004SV"), day=TODAY + timedelta(days=1))
    assert w.count(Sale) == 0


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_manager_and_accountant_create_for_any_sign_and_past_day(w: World, who: str) -> None:
    actor = getattr(w, who)
    s = w.create(actor, direct(staff_sign="0006ac"), day=YESTERDAY).sale
    assert (s.staff_sign, s.day, s.created_by_user_id) == ("0006AC", YESTERDAY, actor.id)


def test_a_manager_must_say_which_sign(w: World) -> None:
    with pytest.raises(InvalidSale) as info:
        w.create(w.manager, direct())
    assert info.value.errors[0].field == "staff_sign"


def test_a_bad_sign_is_refused(w: World) -> None:
    with pytest.raises(InvalidSale):
        w.create(w.manager, direct(staff_sign="a"))


# --- duplicates -----------------------------------------------------------------------------


def test_a_ticket_already_in_another_sale_is_a_duplicate_with_the_existing_id(w: World) -> None:
    first = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale
    with pytest.raises(DuplicateTicket) as info:
        w.create(w.manager, direct(ticket(D3), ticket(D2), staff_sign="0006AC"))
    assert info.value.sale_id == first.id and info.value.doc_number == D2
    assert str(info.value)  # a French message
    with pytest.raises(DuplicateTicket) as own:  # the owner of the sign sees it too
        w.create(w.staff, direct(ticket(D3), ticket(D2)))
    assert own.value.sale_id == first.id
    assert w.count(Sale) == 1 and w.numbers() == {D1, D2}  # nothing of the refused sale is left
    assert w.count(SaleVersion) == 1
    w.db.commit()  # the session is still usable
    assert w.create(w.staff2, direct(ticket(D3))).sale.id != first.id


def test_the_same_number_may_exist_in_another_agency(w: World) -> None:
    w.create(w.staff, direct(ticket(D1)))
    other = sales.create_sale(
        w.db, w.other_manager, direct(ticket(D1), staff_sign="0004SV"), now=NOW
    )
    assert other.sale.agency_id == w.other_agency_id


def test_the_duplicate_check_does_not_reveal_another_agency(w: World) -> None:
    other = sales.create_sale(
        w.db, w.other_manager, direct(ticket(D1), staff_sign="0004SV"), now=NOW
    )
    result = w.create(w.staff, direct(ticket(D1)))  # fine: the other agency's sale is not ours
    assert result.sale.id != other.sale.id


def test_editing_into_a_ticket_of_another_sale_is_a_duplicate(w: World) -> None:
    a = w.create(w.staff, direct(ticket(D1))).sale
    b = w.create(w.staff, direct(ticket(D2))).sale
    with pytest.raises(DuplicateTicket) as info:
        w.edit(w.staff, b.id, direct(ticket(D2), ticket(D1)))
    assert info.value.sale_id == a.id
    again = sales.get_sale_for(w.db, w.manager, b.id)
    assert again is not None and again.version_no == 1 and len(again.tickets) == 1
    assert len(w.history(b.id)) == 1
    assert w.numbers() == {D1, D2}


def test_a_duplicate_that_slips_past_the_check_is_caught_by_the_database(w: World) -> None:
    """The race: another request commits the number after our "already sold?" check (the
    ``after_duplicate_check`` seam stands in for it). The unique constraint refuses the insert,
    the whole sale is rolled back to its savepoint, and the user gets the same DuplicateTicket."""
    competing = {"sale": None}

    def other_request_wins() -> None:
        sales.after_duplicate_check = None
        competing["sale"] = w.create(w.staff, direct(ticket(D2))).sale  # type: ignore[assignment]

    sales.after_duplicate_check = other_request_wins
    try:
        with pytest.raises(DuplicateTicket) as info:
            w.create(w.manager, direct(ticket(D3), ticket(D2), staff_sign="0006AC"))
    finally:
        sales.after_duplicate_check = None
    assert info.value.doc_number == D2
    assert info.value.sale_id == competing["sale"].id  # type: ignore[attr-defined]
    assert w.count(Sale) == 1 and w.count(SaleVersion) == 1 and w.numbers() == {D2}
    mine = w.create(w.manager, direct(ticket(D3), staff_sign="0006AC")).sale

    def other_request_wins_again() -> None:
        sales.after_duplicate_check = None
        w.create(w.staff, direct(ticket(D4)))

    sales.after_duplicate_check = other_request_wins_again
    try:
        with pytest.raises(DuplicateTicket) as again:
            w.edit(w.manager, mine.id, direct(ticket(D3), ticket(D4), staff_sign="0006AC"), "ajout")
    finally:
        sales.after_duplicate_check = None
    assert again.value.doc_number == D4
    assert [len(r.tickets) for r in w.history(mine.id)] == [1]
    assert w.numbers() == {D2, D3, D4}


# --- editing: history -----------------------------------------------------------------------


def test_an_edit_appends_a_version_and_leaves_the_old_one_untouched(w: World) -> None:
    sale_id = w.create(w.staff, direct(ticket(D1, "10000"), pb="12000")).sale.id
    before = snapshot(w.history(sale_id))
    result = w.edit(
        w.staff, sale_id, direct(ticket(D1, "10000"), pb="13000", clt="Autre Client"), "prix revu"
    )
    assert result.sale.version_no == 2 and result.sale.pb == Decimal("13000.00")
    rows = w.history(sale_id)
    assert [r.version_no for r in rows] == [1, 2]
    assert snapshot(rows)[:1] == before
    assert (rows[1].pb, rows[1].clt, rows[1].reason, rows[1].actor_user_id) == (
        Decimal("13000.00"),
        "Autre Client",
        "prix revu",
        w.staff.id,
    )
    head = w.db.scalars(select(Sale).where(Sale.id == sale_id)).one()
    assert head.current_version == 2
    loaded = sales.get_sale_for(w.db, w.manager, sale_id)
    assert loaded is not None and loaded.pb == Decimal("13000.00") and loaded.clt == "Autre Client"
    (event,) = w.audit("sale_edited")
    assert event.details["sale_id"] == sale_id and event.details["version_no"] == 2
    assert "prix revu" not in repr(event.details) and "Autre" not in repr(event.details)


def test_adding_a_ticket_to_a_sale_is_a_new_version(w: World) -> None:
    sale_id = w.create(w.staff, direct(ticket(D1, "10000"), pb="12000")).sale.id
    result = w.edit(w.staff, sale_id, direct(ticket(D1, "10000"), ticket(D2, "5000"), pb="18000"))
    assert [t.doc_number for t in result.sale.tickets] == [D1, D2]
    assert result.sale.cost_total == Decimal("15000.00")
    assert w.numbers() == {D1, D2}
    assert [len(r.tickets) for r in w.history(sale_id)] == [1, 2]
    assert w.count(Sale) == 1


def test_removing_a_ticket_is_a_new_version_and_frees_its_number(w: World) -> None:
    sale_id = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale.id
    result = w.edit(w.staff, sale_id, direct(ticket(D2), pb="15000"), "D1 était une erreur")
    assert [(t.doc_number, t.position) for t in result.sale.tickets] == [(D2, 1)]
    assert w.numbers() == {D2}
    assert [len(r.tickets) for r in w.history(sale_id)] == [2, 1]
    other = w.create(w.staff, direct(ticket(D1))).sale  # the freed number can be sold again
    assert other.id != sale_id


def test_reordering_the_tickets_changes_which_one_carries_the_price(w: World) -> None:
    sale_id = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale.id
    result = w.edit(w.staff, sale_id, direct(ticket(D2), ticket(D1), pb="30000"))
    assert [p for p, _ in sales.pb_per_line(result.sale)] == [D2, D1]
    assert w.numbers() == {D1, D2}


def test_an_edit_that_changes_nothing_is_refused(w: World) -> None:
    data = direct(ticket(D1, "10000"), pb="12000")
    sale_id = w.create(w.staff, data).sale.id
    with pytest.raises(SaleError, match="modif"):
        w.edit(w.staff, sale_id, data)
    assert len(w.history(sale_id)) == 1


def test_a_cost_change_on_a_partner_sale_recomputes_the_price_with_the_same_fee(w: World) -> None:
    sale_id = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale.id
    # the standard fee changes afterwards: the sale keeps the fee it was priced with
    partners.set_fee(w.db, w.badi, Decimal("900"), date(2026, 10, 1), w.manager, now=NOW)
    result = w.edit(w.staff, sale_id, partner_sale(w.badi.id, ticket(D1, "1200")), "coût corrigé")
    assert result.sale.pb == Decimal("1700.00")  # 1200 + 500, not 1200 + 900
    assert result.sale.fee_applied == Decimal("500.00")
    assert [r.pb for r in w.history(sale_id)] == [Decimal("1500.00"), Decimal("1700.00")]


def test_a_cost_change_keeps_an_override_and_its_reason(w: World) -> None:
    created = partner_sale(
        w.badi.id, ticket(D1, "1000"), fee_override="100", fee_override_reason="geste"
    )
    sale_id = w.create(w.staff, created).sale.id
    result = w.edit(w.staff, sale_id, partner_sale(w.badi.id, ticket(D1, "1500")), "coût corrigé")
    assert result.sale.pb == Decimal("1600.00")
    assert (result.sale.fee_applied, result.sale.fee_override_reason) == (
        Decimal("100.00"),
        "geste",
    )


def test_resetting_the_fee_uses_the_standard_fee_of_the_day(w: World) -> None:
    created = partner_sale(
        w.badi.id, ticket(D1, "1000"), fee_override="100", fee_override_reason="geste"
    )
    sale_id = w.create(w.staff, created).sale.id
    result = w.edit(
        w.staff,
        sale_id,
        partner_sale(w.badi.id, ticket(D1, "1000"), reset_fee=True),
        "retour au tarif",
    )
    assert result.sale.pb == Decimal("1500.00") and result.sale.fee_override_reason is None


def test_changing_the_partner_uses_the_new_partners_fee(w: World) -> None:
    partners.set_fee(w.db, w.sahel, Decimal("200"), date(2026, 1, 1), w.manager, now=NOW)
    sale_id = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale.id
    result = w.edit(
        w.staff, sale_id, partner_sale(w.sahel.id, ticket(D1, "1000")), "mauvais partenaire"
    )
    assert result.sale.pb == Decimal("1200.00") and result.sale.ag_label == "SAHEL"


def test_a_sale_can_change_from_partner_to_direct(w: World) -> None:
    sale_id = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale.id
    result = w.edit(
        w.staff, sale_id, direct(ticket(D1, "1000"), pb="1800"), "c'était un client direct"
    )
    assert (result.sale.ag_kind, result.sale.partner_id, result.sale.fee_applied) == (
        "direct",
        None,
        None,
    )


# --- editing: rights ------------------------------------------------------------------------


def test_staff_may_edit_their_own_same_day_sale_without_a_reason(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    result = w.edit(w.staff, sale_id, direct(pb="25000"), None)
    assert result.sale.version_no == 2
    assert w.history(sale_id)[1].reason is None
    w.edit(w.staff, sale_id, direct(pb="26000"), "   ")
    assert w.history(sale_id)[2].reason is None


def test_a_staff_reason_that_is_given_must_still_be_long_enough(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    with pytest.raises(SaleError, match="motif"):
        w.edit(w.staff, sale_id, direct(pb="25000"), "ab")


def test_staff_cannot_edit_another_signs_sale(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    with pytest.raises(SaleNotFound):
        w.edit(w.staff2, sale_id, direct(pb="25000"), "je corrige")
    assert sales.get_sale_for(w.db, w.staff2, sale_id) is None
    assert len(w.history(sale_id)) == 1


def test_staff_cannot_move_their_sale_to_another_sign(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    with pytest.raises(SaleForbidden):
        w.edit(w.staff, sale_id, direct(pb="24000", staff_sign="0006AC"), "ok ok")


def test_staff_edit_a_sale_of_a_past_day_that_is_not_closed(w: World) -> None:
    sale_id = w.create(w.manager, direct(staff_sign="0004SV"), day=YESTERDAY).sale.id
    result = w.edit(w.staff, sale_id, direct(pb="25000"), "correction")
    assert result.sale.pb == Decimal("25000.00") and result.sale.day == YESTERDAY
    assert len(w.history(sale_id)) == 2


def test_staff_cannot_edit_a_sale_of_a_closed_past_day(w: World) -> None:
    sale_id = w.create(w.manager, direct(staff_sign="0004SV"), day=YESTERDAY).sale.id
    w.close(YESTERDAY)
    with pytest.raises(days.DayClosed, match="clôturée"):
        w.edit(w.staff, sale_id, direct(pb="25000"), "trop tard")
    assert len(w.history(sale_id)) == 1


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_manager_and_accountant_edit_any_sale_with_a_reason(w: World, who: str) -> None:
    actor = getattr(w, who)
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    for reason in (None, "", "   ", "abcd"):
        with pytest.raises(SaleError, match="motif"):
            w.edit(actor, sale_id, direct(pb="25000"), reason)
    assert len(w.history(sale_id)) == 1
    result = w.edit(actor, sale_id, direct(pb="25000"), "  le client a négocié  ")
    assert result.sale.pb == Decimal("25000.00") and result.sale.staff_sign == "0004SV"
    assert w.history(sale_id)[1].reason == "le client a négocié"
    assert w.history(sale_id)[1].actor_user_id == actor.id


def test_manager_edits_a_past_days_sale_and_may_change_the_sign(w: World) -> None:
    sale_id = w.create(w.manager, direct(staff_sign="0004SV"), day=YESTERDAY).sale.id
    result = w.edit(w.manager, sale_id, direct(pb="25000", staff_sign="0006AC"), "mauvais signe")
    assert result.sale.staff_sign == "0006AC" and result.sale.day == YESTERDAY


def test_another_agency_cannot_read_or_edit_a_sale(w: World) -> None:
    sale_id = w.create(w.staff, direct()).sale.id
    assert sales.get_sale_for(w.db, w.other_manager, sale_id) is None
    assert sales.sale_history_for(w.db, w.other_manager, sale_id) == []
    with pytest.raises(SaleNotFound):
        w.edit(w.other_manager, sale_id, direct(pb="1", staff_sign="0004SV"), "intrusion")
    with pytest.raises(SaleNotFound):
        w.void(w.other_manager, sale_id)
    assert len(w.history(sale_id)) == 1


def test_an_unknown_sale_is_not_found(w: World) -> None:
    with pytest.raises(SaleNotFound):
        w.edit(w.manager, 999_999, direct(staff_sign="0004SV"), "rien du tout")


# --- closed days ----------------------------------------------------------------------------


def test_a_closed_day_refuses_create_edit_and_void_for_everybody(w: World) -> None:
    sale_id = w.create(w.staff, direct(ticket(D1), pb="24000")).sale.id
    w.close()
    for actor in (w.staff, w.manager, w.accountant):
        with pytest.raises(days.DayClosed):
            w.create(actor, direct(ticket(D2), staff_sign="0004SV"))
    for actor in (w.staff, w.manager, w.accountant):
        with pytest.raises(days.DayClosed):
            w.edit(actor, sale_id, direct(pb="25000"), "après la clôture")
        with pytest.raises(days.DayClosed):
            w.void(actor, sale_id)
    assert w.count(Sale) == 1 and len(w.history(sale_id)) == 1 and w.numbers() == {D1}


def test_after_a_reopen_the_day_accepts_changes_again(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    w.close()
    found = days.get_day(w.db, w.agency_id, TODAY)
    assert found is not None
    days.reopen_day(w.db, w.manager, found, "oubli d'un billet", now=NOW)
    assert w.edit(w.staff, sale_id, direct(pb="25000")).sale.version_no == 2


def test_a_closed_past_day_refuses_a_manager_too(w: World) -> None:
    sale_id = w.create(w.manager, direct(staff_sign="0004SV"), day=YESTERDAY).sale.id
    w.close(YESTERDAY)
    with pytest.raises(days.DayClosed):
        w.edit(w.manager, sale_id, direct(pb="25000"), "trop tard")


# --- voiding --------------------------------------------------------------------------------


def test_voiding_keeps_the_history_frees_the_numbers_and_leaves_the_totals(w: World) -> None:
    keep = w.create(w.staff, direct(ticket(D3, "1000"), pb="2000")).sale
    gone = w.create(w.staff, direct(ticket(D1, "10000"), ticket(D2, "11000"), pb="24000")).sale
    assert sales.day_totals(sales.sales_for_day_for(w.db, w.manager, TODAY)).tickets == 3
    voided = w.void(w.staff, gone.id, "saisie en double")
    assert voided.status == "voided" and voided.version_no == 2
    assert w.numbers() == {D3}
    live = sales.sales_for_day_for(w.db, w.manager, TODAY)
    assert [s.id for s in live] == [keep.id]
    totals = sales.day_totals(live)
    assert (totals.sales, totals.tickets, totals.revenue, totals.cost, totals.profit) == (
        1,
        1,
        Decimal("2000.00"),
        Decimal("1000.00"),
        Decimal("1000.00"),
    )
    everything = sales.sales_for_day_for(w.db, w.manager, TODAY, include_voided=True)
    assert [s.id for s in everything] == [keep.id, gone.id]
    assert sales.day_totals(everything) == totals  # voided sales never count
    history = w.history(gone.id)
    assert [(r.version_no, r.voided, len(r.tickets)) for r in history] == [
        (1, False, 2),
        (2, True, 2),
    ]
    assert history[1].reason == "saisie en double"
    again = sales.get_sale_for(w.db, w.manager, gone.id)
    assert again is not None and again.status == "voided" and len(again.tickets) == 2
    (event,) = w.audit("sale_voided")
    assert event.details["sale_id"] == gone.id and "double" not in repr(event.details)


def test_a_voided_sale_cannot_be_edited_or_voided_again(w: World) -> None:
    sale_id = w.create(w.staff, direct()).sale.id
    w.void(w.staff, sale_id)
    with pytest.raises(SaleError, match="annul"):
        w.edit(w.staff, sale_id, direct(pb="1"), "encore")
    with pytest.raises(SaleError, match="annul"):
        w.void(w.manager, sale_id)


def test_a_number_of_a_voided_sale_can_be_sold_again(w: World) -> None:
    first = w.create(w.staff, direct(ticket(D1))).sale.id
    w.void(w.staff, first)
    second = w.create(w.staff2, direct(ticket(D1))).sale.id
    assert second != first


@pytest.mark.parametrize("reason", [None, "", "  ", "abcd"])
def test_voiding_needs_a_reason(w: World, reason: str | None) -> None:
    sale_id = w.create(w.staff, direct()).sale.id
    with pytest.raises(SaleError, match="motif"):
        w.void(w.staff, sale_id, reason)
    assert sales.get_sale_for(w.db, w.manager, sale_id).status == "active"  # type: ignore[union-attr]


def test_staff_void_only_their_own_sales_on_a_day_that_is_not_closed(w: World) -> None:
    mine = w.create(w.staff, direct(ticket(D1))).sale.id
    theirs = w.create(w.staff2, direct(ticket(D2))).sale.id
    past = w.create(w.manager, direct(ticket(D3), staff_sign="0004SV"), day=YESTERDAY).sale.id
    with pytest.raises(SaleNotFound):
        w.void(w.staff, theirs)
    assert w.void(w.staff, past).status == "voided"  # an earlier day that is open
    assert w.void(w.staff, mine).status == "voided"
    closed = w.create(w.manager, direct(ticket(D4), staff_sign="0004SV"), day=YESTERDAY).sale.id
    w.close(YESTERDAY)
    with pytest.raises(days.DayClosed):
        w.void(w.staff, closed)


# --- reading --------------------------------------------------------------------------------


def test_sales_for_day_filters_by_sign_and_agency(w: World) -> None:
    a = w.create(w.staff, direct(ticket(D1))).sale
    b = w.create(w.staff2, direct(ticket(D2))).sale
    sales.create_sale(w.db, w.other_manager, direct(ticket(D3), staff_sign="0004SV"), now=NOW)
    yesterday = w.create(w.manager, direct(ticket(D4), staff_sign="0004SV"), day=YESTERDAY).sale

    def ids(signs: list[str] | None, day: date = TODAY) -> list[int]:
        return [s.id for s in sales.sales_for_day_for(w.db, w.manager, day, signs=signs)]

    assert ids(None) == [a.id, b.id]
    assert ids(["0004SV"]) == [a.id]
    assert ids(["0006AC", "0004SV"]) == [a.id, b.id]
    assert ids([]) == []
    assert ids(["NOPE00"]) == []
    assert ids(None, YESTERDAY) == [yesterday.id]
    assert ids(None, date(2026, 1, 1)) == []


def test_visible_signs_follow_the_permissions(w: World) -> None:
    assert sales.visible_signs(w.db, w.manager) is None
    assert sales.visible_signs(w.db, w.accountant) is None
    assert sales.visible_signs(w.db, w.staff) == ["0004SV"]
    assert sales.visible_signs(w.db, w.two_signs) == ["0007AA", "0008BB"]
    assert sales.visible_signs(w.db, w.unlinked) == []


def test_get_sale_for_hides_other_signs_from_staff_only(w: World) -> None:
    sale_id = w.create(w.staff, direct()).sale.id
    assert sales.get_sale_for(w.db, w.staff, sale_id) is not None
    assert sales.get_sale_for(w.db, w.manager, sale_id) is not None
    assert sales.get_sale_for(w.db, w.accountant, sale_id) is not None
    assert sales.get_sale_for(w.db, w.staff2, sale_id) is None
    assert sales.get_sale_for(w.db, w.other_manager, sale_id) is None


def test_preview_returns_the_read_back_without_writing(w: World) -> None:
    got = sales.preview_sale(w.db, w.staff, direct(ticket(cost="21368"), pb="20000"))
    assert isinstance(got, ValidatedSale) and len(got.warnings) == 1
    partner = sales.preview_sale(w.db, w.staff, partner_sale(w.badi.id, ticket(cost="1000")))
    assert isinstance(partner, ValidatedSale) and partner.pb == Decimal("1500.00")
    bad = sales.preview_sale(w.db, w.staff, partner_sale(w.sahel.id))
    assert isinstance(bad, list) and bad[0].field == "partner_id"
    assert w.count(Sale) == 0 and w.count(SaleVersion) == 0


def test_a_large_day_of_sales_is_read_in_a_constant_number_of_queries(w: World) -> None:
    for n in range(30):
        w.create(w.staff, direct(ticket(f"{3940001000 + n}")))
    statements: list[str] = []
    engine = w.db.get_bind()

    def record(*args: Any) -> None:
        statements.append(args[2])

    event.listen(engine, "before_cursor_execute", record)
    try:
        rows = sales.sales_for_day_for(w.db, w.manager, TODAY)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(rows) == 30 and len(statements) <= 3  # the actor's signs, then one query


# --- review round: stale rows, scoped previews, limits, B8 helpers --------------------------


def other_session(w: World) -> Session:
    """A second session on the same database: another request. Commit this one's work first."""
    w.db.commit()
    return Session(w.db.get_bind(), expire_on_commit=False)


def test_a_sale_voided_by_another_request_cannot_be_edited_back(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    assert sales.get_sale_for(w.db, w.staff, sale_id) is not None  # the head is now in our session
    with other_session(w) as other:
        sales.void_sale(other, w.manager, sale_id, "doublon confirmé", now=NOW)
        other.commit()
    with pytest.raises(SaleError, match="annul"):
        w.edit(w.staff, sale_id, direct(pb="25000"))
    with pytest.raises(SaleError, match="annul"):
        w.void(w.staff, sale_id)
    w.db.rollback()
    assert [(r.version_no, r.voided) for r in w.history(sale_id)] == [(1, False), (2, True)]
    assert w.numbers() == set()


def test_an_edit_made_by_another_request_is_not_overwritten_or_duplicated(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    assert sales.get_sale_for(w.db, w.staff, sale_id) is not None
    with other_session(w) as other:
        data = direct(pb="26000", staff_sign="0004SV")
        sales.edit_sale(other, w.manager, sale_id, data, "correction", now=NOW)
        other.commit()
    result = w.edit(w.staff, sale_id, direct(pb="27000"))
    assert result.sale.version_no == 3  # on top of the other request's version, not a clash
    assert [r.pb for r in w.history(sale_id)] == [
        Decimal("24000.00"),
        Decimal("26000.00"),
        Decimal("27000.00"),
    ]


def test_a_version_number_clash_is_a_clean_refusal(w: World) -> None:
    sale_id = w.create(w.staff, direct(pb="24000")).sale.id
    first = w.history(sale_id)[0]

    def other_request_edits() -> None:
        sales.after_duplicate_check = None
        w.db.add(
            SaleVersion(
                agency_id=w.agency_id,
                sale_id=sale_id,
                version_no=2,
                report_day_id=first.report_day_id,
                staff_sign="0004SV",
                ag_kind="direct",
                ag_label="BRUGE",
                pb=Decimal("1"),
                cost_total=Decimal("1"),
                clt="X",
                payment_method="cash",
                tickets=[],
                source="form",
                actor_user_id=w.staff.id,
            )
        )
        w.db.flush()

    sales.after_duplicate_check = other_request_edits
    try:
        with pytest.raises(sales.StaleSale, match="rechargez"):
            w.edit(w.staff, sale_id, direct(pb="25000"))
    finally:
        sales.after_duplicate_check = None


# --- preview follows the same access rules as the write -------------------------------------


def test_preview_of_another_signs_sale_is_not_found_and_leaks_nothing(w: World) -> None:
    override = partner_sale(w.badi.id, ticket(D1, "1000"), fee_override="100")
    sale_id = w.create(w.staff, replace(override, fee_override_reason="geste secret")).sale.id
    with pytest.raises(SaleNotFound):
        sales.preview_sale(w.db, w.staff2, partner_sale(w.badi.id), sale_id=sale_id, now=NOW)
    with pytest.raises(SaleNotFound):
        sales.preview_sale(w.db, w.other_manager, partner_sale(w.badi.id), sale_id=sale_id, now=NOW)
    seen = sales.preview_sale(
        w.db, w.staff, partner_sale(w.badi.id, ticket(D1, "1000")), sale_id=sale_id, now=NOW
    )
    assert isinstance(seen, ValidatedSale) and seen.pb == Decimal("1100.00")  # own sale: fee kept


def test_preview_of_a_sale_on_a_closed_day_or_voided_is_refused(w: World) -> None:
    today = w.create(w.staff, direct(ticket(D1))).sale.id
    old = w.create(w.manager, direct(ticket(D2), staff_sign="0004SV"), day=YESTERDAY).sale.id
    gone = w.create(w.staff, direct(ticket(D3))).sale.id
    w.void(w.staff, gone)
    with pytest.raises(SaleError, match="annul"):
        sales.preview_sale(w.db, w.staff, direct(ticket(D3)), sale_id=gone, now=NOW)
    assert isinstance(  # staff may correct an earlier day that is open (owner, 26 Oct 2026)
        sales.preview_sale(w.db, w.staff, direct(ticket(D2)), sale_id=old, now=NOW),
        ValidatedSale,
    )
    assert isinstance(
        sales.preview_sale(
            w.db, w.manager, direct(ticket(D2), staff_sign="0004SV"), sale_id=old, now=NOW
        ),
        ValidatedSale,
    )
    w.close()
    with pytest.raises(days.DayClosed):
        sales.preview_sale(w.db, w.staff, direct(ticket(D1)), sale_id=today, now=NOW)
    w.close(YESTERDAY)
    with pytest.raises(days.DayClosed):
        sales.preview_sale(w.db, w.staff, direct(ticket(D2)), sale_id=old, now=NOW)


def test_preview_reports_a_duplicate_like_the_write_does(w: World) -> None:
    first = w.create(w.staff, direct(ticket(D1))).sale.id
    mine = w.create(w.staff, direct(ticket(D2))).sale.id
    with pytest.raises(DuplicateTicket) as new:
        sales.preview_sale(w.db, w.staff, direct(ticket(D1)), now=NOW)
    assert new.value.sale_id == first
    with pytest.raises(DuplicateTicket) as edit:
        sales.preview_sale(w.db, w.staff, direct(ticket(D2), ticket(D1)), sale_id=mine, now=NOW)
    assert edit.value.sale_id == first
    with pytest.raises(DuplicateTicket) as hidden:  # another sign's sale is not named
        sales.preview_sale(w.db, w.staff2, direct(ticket(D1)), now=NOW)
    assert hidden.value.sale_id is None


def test_a_staff_member_without_a_sign_cannot_preview(w: World) -> None:
    with pytest.raises(SaleForbidden, match="lier votre signe"):
        sales.preview_sale(w.db, w.unlinked, direct(), now=NOW)


# --- amounts that do not fit the column -----------------------------------------------------


def test_totals_that_overflow_numeric_14_2_are_refused() -> None:
    big = "999999999999"
    twenty = [ticket(f"{3940000000 + n}", big) for n in range(20)]
    assert "tickets" in errors(direct(*twenty, pb="1"))
    assert "Montant trop grand" in errors(direct(*twenty, pb="1"))["tickets"]
    assert "pb" in errors(partner_sale(7, ticket(D1, big)), ctx("500"))  # cost + fee >= 10**12
    zero_cost = [ticket(f"{3940000000 + n}", "0") for n in range(20)]
    found = errors(
        partner_sale(7, *zero_cost, fee_override=big, fee_override_reason="accord"), ctx()
    )
    assert "fee_override" in found
    just_under = ok(partner_sale(7, ticket(D1, "999999999499")), ctx("500"))
    assert just_under.pb == Decimal("999999999999.00")


# --- duplicates do not name other agents' sales ---------------------------------------------


def test_a_duplicate_does_not_name_the_sale_of_another_agent(w: World) -> None:
    first = w.create(w.staff, direct(ticket(D1))).sale
    with pytest.raises(DuplicateTicket) as info:
        w.create(w.staff2, direct(ticket(D1)))
    assert info.value.sale_id is None and info.value.doc_number == D1
    assert "modifi" not in str(info.value) and "autre agent" in str(info.value)
    with pytest.raises(DuplicateTicket) as shown:
        w.create(w.accountant, direct(ticket(D1), staff_sign="0006AC"))
    assert shown.value.sale_id == first.id


# --- fee overrides stay visible -------------------------------------------------------------


def test_create_and_edit_audit_show_the_fee_and_whether_it_is_an_override(w: World) -> None:
    plain = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale
    override = partner_sale(
        w.badi.id, ticket(D2, "1000"), fee_override="100", fee_override_reason="geste"
    )
    special = w.create(w.staff, override).sale
    direct_sale = w.create(w.staff, direct(ticket(D3))).sale
    by_sale = {e.details["sale_id"]: e.details for e in w.audit("sale_created")}
    assert (by_sale[plain.id]["fee_applied"], by_sale[plain.id]["fee_override"]) == (
        "500.00",
        False,
    )
    assert (by_sale[special.id]["fee_applied"], by_sale[special.id]["fee_override"]) == (
        "100.00",
        True,
    )
    assert by_sale[special.id]["pb"] == "1100.00"
    assert (by_sale[direct_sale.id]["fee_applied"], by_sale[direct_sale.id]["fee_override"]) == (
        None,
        False,
    )
    w.edit(w.staff, plain.id, replace(override, tickets=(ticket(D1, "1000"),)), "tarif particulier")
    edited = w.audit("sale_edited")[0].details
    assert (edited["fee_applied"], edited["fee_override"], edited["pb"]) == (
        "100.00",
        True,
        "1100.00",
    )
    assert "geste" not in repr(by_sale) and "tarif particulier" not in repr(edited)


def test_overrides_for_day_lists_active_sales_with_an_override_within_the_actors_scope(
    w: World,
) -> None:
    w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000")))
    mine = w.create(
        w.staff,
        partner_sale(
            w.badi.id, ticket(D2, "1000"), fee_override="100", fee_override_reason="geste"
        ),
    ).sale
    theirs = w.create(
        w.staff2,
        partner_sale(
            w.badi.id, ticket(D3, "1000"), fee_override="50", fee_override_reason="geste 2"
        ),
    ).sale
    gone = w.create(
        w.staff,
        partner_sale(
            w.badi.id, ticket(D4, "1000"), fee_override="60", fee_override_reason="geste 3"
        ),
    ).sale
    w.void(w.staff, gone.id)
    w.create(w.staff, direct(ticket("3940000005")))
    assert [s.id for s in sales.overrides_for_day(w.db, w.manager, TODAY)] == [mine.id, theirs.id]
    assert [s.id for s in sales.overrides_for_day(w.db, w.accountant, TODAY)] == [
        mine.id,
        theirs.id,
    ]
    assert [s.id for s in sales.overrides_for_day(w.db, w.staff, TODAY)] == [mine.id]
    assert sales.overrides_for_day(w.db, w.other_manager, TODAY) == []
    assert sales.overrides_for_day(w.db, w.manager, YESTERDAY) == []
    w.edit(w.staff, mine.id, partner_sale(w.badi.id, ticket(D2, "1000"), reset_fee=True), "retour")
    assert [s.id for s in sales.overrides_for_day(w.db, w.manager, TODAY)] == [theirs.id]


# --- the public reads are scoped to the actor -----------------------------------------------


def test_history_and_day_reads_are_scoped_to_the_actor(w: World) -> None:
    a = w.create(w.staff, direct(ticket(D1))).sale
    b = w.create(w.staff2, direct(ticket(D2))).sale
    assert sales.sale_history_for(w.db, w.staff2, a.id) == []
    assert len(sales.sale_history_for(w.db, w.staff, a.id)) == 1
    assert len(sales.sale_history_for(w.db, w.accountant, a.id)) == 1
    assert [s.id for s in sales.sales_for_day_for(w.db, w.staff, TODAY)] == [a.id]
    assert [s.id for s in sales.sales_for_day_for(w.db, w.accountant, TODAY)] == [a.id, b.id]
    assert sales.sales_for_day_for(w.db, w.unlinked, TODAY) == []
    assert sales.sales_for_day_for(w.db, w.staff, TODAY, signs=["0006AC"]) == []  # cannot widen
    assert [
        s.id for s in sales.sales_for_day_for(w.db, w.staff, TODAY, signs=["0004SV", "0006AC"])
    ] == [a.id]
    for name in ("get_sale", "sale_history", "sales_for_day"):
        assert not hasattr(sales, name), f"{name} would read any agent's sales by agency id"


# --- moving a sale to another day (B8) ------------------------------------------------------


def test_a_manager_moves_a_sale_to_an_earlier_day_with_a_new_version(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale
    old_day = days.get_day(w.db, w.agency_id, TODAY)
    assert old_day is not None
    moved = sales.move_sale_day(
        w.db, w.manager, sale.id, YESTERDAY, "billet émis la veille", source="form", now=NOW
    )
    assert (moved.day, moved.version_no, moved.status) == (YESTERDAY, 2, "active")
    assert moved.report_day_id != old_day.id and moved.pb == Decimal("30000.00")
    new_day = days.get_day(w.db, w.agency_id, YESTERDAY)
    assert new_day is not None and new_day.id == moved.report_day_id and new_day.status == "open"
    history = w.history(sale.id)
    assert [v.report_day_id for v in history] == [old_day.id, new_day.id]
    assert history[1].reason == "billet émis la veille" and history[1].source == "form"
    assert [t["doc_number"] for t in history[1].tickets] == [D1, D2]
    assert w.numbers() == {D1, D2}
    assert sales.sales_for_day_for(w.db, w.manager, TODAY) == []
    assert [s.id for s in sales.sales_for_day_for(w.db, w.manager, YESTERDAY)] == [sale.id]
    (event,) = w.audit("sale_moved")
    assert event.details["sale_id"] == sale.id and event.details["from_report_day_id"] == old_day.id
    assert event.details["to_report_day_id"] == new_day.id and "billet" not in repr(event.details)
    assert sales.check_sale_consistency(w.db, w.agency_id) == []


def test_the_system_path_of_the_comparison_moves_a_sale_forward_without_a_manager(w: World) -> None:
    sale = w.create(w.manager, direct(ticket(D1), staff_sign="0004SV"), day=YESTERDAY).sale
    moved = sales.move_sale_day(
        w.db,
        w.staff,
        sale.id,
        TODAY,
        "billet trouvé dans le rapport du lendemain",
        system=True,
        now=NOW,
    )
    assert moved.day == TODAY
    assert w.history(sale.id)[1].source == "comparison"


def test_moving_a_sale_needs_the_right_to_edit_any_entry(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1))).sale
    with pytest.raises(SaleForbidden):
        sales.move_sale_day(
            w.db, w.staff, sale.id, YESTERDAY, "je le déplace", source="form", now=NOW
        )
    ghost = User(id=w.staff.id, agency_id=w.agency_id, phone="+22212360009", role="root")
    with pytest.raises(SaleForbidden):
        sales.move_sale_day(
            w.db, ghost, sale.id, YESTERDAY, "je le déplace", source="form", now=NOW
        )
    with pytest.raises(SaleNotFound):
        sales.move_sale_day(w.db, w.other_manager, sale.id, YESTERDAY, "intrusion", now=NOW)
    assert len(w.history(sale.id)) == 1


def test_a_move_is_refused_when_either_day_is_closed(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1))).sale
    w.create(w.manager, direct(ticket(D2), staff_sign="0004SV"), day=YESTERDAY)
    w.close(YESTERDAY)
    with pytest.raises(days.DayClosed):  # the target is closed
        sales.move_sale_day(w.db, w.manager, sale.id, YESTERDAY, "vers la veille", now=NOW)
    w.close(TODAY)
    other = days.get_day(w.db, w.agency_id, YESTERDAY)
    assert other is not None
    days.reopen_day(w.db, w.manager, other, "on rouvre la veille", now=NOW)
    with pytest.raises(days.DayClosed):  # the source is closed
        sales.move_sale_day(w.db, w.manager, sale.id, YESTERDAY, "vers la veille", now=NOW)
    assert len(w.history(sale.id)) == 1


def test_a_move_needs_a_reason_a_real_change_and_an_active_sale(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1))).sale
    for reason in (None, "", "abcd"):
        with pytest.raises(SaleError, match="motif"):
            sales.move_sale_day(w.db, w.manager, sale.id, YESTERDAY, reason, now=NOW)
    with pytest.raises(SaleError, match="déjà"):
        sales.move_sale_day(w.db, w.manager, sale.id, TODAY, "même jour", now=NOW)
    with pytest.raises(days.DayError, match="futur"):
        sales.move_sale_day(w.db, w.manager, sale.id, TODAY + timedelta(days=1), "demain", now=NOW)
    w.void(w.staff, sale.id)
    with pytest.raises(SaleError, match="annul"):
        sales.move_sale_day(w.db, w.manager, sale.id, YESTERDAY, "trop tard", now=NOW)


def test_a_moved_sale_is_then_edited_on_its_new_day_with_that_days_fee(w: World) -> None:
    partners.set_fee(w.db, w.badi, Decimal("300"), date(2026, 10, 15), w.manager, now=NOW)
    sale = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale
    assert sale.pb == Decimal("1300.00")
    sales.move_sale_day(
        w.db, w.manager, sale.id, date(2026, 10, 10), "ancien billet", source="form", now=NOW
    )
    reset = replace(
        partner_sale(w.badi.id, ticket(D1, "1000")), reset_fee=True, staff_sign="0004SV"
    )
    result = w.edit(w.manager, sale.id, reset, "tarif du jour")
    assert result.sale.pb == Decimal("1500.00") and result.sale.day == date(2026, 10, 10)


# --- what B8 reads --------------------------------------------------------------------------


def test_ticket_rows_flatten_the_sales_of_a_day(w: World) -> None:
    two = w.create(
        w.staff, direct(ticket(D1, "10000"), ticket(D2, "11000", airline="TU"), pb="24000")
    ).sale
    one = w.create(w.staff2, partner_sale(w.badi.id, ticket(D3, "1000"))).sale
    gone = w.create(w.staff, direct(ticket(D4))).sale
    w.void(w.staff, gone.id)
    views = sales.sales_for_day_for(w.db, w.manager, TODAY, include_voided=True)
    rows = sales.ticket_rows(views)
    assert [(r.doc_number, r.sale_id, r.position) for r in rows] == [
        (D1, two.id, 1),
        (D2, two.id, 2),
        (D3, one.id, 1),
    ]
    first, second, third = rows
    assert (first.pb_line, second.pb_line, first.sale_pb, second.sale_pb) == (
        Decimal("24000.00"),
        Decimal("0.00"),
        Decimal("24000.00"),
        Decimal("24000.00"),
    )
    assert (second.airline, second.total_doc, second.staff_sign) == (
        "TU",
        Decimal("11000.00"),
        "0004SV",
    )
    assert (second.pax_name, second.pax_type, second.rloc, second.trnc) == (
        "SYNTH01/PAX",
        "ADT",
        "ABC123",
        "TKTT",
    )
    assert (third.pb_line, third.staff_sign) == (Decimal("1500.00"), "0006AC")
    assert sum(r.pb_line for r in rows) == sales.day_totals(views).revenue


def test_active_ticket_lookup_finds_the_sale_and_day_of_each_active_ticket(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale
    past = w.create(w.manager, direct(ticket(D3), staff_sign="0004SV"), day=YESTERDAY).sale
    gone = w.create(w.staff, direct(ticket(D4))).sale
    w.void(w.staff, gone.id)
    found = sales.active_ticket_lookup(w.db, w.agency_id, [D1, D2, D3, D4, "3999999999"])
    assert found == {
        D1: (sale.id, sale.report_day_id),
        D2: (sale.id, sale.report_day_id),
        D3: (past.id, past.report_day_id),
    }
    assert sales.active_ticket_lookup(w.db, w.agency_id, []) == {}
    assert sales.active_ticket_lookup(w.db, w.other_agency_id, [D1]) == {}
    many = [f"{3950000000 + n}" for n in range(1200)]
    assert sales.active_ticket_lookup(w.db, w.agency_id, [*many, D1]) == {D1: found[D1]}


# --- consistency ----------------------------------------------------------------------------


def test_a_healthy_history_has_no_consistency_problem(w: World) -> None:
    a = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale
    w.edit(w.staff, a.id, direct(ticket(D2), ticket(D3), pb="30000"), "remplacement")
    b = w.create(w.staff2, direct(ticket(D4))).sale
    w.void(w.staff2, b.id)
    sales.move_sale_day(w.db, w.manager, a.id, YESTERDAY, "autre jour", source="form", now=NOW)
    assert sales.check_sale_consistency(w.db, w.agency_id) == []
    assert sales.check_sale_consistency(w.db, w.other_agency_id) == []


def test_the_consistency_check_names_each_kind_of_damage(w: World) -> None:
    a = w.create(w.staff, direct(ticket(D1), ticket(D2), pb="30000")).sale
    b = w.create(w.staff2, direct(ticket(D3))).sale
    c = w.create(w.staff2, direct(ticket(D4))).sale
    w.void(w.staff2, c.id)
    ok_ids = {a.id, b.id, c.id}
    assert sales.check_sale_consistency(w.db, w.agency_id) == []
    # a ticket row lost
    w.db.execute(delete(SaleTicket).where(SaleTicket.doc_number == D2))
    # a ticket row that differs from the version
    w.db.execute(
        update(SaleTicket).where(SaleTicket.doc_number == D3).values(doc_number="3940000099")
    )
    # a voided sale that still holds a ticket
    w.db.add(SaleTicket(agency_id=w.agency_id, sale_id=c.id, doc_number=D4, position=1))
    w.db.flush()
    problems = sales.check_sale_consistency(w.db, w.agency_id)
    assert len(problems) == 3
    for sale_id in ok_ids:
        assert sum(f"sale {sale_id}" in p for p in problems) == 1
    # a head pointing at a version that does not exist
    w.db.execute(update(Sale).where(Sale.id == a.id).values(current_version=9))
    assert any(
        f"sale {a.id}" in p and "version" in p
        for p in sales.check_sale_consistency(w.db, w.agency_id)
    )


# --- who may enter sales --------------------------------------------------------------------


def test_a_role_without_a_sales_permission_is_refused_everywhere(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1))).sale
    ghost = User(id=w.staff.id, agency_id=w.agency_id, phone="+22212360008", role="root")
    for call in (
        lambda: sales.create_sale(w.db, ghost, direct(ticket(D2)), now=NOW),
        lambda: sales.edit_sale(
            w.db, ghost, sale.id, direct(ticket(D1), pb="1"), "raison valable", now=NOW
        ),
        lambda: sales.void_sale(w.db, ghost, sale.id, "raison valable", now=NOW),
        lambda: sales.preview_sale(w.db, ghost, direct(ticket(D2)), now=NOW),
    ):
        with pytest.raises(SaleForbidden):
            call()
    assert sales.sales_for_day_for(w.db, ghost, TODAY) == []
    assert sales.get_sale_for(w.db, ghost, sale.id) is None


def test_refused_attempts_are_audited_without_personal_data(w: World) -> None:
    with pytest.raises(SaleForbidden):
        w.create(w.staff, direct(staff_sign="0006AC", clt="Zorglub Synth"))
    with pytest.raises(SaleForbidden):
        w.create(w.unlinked, direct())
    mine = w.create(w.staff, direct(ticket(D1))).sale
    w.db.commit()
    with pytest.raises(SaleForbidden):
        sales.move_sale_day(
            w.db, w.staff, mine.id, YESTERDAY, "raison valable", source="form", now=NOW
        )
    today_day = days.get_day(w.db, w.agency_id, TODAY)
    assert today_day is not None
    with pytest.raises(days.DayError):
        days.close_day(w.db, w.staff, today_day, now=NOW)
    days.close_day(w.db, w.manager, today_day, now=NOW)
    with pytest.raises(days.DayError):
        days.reopen_day(w.db, w.accountant, today_day, "raison valable", now=NOW)
    events = w.audit("permission_denied")
    assert [e.details["action"] for e in events] == [
        "create_sale",
        "create_sale",
        "move_sale_day",
        "close_day",
        "reopen_day",
    ]
    assert all(e.agency_id == w.agency_id and e.actor_user_id for e in events)
    assert "Zorglub" not in repr([e.details for e in events])


def test_a_refused_create_writes_nothing_before_refusing(w: World) -> None:
    """audit.refused commits at once, so services must refuse before their first write."""
    assert days.get_day(w.db, w.agency_id, TODAY) is None
    with pytest.raises(SaleForbidden):
        w.create(w.unlinked, direct())
    w.db.rollback()
    assert days.get_day(w.db, w.agency_id, TODAY) is None
    assert [e.details["action"] for e in w.audit("permission_denied")] == ["create_sale"]


# --- smaller rules --------------------------------------------------------------------------


def test_a_manager_using_a_sign_linked_to_nobody_gets_a_warning(w: World) -> None:
    unknown = w.create(w.manager, direct(ticket(D1), staff_sign="0099ZZ"))
    assert len(unknown.warnings) == 1 and "0099ZZ" in unknown.warnings[0]
    known = w.create(w.manager, direct(ticket(D2), staff_sign="0004SV"))
    assert known.warnings == ()
    edited = w.edit(w.manager, unknown.sale.id, direct(ticket(D1), pb="25000"), "prix revu")
    assert len(edited.warnings) == 1


def test_days_far_in_the_past_and_odd_day_values_are_refused(w: World) -> None:
    long_ago = TODAY - timedelta(days=6 * 366)
    with pytest.raises(days.DayError, match="ancienne"):
        w.create(w.manager, direct(staff_sign="0004SV"), day=long_ago)
    assert w.create(
        w.manager, direct(staff_sign="0004SV"), day=TODAY - timedelta(days=4 * 365)
    ).sale
    with pytest.raises(days.DayError):
        w.create(w.manager, direct(ticket(D2), staff_sign="0004SV"), day=NOW)  # a datetime
    with pytest.raises(days.DayError):
        w.create(w.manager, direct(ticket(D2), staff_sign="0004SV"), day="2026-10-20")


def test_validate_refuses_a_text_instead_of_tickets_and_items_of_the_wrong_type() -> None:
    as_text = SaleInput(tickets="3940000001", pb="1", clt="C", payment_method="cash")  # type: ignore[arg-type]
    assert "tickets" in errors(as_text)
    odd = SaleInput(
        tickets=({"doc_number": D1}, None, ticket(D2)),  # type: ignore[arg-type]
        pb="1",
        clt="C",
        payment_method="cash",
    )
    assert set(errors(odd)) == {"tickets[0]", "tickets[1]"}


def test_an_unknown_kind_does_not_hide_the_other_mistakes() -> None:
    found = errors(
        direct(ticket(doc="x"), ag_kind="other", clt=None, payment_method="nope", ap="1")
    )
    assert set(found) == {"ag_kind", "tickets[0].doc_number", "payment_method", "ap"}


def test_an_override_reason_that_is_too_long_says_so() -> None:
    data = partner_sale(7, fee_override="100", fee_override_reason="x" * 501)
    assert "500" in errors(data, ctx())["fee_override_reason"]
    short = partner_sale(7, fee_override="100", fee_override_reason="abc")
    assert "5" in errors(short, ctx())["fee_override_reason"]


def test_a_sale_of_a_partner_deactivated_since_can_still_be_corrected(w: World) -> None:
    sale = w.create(w.staff, partner_sale(w.badi.id, ticket(D1, "1000"))).sale
    partners.deactivate_partner(w.db, w.manager, w.badi, now=NOW)
    result = w.edit(w.staff, sale.id, partner_sale(w.badi.id, ticket(D1, "1200")), "coût corrigé")
    assert result.sale.pb == Decimal("1700.00")
    with pytest.raises(InvalidSale):  # but no new sale for it
        w.create(w.staff, partner_sale(w.badi.id, ticket(D2)))


def test_an_edit_that_changes_only_a_warning_is_still_no_change(w: World) -> None:
    data = direct(ticket(D1, "21368"), pb="20000")  # below cost: a warning, not content
    sale = w.create(w.staff, data)
    assert len(sale.warnings) == 1
    with pytest.raises(SaleError, match="modif"):
        w.edit(w.staff, sale.sale.id, data)
    assert len(w.history(sale.sale.id)) == 1


def test_a_failed_create_leaves_nothing_after_the_caller_rolls_back(w: World) -> None:
    w.db.commit()
    with pytest.raises(InvalidSale):
        w.create(w.staff, direct(ticket(D1), pb="-1"))
    w.db.rollback()
    assert w.count(Sale) == 0 and days.get_day(w.db, w.agency_id, TODAY) is None


# --- B5 additions: the version the form was built on, and warnings on a stored sale ---------


def test_an_edit_built_on_an_older_version_is_refused_as_stale(w: World) -> None:
    sale = w.create(w.staff, direct(pb="24000")).sale
    w.edit(w.staff, sale.id, direct(pb="25000"))  # version 2, made by somebody else's form
    with pytest.raises(sales.StaleSale, match="rechargez"):
        w.edit(w.staff, sale.id, direct(pb="26000"), expected_version=1)
    assert [v.version_no for v in w.history(sale.id)] == [1, 2]
    again = w.edit(w.staff, sale.id, direct(pb="26000"), expected_version=2)
    assert again.sale.version_no == 3


def test_a_void_built_on_an_older_version_is_refused_as_stale(w: World) -> None:
    sale = w.create(w.staff, direct(pb="24000")).sale
    w.edit(w.staff, sale.id, direct(pb="25000"))
    with pytest.raises(sales.StaleSale, match="rechargez"):
        w.void(w.staff, sale.id, expected_version=1)
    assert sales.get_sale_for(w.db, w.staff, sale.id).status == "active"  # type: ignore[union-attr]
    assert w.void(w.staff, sale.id, expected_version=2).status == "voided"


def test_a_stored_direct_sale_below_cost_still_warns(w: World) -> None:
    low = w.create(w.staff, direct(ticket(D1, "21368"), pb="20000")).sale
    fine = w.create(w.staff, direct(ticket(D2, "1000"), pb="1500")).sale
    assert len(sales.view_warnings(w.db, w.staff, low)) == 1
    assert sales.view_warnings(w.db, w.staff, fine) == ()


def test_a_manager_is_warned_about_a_sign_nobody_has(w: World) -> None:
    mine = w.create(w.manager, direct(ticket(D1), staff_sign="0004SV")).sale
    orphan = w.create(w.manager, direct(ticket(D2), staff_sign="0099ZZ")).sale
    assert sales.view_warnings(w.db, w.manager, mine) == ()
    assert "0099ZZ" in sales.view_warnings(w.db, w.manager, orphan)[0]


def test_a_prefixed_number_is_stored_without_its_prefix_and_caught_as_a_duplicate(w: World) -> None:
    first = w.create(w.staff, direct(ticket("495-3940281465", airline="495"))).sale
    assert [t.doc_number for t in first.tickets] == ["3940281465"]
    assert w.numbers() == {"3940281465"}
    with pytest.raises(DuplicateTicket) as info:
        w.create(w.staff, direct(ticket("3940281465", airline="495")))
    assert info.value.sale_id == first.id and info.value.doc_number == "3940281465"
    with pytest.raises(DuplicateTicket):
        w.create(w.staff, direct(ticket("4953940281465", airline="495")))


def test_the_typed_errors_the_pages_rely_on(w: World) -> None:
    sale = w.create(w.staff, direct()).sale
    with pytest.raises(sales.InvalidReason):
        w.void(w.staff, sale.id, reason="abc")
    with pytest.raises(sales.InvalidReason):
        w.edit(w.manager, sale.id, direct(pb="25000", staff_sign="0004SV"), reason=None)
    w.void(w.staff, sale.id)
    with pytest.raises(sales.SaleVoided):
        w.void(w.staff, sale.id)
    with pytest.raises(sales.SaleVoided):
        w.edit(w.staff, sale.id, direct(pb="26000"))
