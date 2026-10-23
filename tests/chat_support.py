"""A world for the chat tests: the flag world (agency A with a manager, an accountant and two staff
members; agency G with a manager) loaded with the made-up 20 October report, a sale for each issued
line except one (a NOT SENT flag) and one with a changed cost (a DIFFERENT flag). Synthetic only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import sales
from app.chat.tools import ChatContext
from app.models import AmadeusLine, Partner, User
from app.sales import SaleInput, SaleView, TicketInput
from app.tenancy import rebind_tenant
from tests.flag_support import D20, NOW, FlagWorld, changed

D22 = date(2026, 10, 22)  # NOW is 22 October 2026, 09:00 UTC
OTHER_DOC = "3940000999"  # a ticket that exists only in the other agency
PHONE = "+22236123456"


@dataclass(frozen=True)
class Scenario:
    not_sent: str  # report line of 0004SV that was never entered (NOT SENT flag)
    different: str  # report line of 0006AC entered with another cost (DIFFERENT flag)
    complete: str  # a ticket of 0004SV entered with the client's phone
    plain: str  # a ticket of 0004SV entered without a phone
    other_agency_doc: str


def _picks(lines: list[AmadeusLine]) -> tuple[AmadeusLine, AmadeusLine, AmadeusLine, AmadeusLine]:
    """``(not_sent, complete, plain, different)``: the report lines the scenario is built on."""
    issued = [x for x in lines if x.trnc == "TKTT"]
    sv = [x for x in issued if x.staff_sign == "0004SV"]
    ac = [x for x in issued if x.staff_sign == "0006AC"]
    return sv[0], sv[1], sv[2], ac[0]


class ChatWorld(FlagWorld):
    """``FlagWorld`` plus the made-up report, its sales and flags, and a foreign sale. Building it
    takes a few seconds: tests get a copy of one prebuilt database (``chat_template``)."""

    scenario: Scenario

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        upload = self.upload()
        lines = self.lines(upload.upload_id)
        not_sent, complete, plain, different = _picks(lines)
        for line in (x for x in lines if x.trnc == "TKTT"):
            if line.doc_number == not_sent.doc_number:
                continue
            ticket = self.ticket_of(line)
            if line.doc_number == different.doc_number:
                ticket = changed(line, total_doc=self.cost_of(line) + 1000)
            self._sell(ticket, line.staff_sign, ap=PHONE if line is complete else None)
        self.run()
        self._foreign_sale()
        rebind_tenant(self.db, self.agency_id)

    @classmethod
    def attach(cls, session: Session) -> ChatWorld:
        """The world already built in this database."""
        self = cls.__new__(cls)
        self.db = session

        def user(phone: str) -> User:
            return session.scalars(select(User).where(User.phone == phone)).one()

        self.manager, self.accountant = user("+22212370001"), user("+22212370002")
        self.staff, self.staff2 = user("+22212370003"), user("+22212370004")
        self.other_manager = user("+22212370005")
        self.agency_id, self.other_agency_id = self.manager.agency_id, self.other_manager.agency_id
        rebind_tenant(session, self.agency_id)
        self.badi = session.scalars(select(Partner).where(Partner.name == "BADI")).one()
        not_sent, complete, plain, different = _picks(
            list(session.scalars(select(AmadeusLine).order_by(AmadeusLine.id)))
        )
        self.scenario = Scenario(
            not_sent.doc_number,
            different.doc_number,
            complete.doc_number,
            plain.doc_number,
            OTHER_DOC,
        )
        return self

    def _sell(self, ticket: TicketInput, sign: str, *, ap: str | None = None) -> SaleView:
        data = SaleInput(
            tickets=[ticket],
            ag_kind="direct",
            pb="99999",
            clt="Client Synth",
            ap=ap,
            payment_method="cash",
            staff_sign=sign,
        )
        result = sales.create_sale(self.db, self.manager, data, day=D20, now=NOW)
        self.db.commit()
        return result.sale

    def sell_today(self, actor: User, doc: str, sign: str, *, ap: str | None = None) -> SaleView:
        """A direct sale of 22 October (the clock's day) by ``actor``, one fresh ticket."""
        ticket = TicketInput(
            doc_number=doc,
            airline="199",
            total_doc=Decimal("1000"),
            pax_name="SYNTH01/PAX",
            pax_type="ADT",
            rloc="ABC123",
            trnc="TKTT",
        )
        data = SaleInput(
            tickets=[ticket],
            ag_kind="direct",
            pb="2000",
            clt="Client Synth",
            ap=ap,
            payment_method="cash",
            staff_sign=sign,
        )
        rebind_tenant(self.db, actor.agency_id)
        result = sales.create_sale(self.db, actor, data, day=D22, now=NOW)
        self.db.commit()
        return result.sale

    def _foreign_sale(self) -> None:
        """A sale in agency G, so that "another agency's anything" has something to be."""
        rebind_tenant(self.db, self.other_agency_id)
        ticket = TicketInput(
            doc_number=OTHER_DOC,
            airline="199",
            total_doc=Decimal("1000"),
            pax_name="SYNTH02/PAX",
            pax_type="ADT",
            rloc="XYZ789",
            trnc="TKTT",
        )
        data = SaleInput(
            tickets=[ticket],
            ag_kind="direct",
            pb="2000",
            clt="Client Synth",
            payment_method="cash",
            staff_sign="0004SV",
        )
        sales.create_sale(self.db, self.other_manager, data, day=D20, now=NOW)
        self.db.commit()

    def close(self, day: date) -> None:
        """Mark a day closed, as the close step would stamp it (no export is stored)."""
        report_day = self.day(day)
        report_day.status = "closed"
        report_day.closed_at = NOW
        report_day.closed_by_user_id = self.manager.id
        self.db.commit()

    def ctx(self, user: User, now: datetime = NOW) -> ChatContext:
        """The chat context of ``user``: the session is bound to the user's own agency."""
        rebind_tenant(self.db, user.agency_id)
        return ChatContext(self.db, user, now)
