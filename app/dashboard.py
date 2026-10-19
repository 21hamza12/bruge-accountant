"""View data for the home pages and the questions page.

Pure functions over tickets (the ORM rows and the parsed tickets both fit), so the numbers can be
tested without a database or a request. The tickets come from ``repository.tickets_for_day``,
already limited to what the viewer may see; nothing here looks at who is asking.

A *booking* is one price question of ``app.questions`` (a staff sign plus a booking reference; a
ticket without a reference is a booking of its own). It is *confirmed* when every one of its
tickets has left the awaiting-price status, which cannot happen before Phase 3 but is already
counted correctly.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol

from app.airlines import airline_name
from app.models import PRICE_STATUS_AWAITING
from app.questions import PriceQuestion, TicketLike, build_questions

STATUS_AWAITING: Final = "awaiting"
STATUS_CONFIRMED: Final = "confirmed"
STATUS_ALL: Final = "all"

PAX_ADULT, PAX_CHILD, PAX_INFANT = "ADT", "CHD", "INF"


class DayTicket(TicketLike, Protocol):
    @property
    def price_status(self) -> str: ...


@dataclass(frozen=True)
class Booking:
    staff_sign: str
    question: PriceQuestion
    confirmed: bool


@dataclass(frozen=True)
class AirlineTotal:
    code: str
    name: str
    ticket_count: int
    total_cost: Decimal  # the known costs only


@dataclass(frozen=True)
class PaxCounts:
    adults: int
    children: int
    infants: int


@dataclass(frozen=True)
class AgentGroup:
    staff_sign: str
    booking_count: int
    confirmed_count: int
    ticket_count: int
    total_cost: Decimal  # the known costs only
    missing_cost_count: int
    bookings: tuple[Booking, ...]


@dataclass(frozen=True)
class DaySummary:
    ticket_count: int
    booking_count: int
    confirmed_count: int
    awaiting_count: int
    total_cost: Decimal  # the known costs only
    missing_cost_count: int
    airlines: tuple[AirlineTotal, ...]  # costliest first
    pax: PaxCounts
    agents: tuple[AgentGroup, ...]  # by staff sign
    bookings: tuple[Booking, ...]

    def top_waiting(self, count: int) -> tuple[Booking, ...]:
        """The ``count`` bookings still awaiting a price that cost the most."""
        waiting = [b for b in self.bookings if not b.confirmed]
        waiting.sort(
            key=lambda b: (
                -b.question.known_cost_total,
                b.question.booking_ref,
                b.question.passengers[0].ticket_no,
            )
        )
        return tuple(waiting[:count])


def build_bookings(tickets: Iterable[DayTicket]) -> list[Booking]:
    """Every booking of these tickets, by staff sign then booking reference."""
    items = list(tickets)
    settled = {t.ticket_no for t in items if t.price_status != PRICE_STATUS_AWAITING}
    return [
        Booking(group.staff_sign, q, all(p.ticket_no in settled for p in q.passengers))
        for group in build_questions(items)
        for q in group.questions
    ]


def group_by_agent(bookings: Sequence[Booking]) -> tuple[AgentGroup, ...]:
    by_sign: dict[str, list[Booking]] = defaultdict(list)
    for booking in bookings:
        by_sign[booking.staff_sign].append(booking)
    return tuple(
        AgentGroup(
            staff_sign=sign,
            booking_count=len(items),
            confirmed_count=sum(1 for b in items if b.confirmed),
            ticket_count=sum(b.question.ticket_count for b in items),
            total_cost=sum((b.question.known_cost_total for b in items), Decimal(0)),
            missing_cost_count=sum(b.question.missing_cost_count for b in items),
            bookings=tuple(items),
        )
        for sign, items in sorted(by_sign.items())
    )


def _airlines(tickets: Sequence[DayTicket]) -> tuple[AirlineTotal, ...]:
    counts: dict[str, int] = defaultdict(int)
    costs: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for t in tickets:
        counts[t.airline] += 1
        costs[t.airline] += t.cost if t.cost is not None else Decimal(0)
    ordered = sorted(counts, key=lambda code: (-costs[code], code))
    return tuple(AirlineTotal(c, airline_name(c), counts[c], costs[c]) for c in ordered)


def summarize(tickets: Iterable[DayTicket]) -> DaySummary:
    items = list(tickets)
    bookings = build_bookings(items)
    pax_types = [t.pax_type for t in items]
    confirmed = sum(1 for b in bookings if b.confirmed)
    return DaySummary(
        ticket_count=len(items),
        booking_count=len(bookings),
        confirmed_count=confirmed,
        awaiting_count=len(bookings) - confirmed,
        total_cost=sum((t.cost for t in items if t.cost is not None), Decimal(0)),
        missing_cost_count=sum(1 for t in items if t.cost is None),
        airlines=_airlines(items),
        pax=PaxCounts(
            adults=pax_types.count(PAX_ADULT),
            children=pax_types.count(PAX_CHILD),
            infants=pax_types.count(PAX_INFANT),
        ),
        agents=group_by_agent(bookings),
        bookings=tuple(bookings),
    )


def matches_search(booking: Booking, query: str | None) -> bool:
    """Case-insensitive substring over the booking reference, passenger names and ticket numbers.

    Plain text matching in Python: the query is never a pattern and never reaches SQL.
    """
    needle = (query or "").strip().casefold()
    if not needle:
        return True
    q = booking.question
    if needle in q.booking_ref.casefold():
        return True
    return any(
        needle in p.name.casefold() or needle in p.ticket_no.casefold() for p in q.passengers
    )


def filter_bookings(
    bookings: Iterable[Booking], status: str = STATUS_ALL, query: str | None = None
) -> list[Booking]:
    wanted = {STATUS_AWAITING: (False,), STATUS_CONFIRMED: (True,)}.get(status, (False, True))
    return [b for b in bookings if b.confirmed in wanted and matches_search(b, query)]
