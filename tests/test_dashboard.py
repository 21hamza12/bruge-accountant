"""The dashboard aggregates: pure functions over tickets, and the two queries that feed them.

The numbers are those of the synthetic fixture ``tests/fixtures/1OCT.xlsx`` (48 tickets, 33
tickets, 1 118 857 in total; the real file has the same airline codes and totals).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.dashboard import (
    CHART_MAX_HEIGHT,
    CHART_MIN_HEIGHT,
    AirlineTotal,
    chart_bars,
    is_cancelled,
    summarize,
)
from app.db import make_engine, make_session_factory
from app.models import Ticket, Upload
from app.parsing.amadeus import ParsedReport
from app.repository import (
    create_agency,
    import_report,
    latest_report_date,
    recent_report_dates,
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
    transaction_type: str = "TKTT"


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


# --- the fixture's numbers ----------------------------------------------------------------------


def test_the_fixture_totals(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    assert s.ticket_count == 48
    assert s.total_cost == Decimal("1118857")
    assert s.missing_cost_count == 0


def test_the_fixture_per_airline_in_descending_cost(parsed_fixture: ParsedReport) -> None:
    s = summarize(fixture_tickets(parsed_fixture))
    assert [(a.code, a.name, a.ticket_count, a.total_cost) for a in s.airlines] == [
        ("495", "Mauritania Airlines", 19, Decimal("336190")),
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
    got = [(a.staff_sign, a.ticket_count, a.total_cost, a.missing_cost_count) for a in s.agents]
    assert got == [
        ("0004SV", 34, Decimal("746423"), 0),
        ("0009ER", 14, Decimal("372434"), 0),
    ]


# --- edge cases ---------------------------------------------------------------------------------


def test_no_tickets_gives_zeroes_not_errors() -> None:
    s = summarize([])
    assert s.ticket_count == 0
    assert s.total_cost == Decimal(0)
    assert s.airlines == () and s.agents == ()
    assert (s.pax.adults, s.pax.children, s.pax.infants) == (0, 0, 0)


def test_a_missing_cost_is_counted_not_summed() -> None:
    s = summarize([T("1", cost=Decimal("50")), T("2", cost=None, booking_ref="BBBBBB")])
    assert s.total_cost == Decimal("50")
    assert s.missing_cost_count == 1
    assert s.airlines[0].total_cost == Decimal("50")


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
    assert s.total_cost == Decimal(1_000_000)


# --- the query ---------------------------------------------------------------------------------


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


def _add_ticket(session: Session, agency_id: int, upload_id: int, day: date, **fields: str) -> None:
    """One awaiting ticket written straight to the table (a hand-made day or booking)."""
    values: dict[str, str] = {
        "ticket_no": "7770000000",
        "staff_sign": "0004SV",
        "booking_ref": "",
        **fields,
    }
    session.add(
        Ticket(
            agency_id=agency_id,
            first_upload_id=upload_id,
            last_upload_id=upload_id,
            report_date=day,
            seq_no="1",
            airline="147",
            cost=Decimal(1),
            passenger_name="X/Y",
            pax_type="ADT",
            transaction_type="TKTT",
            flags=[],
            **values,
        )
    )


def test_the_day_query_never_sees_another_agency(
    session: Session, loaded: int, parsed_fixture: ParsedReport
) -> None:
    other = create_agency(session, "b", "B")
    # The other agency has tickets on the same sign and the same day, but only two bookings.
    refs_taken = sorted({t.booking_ref for t in parsed_fixture.tickets if t.staff_sign == "0004SV"})
    small = replace(
        parsed_fixture,
        tickets=tuple(t for t in parsed_fixture.tickets if t.booking_ref in refs_taken[:2]),
    )
    import_report(session, other.id, small, "b.xlsx", SHA)
    expected_other = summarize(tickets_for_day(session, other.id, DAY))
    assert 0 < expected_other.ticket_count < 48

    rebind_tenant(session, other.id)
    assert len(tickets_for_day(session, other.id, DAY)) == expected_other.ticket_count
    # asking for the first agency's id from the second agency's session is refused, not answered
    with pytest.raises(TenantBindingError):
        tickets_for_day(session, loaded, DAY)

    rebind_tenant(session, loaded)
    assert len(tickets_for_day(session, loaded, DAY)) == 48
    assert (
        session.scalar(select(func.count()).select_from(Ticket)) == 48 + expected_other.ticket_count
    )


def test_the_report_dates_can_be_limited_to_some_signs(session: Session, loaded: int) -> None:
    upload_id = session.scalars(select(Upload.id)).one()
    later = date(2026, 10, 5)
    _add_ticket(session, loaded, upload_id, later, staff_sign="0777ZZ", booking_ref="LATE01")
    session.commit()
    assert latest_report_date(session, loaded) == later
    assert latest_report_date(session, loaded, ["0009ER"]) == DAY
    assert latest_report_date(session, loaded, ["0009ER", "0777ZZ"]) == later
    assert latest_report_date(session, loaded, ["NOPE00"]) is None
    assert latest_report_date(session, loaded, []) is None  # empty means nothing, never "all"
    assert recent_report_dates(session, loaded) == [later, DAY]
    assert recent_report_dates(session, loaded, staff_signs=["0009ER"]) == [DAY]
    assert recent_report_dates(session, loaded, staff_signs=["0777ZZ"]) == [later]
    assert recent_report_dates(session, loaded, staff_signs=[]) == []
    assert recent_report_dates(session, loaded, limit=1) == [later]


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
            rebind_tenant(s, b)
            assert tickets_for_day(s, b, DAY) == []
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


def test_a_cancelled_line_is_recognised_by_its_transaction_type() -> None:
    assert is_cancelled(T("1", transaction_type="CANX"))
    assert not is_cancelled(T("2", transaction_type="TKTT"))
