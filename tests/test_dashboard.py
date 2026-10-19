"""The dashboard aggregates: pure functions over tickets, and the two queries that feed them.

The numbers are those of the synthetic fixture ``tests/fixtures/1OCT.xlsx`` (48 tickets, 33
bookings, 1 118 857 in total; the real file has the same airline codes and totals).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dashboard import (
    CHART_MAX_HEIGHT,
    CHART_MIN_HEIGHT,
    STATUS_ALL,
    STATUS_AWAITING,
    STATUS_CONFIRMED,
    AirlineTotal,
    Booking,
    build_bookings,
    chart_bars,
    filter_bookings,
    group_by_agent,
    matches_search,
    summarize,
)
from app.db import make_engine, make_session_factory
from app.models import PRICE_STATUS_AWAITING, Ticket, Upload
from app.parsing.amadeus import ParsedReport
from app.repository import (
    awaiting_counts_by_sign,
    create_agency,
    import_report,
    tickets_for_day,
)
from app.tenancy import TenantBindingError, bind_tenant, rebind_tenant
from tests.conftest import SHA, RlsDatabase

DAY = date(2026, 10, 1)


@dataclass(frozen=True)
class T:
    """A ticket-shaped value (the ORM row and the parsed ticket both look like this)."""

    ticket_no: str
    staff_sign: str = "0004SV"
    booking_ref: str = "AAAAAA"
    airline: str = "147"
    passenger_name: str = "DOE/JANE MRS"
    pax_type: str = "ADT"
    cost: Decimal | None = Decimal("100")
    flags: tuple[str, ...] = field(default_factory=tuple)
    price_status: str = PRICE_STATUS_AWAITING


def fixture_tickets(parsed: ParsedReport) -> list[T]:
    return [
        T(
            ticket_no=t.ticket_no,
            staff_sign=t.staff_sign,
            booking_ref=t.booking_ref,
            airline=t.airline,
            passenger_name=t.passenger_name,
            pax_type=t.pax_type,
            cost=t.cost,
            flags=t.flags,
        )
        for t in parsed.tickets
    ]


def refs(items: object) -> list[str]:
    return sorted(b.question.booking_ref for b in items)  # type: ignore[attr-defined]


# --- the fixture's numbers ----------------------------------------------------------------------


def test_the_fixture_totals(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    assert (s.ticket_count, s.booking_count) == (48, 33)
    assert s.total_cost == Decimal("1118857")
    assert s.missing_cost_count == 0
    assert (s.confirmed_count, s.awaiting_count) == (0, 33)


def test_the_fixture_per_airline_in_descending_cost(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    assert [(a.code, a.name, a.ticket_count, a.total_cost) for a in s.airlines] == [
        ("495", "Cie 495", 19, Decimal("336190")),
        ("147", "Royal Air Maroc", 7, Decimal("266361")),
        ("235", "Turkish Airlines", 6, Decimal("238252")),
        ("199", "Tunisair", 14, Decimal("222125")),
        ("157", "Qatar Airways", 2, Decimal("55929")),
    ]


def test_the_fixture_passenger_types(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    assert (s.pax.adults, s.pax.children, s.pax.infants) == (45, 1, 2)


def test_the_fixture_per_agent(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    got = [
        (a.staff_sign, a.booking_count, a.confirmed_count, a.ticket_count, a.total_cost)
        for a in s.agents
    ]
    assert got == [
        ("0004SV", 25, 0, 34, Decimal("746423")),
        ("0009ER", 8, 0, 14, Decimal("372434")),
    ]


def test_the_top_five_waiting_bookings_are_the_costliest(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    top = s.top_waiting(5)
    assert len(top) == 5
    costs = [b.question.known_cost_total for b in top]
    assert costs == sorted(costs, reverse=True)
    every = sorted((b.question.known_cost_total for b in s.bookings), reverse=True)
    assert costs == every[:5]
    assert all(not b.confirmed for b in top)


# --- edge cases ---------------------------------------------------------------------------------


def test_no_tickets_gives_zeroes_not_errors() -> None:
    s = summarize([])
    assert (s.ticket_count, s.booking_count, s.confirmed_count, s.awaiting_count) == (0, 0, 0, 0)
    assert s.total_cost == Decimal(0)
    assert s.airlines == () and s.agents == () and s.bookings == ()
    assert (s.pax.adults, s.pax.children, s.pax.infants) == (0, 0, 0)
    assert s.top_waiting(5) == ()


def test_a_missing_cost_is_counted_not_summed() -> None:
    s = summarize([T("1", cost=Decimal("50")), T("2", cost=None, booking_ref="BBBBBB")])
    assert s.total_cost == Decimal("50")
    assert s.missing_cost_count == 1
    assert s.airlines[0].total_cost == Decimal("50")


def test_a_booking_is_confirmed_only_when_every_ticket_is() -> None:
    tickets = [
        T("1", booking_ref="AAAAAA", price_status="confirmed"),
        T("2", booking_ref="AAAAAA", price_status="confirmed"),
        T("3", booking_ref="BBBBBB", price_status="confirmed"),
        T("4", booking_ref="BBBBBB"),  # one ticket still waits
        T("5", booking_ref="CCCCCC"),
    ]
    s = summarize(tickets)
    by_ref = {b.question.booking_ref: b.confirmed for b in s.bookings}
    assert by_ref == {"AAAAAA": True, "BBBBBB": False, "CCCCCC": False}
    assert (s.confirmed_count, s.awaiting_count, s.booking_count) == (1, 2, 3)
    agent = s.agents[0]
    assert (agent.confirmed_count, agent.booking_count) == (1, 3)


def test_tickets_without_a_booking_reference_are_their_own_bookings() -> None:
    s = summarize([T("1", booking_ref=""), T("2", booking_ref="")])
    assert s.booking_count == 2


def test_the_airline_order_is_stable_on_ties() -> None:
    s = summarize([T("1", airline="199"), T("2", airline="147", booking_ref="BBBBBB")])
    assert [a.code for a in s.airlines] == ["147", "199"]


def test_an_unknown_pax_type_is_ignored_in_the_split() -> None:
    s = summarize([T("1", pax_type="ADT"), T("2", pax_type="XXX", booking_ref="BBBBBB")])
    assert (s.pax.adults, s.pax.children, s.pax.infants) == (1, 0, 0)
    assert s.ticket_count == 2


def test_the_output_does_not_depend_on_the_input_order(parsed_fixture: ParsedReport) -> None:
    tickets = fixture_tickets(parsed_fixture)
    assert summarize(tickets) == summarize(list(reversed(tickets)))


def test_ten_thousand_tickets_are_summarised() -> None:
    tickets = [T(f"{i:010d}", booking_ref=f"R{i % 3000:05d}") for i in range(10_000)]
    s = summarize(tickets)
    assert s.ticket_count == 10_000
    assert s.booking_count == 3000
    assert s.total_cost == Decimal(1_000_000)


# --- filtering, grouping and search -------------------------------------------------------------


def _bookings() -> list[Booking]:
    return build_bookings(
        [
            T("1", booking_ref="AAAAAA", passenger_name="ALPHA/ONE MR", price_status="confirmed"),
            T("2", booking_ref="BBBBBB", passenger_name="BRAVO/TWO MRS", staff_sign="0009ER"),
            T("3", booking_ref="CCCCCC", passenger_name="CHARLIE/THREE MR"),
        ]
    )


def test_filter_by_status() -> None:
    bookings = _bookings()
    assert refs(filter_bookings(bookings, STATUS_AWAITING)) == ["BBBBBB", "CCCCCC"]
    assert refs(filter_bookings(bookings, STATUS_CONFIRMED)) == ["AAAAAA"]
    assert len(filter_bookings(bookings, STATUS_ALL)) == 3


def test_filter_by_search_across_ref_passenger_and_ticket_number() -> None:
    bookings = _bookings()

    def found(q: str | None) -> list[str]:
        return refs(filter_bookings(bookings, STATUS_ALL, q))

    assert found("bbbb") == ["BBBBBB"]  # booking reference, case-insensitive
    assert found("charlie") == ["CCCCCC"]  # passenger
    assert found("  Three ") == ["CCCCCC"]  # trimmed
    assert found("2") == ["BBBBBB"]  # ticket number
    assert found("nothing like this") == []
    assert len(found("")) == 3
    assert len(found(None)) == 3


def test_search_treats_wildcards_and_quotes_literally() -> None:
    bookings = _bookings()
    assert filter_bookings(bookings, STATUS_ALL, "%") == []
    assert filter_bookings(bookings, STATUS_ALL, "_") == []
    assert not matches_search(bookings[0], "' OR 1=1 --")


def test_group_by_agent_keeps_the_agent_order_and_the_totals() -> None:
    groups = group_by_agent(
        build_bookings(
            [
                T("1", booking_ref="AAAAAA", price_status="confirmed"),
                T("2", booking_ref="BBBBBB"),
                T("3", booking_ref="CCCCCC", staff_sign="0009ER"),
            ]
        )
    )
    assert [g.staff_sign for g in groups] == ["0004SV", "0009ER"]
    sv = groups[0]
    assert (sv.booking_count, sv.confirmed_count, sv.ticket_count) == (2, 1, 2)
    assert sv.total_cost == Decimal(200)
    assert len(sv.bookings) == 2


# --- the queries --------------------------------------------------------------------------------


@pytest.fixture
def loaded(session: Session, parsed_fixture: ParsedReport) -> int:
    agency = create_agency(session, "a", "A")
    import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    return agency.id


def test_tickets_for_day_returns_every_status_in_a_stable_order(
    session: Session, loaded: int
) -> None:
    rows = tickets_for_day(session, loaded, DAY)
    assert len(rows) == 48
    keys = [(r.staff_sign, r.booking_ref, r.ticket_no) for r in rows]
    assert keys == sorted(keys)
    assert summarize(rows).total_cost == Decimal("1118857")


def test_tickets_for_day_can_be_limited_to_some_signs(session: Session, loaded: int) -> None:
    assert len(tickets_for_day(session, loaded, DAY, staff_signs=["0009ER"])) == 14
    assert len(tickets_for_day(session, loaded, DAY, staff_signs=["0009ER", "0004SV"])) == 48
    assert tickets_for_day(session, loaded, DAY, staff_signs=[]) == []
    assert tickets_for_day(session, loaded, DAY, staff_signs=["NOPE00"]) == []
    assert tickets_for_day(session, loaded, date(2026, 9, 1)) == []


def test_tickets_for_day_refuses_another_agency(session: Session, loaded: int) -> None:
    bind_tenant(session, loaded)
    with pytest.raises(TenantBindingError):
        tickets_for_day(session, loaded + 1, DAY)


def test_awaiting_counts_by_sign(session: Session, loaded: int) -> None:
    assert awaiting_counts_by_sign(session, loaded, DAY) == {"0004SV": 25, "0009ER": 8}
    assert awaiting_counts_by_sign(session, loaded, DAY, staff_signs=["0009ER"]) == {"0009ER": 8}
    assert awaiting_counts_by_sign(session, loaded, DAY, staff_signs=[]) == {}
    assert awaiting_counts_by_sign(session, loaded, date(2026, 9, 1)) == {}


def test_awaiting_counts_agree_with_the_grouped_questions(session: Session, loaded: int) -> None:
    upload_id = session.scalars(select(Upload.id)).one()
    session.add_all(
        [
            Ticket(  # two tickets without a booking reference are two questions
                agency_id=loaded,
                first_upload_id=upload_id,
                last_upload_id=upload_id,
                report_date=DAY,
                ticket_no=f"777000000{i}",
                seq_no="1",
                airline="147",
                cost=Decimal(1),
                passenger_name="X/Y",
                pax_type="ADT",
                staff_sign="0004SV",
                transaction_type="TKTT",
                booking_ref="",
                flags=[],
            )
            for i in range(2)
        ]
    )
    session.commit()
    counts = awaiting_counts_by_sign(session, loaded, DAY)
    summary = summarize(tickets_for_day(session, loaded, DAY))
    assert counts == {a.staff_sign: a.booking_count for a in summary.agents}
    assert counts["0004SV"] == 27


def test_the_two_queries_never_see_another_agency(session: Session, loaded: int) -> None:
    other = create_agency(session, "b", "B")
    rebind_tenant(session, other.id)
    assert tickets_for_day(session, other.id, DAY) == []
    assert awaiting_counts_by_sign(session, other.id, DAY) == {}
    rebind_tenant(session, loaded)
    assert session.scalar(select(func.count()).select_from(Ticket)) == 48


def test_the_queries_respect_row_level_security(
    rls_db: RlsDatabase, parsed_fixture: ParsedReport
) -> None:
    owner = make_engine(rls_db.owner_url)
    try:
        with make_session_factory(owner)() as s:
            a = create_agency(s, "a", "A").id
            b = create_agency(s, "b", "B").id
            rebind_tenant(s, a)
            import_report(s, a, parsed_fixture, "1OCT.xlsx", SHA)
    finally:
        owner.dispose()
    app = make_engine(rls_db.app_url)
    try:
        with make_session_factory(app)() as s:
            bind_tenant(s, a)
            assert len(tickets_for_day(s, a, DAY)) == 48
            assert awaiting_counts_by_sign(s, a, DAY) == {"0004SV": 25, "0009ER": 8}
            rebind_tenant(s, b)
            assert tickets_for_day(s, b, DAY) == []
            assert awaiting_counts_by_sign(s, b, DAY) == {}
    finally:
        app.dispose()


# --- the chart ----------------------------------------------------------------------------------


def test_chart_bars_are_proportional_to_the_cost(parsed_fixture: ParsedReport) -> None:
    bars = chart_bars(summarize(fixture_tickets(parsed_fixture)).airlines)
    assert [b.airline.code for b in bars] == ["495", "147", "235", "199", "157"]
    assert bars[0].height == CHART_MAX_HEIGHT
    assert bars[0].top == round(100 - CHART_MAX_HEIGHT, 2)
    ratio = bars[1].height / bars[0].height
    assert abs(ratio - 266361 / 336190) < 0.01
    assert all(b.height + b.top == pytest.approx(100) for b in bars)
    assert [b.height for b in bars] == sorted((b.height for b in bars), reverse=True)


def test_chart_bars_of_nothing_or_of_zero_costs() -> None:
    assert chart_bars([]) == ()
    zero = AirlineTotal("147", "Royal Air Maroc", 1, Decimal(0))
    (bar,) = chart_bars([zero])
    assert bar.height == CHART_MIN_HEIGHT


def test_a_tiny_airline_keeps_a_visible_bar() -> None:
    big = AirlineTotal("147", "Royal Air Maroc", 9, Decimal(1_000_000))
    tiny = AirlineTotal("199", "Tunisair", 1, Decimal(1))
    assert chart_bars([big, tiny])[1].height == CHART_MIN_HEIGHT
