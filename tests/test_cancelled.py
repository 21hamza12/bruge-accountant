"""Cancelled Amadeus lines (TRNC = CANX): stored, but never counted.

The accountant removes every CANX line before working on a day, so a CANX is not a sale: it adds
nothing to the cost, the ticket count, the airline and agent totals or the list of tickets
to enter. It is still imported (no data is dropped) and shown apart, as a cancelled count.

A CANX whose booking reference and amount match a ticket issued the same day is a cancel and
reissue: the line is listed as cancelled with the ticket that replaced it.

Synthetic day used below (names are made up):

=========  ======  ==========  =====  ======  ===============================================
sign       PNR     ticket      TRNC   cost    note
=========  ======  ==========  =====  ======  ===============================================
0004SV     CNX001  3940000100  CANX   50 000  reissued as 3940000101 (same PNR, same amount)
0004SV     CNX001  3940000101  TKTT   50 000
0004SV     CNX002  3940000102  CANX   30 000  the whole booking was cancelled
0009ER     CNX003  1470000103  TKTT   20 000
0009ER     CNX003  1470000104  TKTT   10 000
0009ER     CNX003  1470000105  canx    7 000  a different amount: not a reissue
=========  ======  ==========  =====  ======  ===============================================

Counted: 3 tickets, 80 000. Cancelled: 3 lines, 87 000.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dashboard import cancelled_tickets, is_cancelled, summarize
from app.models import Ticket, Upload
from app.parsing.amadeus import (
    FLAG_CANCELLED,
    FLAG_UNKNOWN_TRNC,
    TRNC_CANCELLED,
    ParsedReport,
    parse_report,
)
from app.repository import (
    create_agency,
    import_report,
    tickets_for_day,
)
from tests.builders import build_workbook, ticket_row
from tests.conftest import SHA
from tests.page_support import DAY as DAY_TEXT
from tests.page_support import Env, fr
from tests.web_support import WebDb

DAY = date(2026, 10, 1)
OTHER_SHA = "b" * 64  # a second upload; tests.conftest.SHA is the first


def _row(seq: str, doc: str, cost: int, name: str, rloc: str, **kw: Any) -> list[Any]:
    return ticket_row(seq=seq, doc=doc, cost=cost, name=name, rloc=rloc, **kw)


ROWS: list[list[Any]] = [
    _row("1", "3940000100", 50000, "ALPHA/ONE", "CNX001", trnc="CANX"),
    _row("2", "3940000101", 50000, "ALPHA/ONE", "CNX001"),
    _row("3", "3940000102", 30000, "BRAVO/TWO", "CNX002", trnc="CANX"),
    _row("4", "1470000103", 20000, "CHARLIE/THREE", "CNX003", airline="147", staff="0009ER"),
    _row("5", "1470000104", 10000, "CHARLIE/FOUR", "CNX003", airline="147", staff="0009ER"),
    _row(
        "6",
        "1470000105",
        7000,
        "CHARLIE/FIVE",
        "CNX003",
        airline="147",
        staff="0009ER",
        trnc="canx",
    ),
]
CANCELLED_NOS = {"3940000100", "3940000102", "1470000105"}


def workbook() -> bytes:
    return build_workbook(ROWS)


@pytest.fixture
def parsed() -> ParsedReport:
    return parse_report(workbook())


@dataclass(frozen=True)
class T:
    """A ticket-shaped value, as in ``tests/test_dashboard.py``, with its transaction type."""

    ticket_no: str
    transaction_type: str = "TKTT"
    staff_sign: str = "0004SV"
    booking_ref: str = "AAAAAA"
    airline: str = "199"
    passenger_name: str = "DOE/JANE MRS"
    pax_type: str = "ADT"
    cost: Decimal | None = Decimal(100)
    flags: tuple[str, ...] = field(default_factory=tuple)


def canx(ticket_no: str, **kw: Any) -> T:
    return T(ticket_no, transaction_type=TRNC_CANCELLED, **kw)


def as_day_tickets(parsed: ParsedReport) -> list[T]:
    return [
        T(
            ticket_no=t.ticket_no,
            transaction_type=t.transaction_type,
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


# --- the parser keeps them, marked ---------------------------------------------------------------


def test_the_parser_keeps_every_cancelled_line(parsed: ParsedReport) -> None:
    assert len(parsed.tickets) == len(ROWS)
    assert {t.ticket_no for t in parsed.tickets if is_cancelled(t)} == CANCELLED_NOS


def test_a_cancelled_line_is_flagged_cancelled_not_unknown(parsed: ParsedReport) -> None:
    by_no = {t.ticket_no: t for t in parsed.tickets}
    for no in CANCELLED_NOS:
        assert by_no[no].transaction_type == TRNC_CANCELLED  # lower case "canx" is normalised
        assert FLAG_CANCELLED in by_no[no].flags
        assert FLAG_UNKNOWN_TRNC not in by_no[no].flags
    assert by_no["3940000101"].flags == ()


# --- the pure aggregates -------------------------------------------------------------------------


def test_the_day_summary_counts_only_live_tickets(parsed: ParsedReport) -> None:
    s = summarize(as_day_tickets(parsed))
    assert s.ticket_count == 3
    assert s.total_cost == Decimal(80000)
    assert s.missing_cost_count == 0
    assert [(a.code, a.ticket_count, a.total_cost) for a in s.airlines] == [
        ("199", 1, Decimal(50000)),
        ("147", 2, Decimal(30000)),
    ]
    assert [(a.staff_sign, a.ticket_count, a.total_cost) for a in s.agents] == [
        ("0004SV", 1, Decimal(50000)),
        ("0009ER", 2, Decimal(30000)),
    ]
    assert (s.pax.adults, s.pax.children, s.pax.infants) == (3, 0, 0)


def test_the_day_summary_shows_the_cancelled_lines_apart(parsed: ParsedReport) -> None:
    s = summarize(as_day_tickets(parsed))
    assert s.cancelled_count == 3
    assert s.cancelled_cost == Decimal(87000)
    assert [(c.ticket_no, c.reissued_as) for c in s.cancelled] == [
        ("1470000105", None),  # same PNR as two live tickets, but a different amount
        ("3940000100", "3940000101"),
        ("3940000102", None),
    ]


def test_a_day_of_only_cancelled_lines_counts_nothing() -> None:
    s = summarize([canx("1", cost=Decimal(5)), canx("2", cost=None)])
    assert (s.ticket_count, s.missing_cost_count) == (0, 0)
    assert s.total_cost == Decimal(0)
    assert s.airlines == () and s.agents == ()
    assert s.cancelled_count == 2
    assert s.cancelled_cost == Decimal(5)  # the known amounts only


def test_a_missing_cost_on_a_cancelled_line_does_not_make_the_day_partial() -> None:
    s = summarize([T("1"), canx("2", cost=None)])
    assert s.missing_cost_count == 0
    assert s.total_cost == Decimal(100)


def test_a_reissue_needs_the_same_booking_passenger_and_amount() -> None:
    pairs = cancelled_tickets(
        [
            canx("10", booking_ref="PNR001", cost=Decimal(10)),
            T("11", booking_ref="PNR001", cost=Decimal(10)),  # match
            canx("20", booking_ref="PNR002", cost=Decimal(10)),
            T("21", booking_ref="PNR003", cost=Decimal(10)),  # another booking
            canx("30", booking_ref="", cost=Decimal(10)),
            T("31", booking_ref="", cost=Decimal(10)),  # no reference: never paired
            canx("40", booking_ref="PNR004", cost=None),
            T("41", booking_ref="PNR004", cost=None),  # no amount: never paired
            canx("50", booking_ref="PNR005", passenger_name="DOE/ANN", cost=Decimal(10)),
            T("51", booking_ref="PNR005", passenger_name="DOE/BEN", cost=Decimal(10)),  # sibling
            canx("60", booking_ref="PNR006", cost=Decimal("10.00")),
            T("61", booking_ref="PNR006", cost=Decimal(10)),  # 10.00 and 10 are one amount
        ]
    )
    assert [(c.ticket_no, c.reissued_as) for c in pairs] == [
        ("10", "11"),
        ("20", None),
        ("30", None),
        ("40", None),
        ("50", None),
        ("60", "61"),
    ]


def test_one_new_ticket_replaces_only_one_cancelled_line() -> None:
    pairs = cancelled_tickets(
        [
            canx("1", cost=Decimal(10)),
            canx("2", cost=Decimal(10)),
            T("3", cost=Decimal(10)),
            canx("4", cost=Decimal(10)),
            T("5", cost=Decimal(10)),
        ]
    )
    assert [(c.ticket_no, c.reissued_as) for c in pairs] == [("1", "3"), ("2", "5"), ("4", None)]


def test_a_cancelled_line_never_replaces_another() -> None:
    assert [c.reissued_as for c in cancelled_tickets([canx("1"), canx("2")])] == [None, None]


def test_the_cancelled_list_ignores_the_input_order() -> None:
    items = [canx("2"), T("3"), canx("1"), T("4")]
    assert cancelled_tickets(items) == cancelled_tickets(list(reversed(items)))


# --- the database (SQLite here, Postgres when TEST_DATABASE_URL names one) -----------------------


@pytest.fixture
def loaded(session: Session, parsed: ParsedReport) -> int:
    agency = create_agency(session, "a", "A")
    import_report(session, agency.id, parsed, "01OCT.xlsx", SHA)
    return agency.id


def test_cancelled_lines_are_stored(session: Session, loaded: int) -> None:
    stored = {r.ticket_no: r for r in tickets_for_day(session, loaded, DAY)}
    assert len(stored) == len(ROWS)
    for no in CANCELLED_NOS:
        assert stored[no].transaction_type == TRNC_CANCELLED
        assert FLAG_CANCELLED in stored[no].flags


def test_the_stored_day_counts_only_live_tickets(session: Session, loaded: int) -> None:
    s = summarize(tickets_for_day(session, loaded, DAY))
    assert (s.ticket_count, s.total_cost) == (3, Decimal(80000))
    assert (s.cancelled_count, s.cancelled_cost) == (3, Decimal(87000))


def test_a_row_stored_before_the_cancelled_flag_is_still_left_out(
    session: Session, loaded: int
) -> None:
    """Older imports flagged CANX ``unknown_trnc`` only; exclusion keys on the TRNC itself."""
    upload_id = session.scalars(select(Upload.id)).one()
    session.add(
        Ticket(
            agency_id=loaded,
            first_upload_id=upload_id,
            last_upload_id=upload_id,
            report_date=DAY,
            ticket_no="3940000777",
            seq_no="7",
            airline="199",
            cost=Decimal(9000),
            passenger_name="DELTA/OLD",
            pax_type="ADT",
            staff_sign="0777ZZ",
            transaction_type=TRNC_CANCELLED,
            booking_ref="OLD001",
            flags=[FLAG_UNKNOWN_TRNC],
        )
    )
    session.commit()
    assert is_cancelled(
        next(t for t in tickets_for_day(session, loaded, DAY) if t.ticket_no == "3940000777")
    )
    s = summarize(tickets_for_day(session, loaded, DAY))
    assert (s.ticket_count, s.total_cost, s.cancelled_count) == (3, Decimal(80000), 4)


# --- the pages -----------------------------------------------------------------------------------


@pytest.fixture
def env(web_db: WebDb) -> Env:
    env = Env(web_db)
    env.load(data=workbook())
    return env


def test_the_dashboard_counts_live_tickets_and_lists_the_cancelled_ones(env: Env) -> None:
    with env.manager_a() as c:
        html = c.get(f"/?date={DAY_TEXT}").text
    assert fr(80000) in html
    assert fr(80000 + 87000) not in html
    assert "3 annulés" in html
    assert 'id="cancelled-title"' in html
    cancelled = html.split('id="cancelled-title"')[1].split("</section>")[0]
    for no in CANCELLED_NOS:
        assert no in cancelled
    assert "3940000101" in cancelled  # the reissue is named next to the line it replaced


def test_a_staff_member_is_never_told_to_enter_a_cancelled_line(env: Env) -> None:
    env.link(env.seeded.staff_a, "0004SV")
    with env.staff_a() as c:
        home = c.get(f"/?date={DAY_TEXT}").text
    assert fr(50000) in home  # 3940000101 only, not the cancelled 3940000100 and 3940000102
    assert fr(50000 + 50000 + 30000) not in home
    assert "3940000101" in home
    for no in ("3940000100", "3940000102"):
        assert no not in home
