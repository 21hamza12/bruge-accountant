"""Group tickets into one price question per (staff sign, booking reference).

A ticket without a booking reference is never merged with another one: it becomes a
question of its own (shown with ``booking_ref == ""``), flagged ``missing_booking_ref``.
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.parsing.amadeus import FLAG_MISSING_BOOKING_REF, FLAG_MISSING_COST


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
    airlines: tuple[str, ...]
    ticket_count: int
    total_cost: Decimal | None  # None as soon as one ticket has no cost
    known_cost_total: Decimal  # sum of the costs that are known
    missing_cost_count: int
    passengers: tuple[PassengerLine, ...]
    flags: tuple[str, ...]


@dataclass(frozen=True)
class StaffQuestions:
    staff_sign: str
    ticket_count: int
    group_count: int
    total_cost: Decimal | None
    known_cost_total: Decimal
    missing_cost_count: int
    questions: tuple[PriceQuestion, ...]


def _question(booking_ref: str, tickets: list[TicketLike]) -> PriceQuestion:
    ordered = sorted(tickets, key=lambda t: t.ticket_no)
    known = [t.cost for t in ordered if t.cost is not None]
    missing = len(ordered) - len(known)
    known_total = sum(known, Decimal(0))
    flags = {flag for t in ordered for flag in t.flags}
    if missing:
        flags.add(FLAG_MISSING_COST)
    if not booking_ref:
        flags.add(FLAG_MISSING_BOOKING_REF)
    return PriceQuestion(
        booking_ref=booking_ref,
        airlines=tuple(sorted({t.airline for t in ordered})),
        ticket_count=len(ordered),
        total_cost=None if missing else known_total,
        known_cost_total=known_total,
        missing_cost_count=missing,
        passengers=tuple(
            PassengerLine(t.passenger_name, t.pax_type, t.ticket_no, t.airline, t.cost)
            for t in ordered
        ),
        flags=tuple(sorted(flags)),
    )


def build_questions(tickets: Iterable[TicketLike]) -> list[StaffQuestions]:
    """Pure and deterministic: output does not depend on the order of ``tickets``."""
    # Third key part keeps tickets without a booking reference apart from each other.
    groups: dict[tuple[str, str, str], list[TicketLike]] = defaultdict(list)
    for ticket in tickets:
        own_group = "" if ticket.booking_ref else ticket.ticket_no
        groups[(ticket.staff_sign, ticket.booking_ref, own_group)].append(ticket)

    by_staff: dict[str, list[PriceQuestion]] = defaultdict(list)
    for (staff_sign, booking_ref, _), members in groups.items():
        by_staff[staff_sign].append(_question(booking_ref, members))

    result: list[StaffQuestions] = []
    for staff_sign in sorted(by_staff):
        questions = tuple(
            sorted(by_staff[staff_sign], key=lambda q: (q.booking_ref, q.passengers[0].ticket_no))
        )
        missing = sum(q.missing_cost_count for q in questions)
        known_total = sum((q.known_cost_total for q in questions), Decimal(0))
        result.append(
            StaffQuestions(
                staff_sign=staff_sign,
                ticket_count=sum(q.ticket_count for q in questions),
                group_count=len(questions),
                total_cost=None if missing else known_total,
                known_cost_total=known_total,
                missing_cost_count=missing,
                questions=questions,
            )
        )
    return result
