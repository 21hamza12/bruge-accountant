"""Group tickets into one price question per (staff sign, booking reference)."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

FLAG_MISSING_COST = "missing_cost"


class TicketLike(Protocol):
    """What the generator needs; satisfied by ``ParsedTicket`` and the ORM ``Ticket``."""

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
    def flags(self) -> Sequence[str]: ...


@dataclass(frozen=True)
class PassengerLine:
    name: str
    pax_type: str
    ticket_no: str
    airline: str
    cost: Decimal | None


@dataclass(frozen=True)
class PriceQuestion:
    booking_ref: str
    airlines: list[str]
    ticket_count: int
    total_cost: Decimal | None
    passengers: list[PassengerLine]
    flags: list[str]


@dataclass(frozen=True)
class StaffQuestions:
    staff_sign: str
    ticket_count: int
    group_count: int
    total_cost: Decimal | None
    questions: list[PriceQuestion]


def _total(costs: Iterable[Decimal | None]) -> Decimal | None:
    total = Decimal(0)
    for cost in costs:
        if cost is None:
            return None
        total += cost
    return total


def _question(booking_ref: str, tickets: list[TicketLike]) -> PriceQuestion:
    ordered = sorted(tickets, key=lambda t: t.ticket_no)
    total = _total(t.cost for t in ordered)
    flags = {flag for t in ordered for flag in t.flags}
    if total is None:
        flags.add(FLAG_MISSING_COST)
    return PriceQuestion(
        booking_ref=booking_ref,
        airlines=sorted({t.airline for t in ordered}),
        ticket_count=len(ordered),
        total_cost=total,
        passengers=[
            PassengerLine(t.passenger_name, t.pax_type, t.ticket_no, t.airline, t.cost)
            for t in ordered
        ],
        flags=sorted(flags),
    )


def build_questions(tickets: Iterable[TicketLike]) -> list[StaffQuestions]:
    """Pure and deterministic: output does not depend on the order of ``tickets``."""
    groups: dict[tuple[str, str], list[TicketLike]] = defaultdict(list)
    for ticket in tickets:
        groups[(ticket.staff_sign, ticket.booking_ref)].append(ticket)

    by_staff: dict[str, list[PriceQuestion]] = defaultdict(list)
    for (staff_sign, booking_ref), members in groups.items():
        by_staff[staff_sign].append(_question(booking_ref, members))

    result: list[StaffQuestions] = []
    for staff_sign in sorted(by_staff):
        questions = sorted(by_staff[staff_sign], key=lambda q: q.booking_ref)
        result.append(
            StaffQuestions(
                staff_sign=staff_sign,
                ticket_count=sum(q.ticket_count for q in questions),
                group_count=len(questions),
                total_cost=_total(q.total_cost for q in questions),
                questions=questions,
            )
        )
    return result
