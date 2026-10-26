"""View data for the home pages.

Pure functions over tickets (the ORM rows and the parsed tickets both fit), so the numbers can be
tested without a database or a request. The tickets come from ``repository.tickets_for_day``,
already limited to what the viewer may see; nothing here looks at who is asking.

Cancelled lines (TRNC ``CANX``) are not sales: every count and total leaves them out. They are
listed apart (``DaySummary.cancelled``), each with the ticket that reissued it when a live ticket
of the same day has the same booking reference, passenger and amount.

(The old « Questions de prix » grouping of tickets into bookings to price is gone: sales are
entered through « Saisir une vente » and compared with the report.)
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.airlines import airline_name
from app.parsing.amadeus import TRNC_CANCELLED

PAX_ADULT, PAX_CHILD, PAX_INFANT = "ADT", "CHD", "INF"


class TicketLike(Protocol):
    """What the summary needs; satisfied by ``ParsedTicket`` and the ORM ``Ticket``."""

    @property
    def staff_sign(self) -> str: ...
    @property
    def booking_ref(self) -> str: ...
    @property
    def airline(self) -> str: ...
    @property
    def ticket_no(self) -> str: ...
    @property
    def passenger_name(self) -> str: ...
    @property
    def pax_type(self) -> str: ...
    @property
    def cost(self) -> Decimal | None: ...
    @property
    def transaction_type(self) -> str: ...


def is_cancelled(ticket: TicketLike) -> bool:
    """A cancelled document (TRNC ``CANX``): stored, but never counted or totalled.

    Relies on ``transaction_type`` being stored upper case (the parser and the wire both
    normalise it), as do the SQL filters of ``app.repository``."""
    return ticket.transaction_type == TRNC_CANCELLED


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
class AgentTotal:
    staff_sign: str
    ticket_count: int
    total_cost: Decimal  # the known costs only
    missing_cost_count: int


@dataclass(frozen=True)
class CancelledTicket:
    """A cancelled line of the day. ``reissued_as`` is the live ticket that replaced it (same
    booking reference, same amount), or None when no such ticket was issued that day."""

    ticket_no: str
    staff_sign: str
    booking_ref: str
    airline: str
    passenger_name: str
    cost: Decimal | None
    reissued_as: str | None


@dataclass(frozen=True)
class DaySummary:
    ticket_count: int
    total_cost: Decimal  # the known costs only
    missing_cost_count: int
    airlines: tuple[AirlineTotal, ...]  # costliest first
    pax: PaxCounts
    agents: tuple[AgentTotal, ...]  # by staff sign
    cancelled: tuple[CancelledTicket, ...]  # by ticket number; in none of the numbers above

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled)

    @property
    def cancelled_cost(self) -> Decimal:
        """The known amounts of the cancelled lines (shown apart, never in ``total_cost``)."""
        return sum((c.cost for c in self.cancelled if c.cost is not None), Decimal(0))


def agent_totals(tickets: Sequence[TicketLike]) -> tuple[AgentTotal, ...]:
    """One ``AgentTotal`` per staff sign (sorted by sign)."""
    by_sign: dict[str, list[TicketLike]] = defaultdict(list)
    for ticket in tickets:
        by_sign[ticket.staff_sign].append(ticket)
    return tuple(
        AgentTotal(
            staff_sign=sign,
            ticket_count=len(items),
            total_cost=sum((t.cost for t in items if t.cost is not None), Decimal(0)),
            missing_cost_count=sum(1 for t in items if t.cost is None),
        )
        for sign, items in sorted(by_sign.items())
    )


def _airlines(tickets: Sequence[TicketLike]) -> tuple[AirlineTotal, ...]:
    counts: dict[str, int] = defaultdict(int)
    costs: dict[str, Decimal] = defaultdict(lambda: Decimal(0))
    for t in tickets:
        counts[t.airline] += 1
        costs[t.airline] += t.cost if t.cost is not None else Decimal(0)
    ordered = sorted(counts, key=lambda code: (-costs[code], code))
    return tuple(AirlineTotal(c, airline_name(c), counts[c], costs[c]) for c in ordered)


def _reissue_key(ticket: TicketLike) -> tuple[str, str, Decimal] | None:
    """What a cancelled line and its reissue share: the booking, the passenger and the amount.
    None when the reference or the amount is missing, since nothing would tell the reissue apart."""
    if not ticket.booking_ref or ticket.cost is None:
        return None
    return (ticket.booking_ref, ticket.passenger_name, ticket.cost)


def cancelled_tickets(tickets: Iterable[TicketLike]) -> tuple[CancelledTicket, ...]:
    """The cancelled lines of these tickets, by ticket number, each paired with its reissue.

    A cancel and reissue shows as a CANX and a live ticket of the same booking reference, the
    same passenger and the same amount (another passenger of the booking at the same fare is
    not a reissue). Each live ticket replaces at most one cancelled line, the lowest ticket
    numbers pairing first. Pure and independent of the order of ``tickets``.
    """
    ordered = sorted(tickets, key=lambda t: (t.ticket_no, t.staff_sign, t.booking_ref))
    replacements: dict[tuple[str, str, Decimal], deque[str]] = defaultdict(deque)
    for t in ordered:
        key = _reissue_key(t)
        if key is not None and not is_cancelled(t):
            replacements[key].append(t.ticket_no)
    result: list[CancelledTicket] = []
    for t in ordered:
        if not is_cancelled(t):
            continue
        key = _reissue_key(t)
        waiting = replacements.get(key) if key is not None else None
        result.append(
            CancelledTicket(
                ticket_no=t.ticket_no,
                staff_sign=t.staff_sign,
                booking_ref=t.booking_ref,
                airline=t.airline,
                passenger_name=t.passenger_name,
                cost=t.cost,
                reissued_as=waiting.popleft() if waiting else None,
            )
        )
    return tuple(result)


def summarize(tickets: Iterable[TicketLike]) -> DaySummary:
    """Every number of one report day from its tickets: counts, costs (known costs only, with
    the number of missing ones), per-airline and per-agent totals and passenger types, all
    without the cancelled lines, which are listed apart. Pure and independent of
    the order of ``tickets``; no tickets gives zeroes."""
    everything = list(tickets)
    items = [t for t in everything if not is_cancelled(t)]
    pax_types = [t.pax_type for t in items]
    return DaySummary(
        ticket_count=len(items),
        total_cost=sum((t.cost for t in items if t.cost is not None), Decimal(0)),
        missing_cost_count=sum(1 for t in items if t.cost is None),
        airlines=_airlines(items),
        pax=PaxCounts(
            adults=pax_types.count(PAX_ADULT),
            children=pax_types.count(PAX_CHILD),
            infants=pax_types.count(PAX_INFANT),
        ),
        agents=agent_totals(items),
        cancelled=cancelled_tickets(everything),
    )


@dataclass(frozen=True)
class ChartBar:
    """One bar of the cost chart. ``height`` and ``top`` are percentages of the plot area, written
    to the SVG as attributes (the CSP forbids style attributes)."""

    airline: AirlineTotal
    height: float
    top: float


CHART_MAX_HEIGHT = 82.0  # the tallest bar; the rest of the plot holds its value label
CHART_MIN_HEIGHT = 2.0  # a bar for a small, non-empty airline stays visible


def chart_bars(airlines: Sequence[AirlineTotal]) -> tuple[ChartBar, ...]:
    """Bar sizes proportional to each airline's cost, the costliest at ``CHART_MAX_HEIGHT``."""
    biggest = max((a.total_cost for a in airlines), default=Decimal(0))
    bars: list[ChartBar] = []
    for airline in airlines:
        share = float(airline.total_cost / biggest) if biggest > 0 else 0.0
        height = round(max(CHART_MIN_HEIGHT, share * CHART_MAX_HEIGHT), 2)
        bars.append(ChartBar(airline, height, round(100 - height, 2)))
    return tuple(bars)
