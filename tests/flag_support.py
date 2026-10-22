"""A world for the comparison tests: one agency (plus a second one), a manager, an accountant, two
staff members, and the made-up 20 October report (tests/fixtures/synthetic_20oct.py) with a sale
for each of its issued lines. Everything is synthetic."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import days, flags, partners, sales
from app.ingest import IngestResult, ingest_report
from app.models import AmadeusLine, ComparisonFlag, StaffSignLink, User
from app.repository import create_agency
from app.sales import SaleInput, SaleView, TicketInput
from app.tenancy import rebind_tenant
from tests.builders import build_amadeus_xlsx
from tests.fixtures import synthetic_20oct as fx

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
D20 = date(2026, 10, 20)
D19 = date(2026, 10, 19)
D21 = date(2026, 10, 21)


class FlagWorld:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.agency_id = create_agency(session, "agence-f", "Agence F").id
        self.manager = self._user("+22212370001", "manager")
        self.accountant = self._user("+22212370002", "accountant")
        self.staff = self._user("+22212370003", "staff", "0004SV")
        self.staff2 = self._user("+22212370004", "staff", "0006AC")
        other = create_agency(session, "agence-g", "Agence G").id
        self.other_agency_id = other
        self.other_manager = self._user("+22212370005", "manager", agency_id=other)
        self.badi = partners.create_partner(session, self.manager, "BADI", now=NOW)
        partners.set_fee(
            session, self.badi, Decimal("500"), date(2026, 1, 1), self.manager, now=NOW
        )
        session.commit()
        rebind_tenant(session, self.agency_id)

    def _user(self, phone: str, role: str, *signs: str, agency_id: int | None = None) -> User:
        user = User(agency_id=agency_id or self.agency_id, phone=phone, role=role)
        self.db.add(user)
        self.db.commit()
        for sign in signs:
            self.db.add(StaffSignLink(agency_id=user.agency_id, user_id=user.id, staff_sign=sign))
        self.db.commit()
        return user

    # --- reports -----------------------------------------------------------------------------

    def upload(
        self, data: bytes | None = None, name: str = "20OCT.xlsx", agency_id: int | None = None
    ) -> IngestResult:
        return ingest_report(
            self.db,
            agency_id or self.agency_id,
            fx.build_synthetic_20oct() if data is None else data,
            name,
            uploaded_by=self.manager.id,
        )

    def report(self, lines: Sequence[dict[str, Any]], range_raw: str) -> bytes:
        return build_amadeus_xlsx(lines, range_raw=range_raw, current="22-OCT-2026")

    def changed_20oct(self, doc_index: int, **changes: Any) -> bytes:
        """The synthetic report with line ``doc_index`` (0-based, file order) changed."""
        lines = [dict(line) for line in fx.LINES]
        lines[doc_index].update(changes)
        return self.report(lines, "20OCT")

    def lines(self, upload_id: int) -> list[AmadeusLine]:
        return list(
            self.db.scalars(
                select(AmadeusLine)
                .where(AmadeusLine.agency_id == self.agency_id, AmadeusLine.upload_id == upload_id)
                .order_by(AmadeusLine.seq_order)
            )
        )

    # --- sales -------------------------------------------------------------------------------

    @staticmethod
    def cost_of(line: AmadeusLine) -> Decimal:
        assert line.total_doc is not None
        return line.total_doc

    @staticmethod
    def ticket_of(line: AmadeusLine, **kw: Any) -> TicketInput:
        fields: dict[str, Any] = {
            "doc_number": line.doc_number,
            "airline": line.airline,
            "total_doc": line.total_doc,
            "pax_name": line.pax_name,
            "pax_type": line.pax_type,
            "rloc": line.rloc,
            "trnc": line.trnc,
        }
        fields.update(kw)
        return TicketInput(**fields)

    def sell(
        self,
        tickets: Sequence[TicketInput],
        *,
        sign: str = "0004SV",
        day: date = D20,
        partner: bool = False,
        pb: str = "99999",
    ) -> SaleView:
        data: SaleInput
        if partner:
            data = SaleInput(
                tickets=tickets,
                ag_kind="partner",
                partner_id=self.badi.id,
                payment_method="credit",
                staff_sign=sign,
            )
        else:
            data = SaleInput(
                tickets=tickets,
                ag_kind="direct",
                pb=pb,
                clt="Client Synth",
                payment_method="cash",
                staff_sign=sign,
            )
        result = sales.create_sale(self.db, self.manager, data, day=day, now=NOW)
        self.db.commit()
        return result.sale

    def sell_all_issued(self, upload_id: int, *, skip: Sequence[str] = ()) -> list[SaleView]:
        """One sale per TKTT line of the report (the Amadeus sign), but not the ``skip`` numbers."""
        made: list[SaleView] = []
        for line in self.lines(upload_id):
            if line.trnc == "TKTT" and line.doc_number not in skip:
                made.append(self.sell([self.ticket_of(line)], sign=line.staff_sign))
        return made

    # --- the comparison ----------------------------------------------------------------------

    def day(self, day: date = D20) -> Any:
        found = days.get_day(self.db, self.agency_id, day)
        assert found is not None
        return found

    def run(self, day: date = D20, actor: User | None = None) -> Any:
        report_day = days.get_or_open_day(self.db, self.manager, day, now=NOW)
        run = flags.run_comparison(self.db, actor or self.manager, report_day, now=NOW)
        self.db.commit()
        return run

    def all_flags(self, day: date | None = None) -> list[ComparisonFlag]:
        query = select(ComparisonFlag).where(ComparisonFlag.agency_id == self.agency_id)
        if day is not None:
            query = query.where(ComparisonFlag.report_day_id == self.day(day).id)
        return list(self.db.scalars(query.order_by(ComparisonFlag.id)))

    def live(self, day: date = D20) -> list[ComparisonFlag]:
        return [f for f in self.all_flags(day) if f.status in ("open", "answered", "resolved")]

    def set_status(self, flag_id: int, status: str) -> None:
        """Stand-in for B9's answering: set a status directly, as the owner."""
        row = self.db.get(ComparisonFlag, flag_id)
        assert row is not None
        row.status = status
        self.db.commit()


def changed(line: AmadeusLine, **kw: Any) -> TicketInput:
    return replace(FlagWorld.ticket_of(line), **kw)


# --- the web world -----------------------------------------------------------------------------


class FlagWeb:
    """Two agencies seeded for the page tests, a clock on 22 October 2026, the real parser instead
    of the sandbox, and helpers that sell the lines of a report through the service."""

    def __init__(self, web_db: Any) -> None:
        import app.web.routes.uploads as upload_routes
        from app.parsing.amadeus import parse_report
        from tests.web_support import FakeClock, make_client, seed

        self.db = web_db
        self.seeded = seed(web_db.owner_url)
        self.clock = FakeClock(NOW)
        self.factory = make_client(web_db, self.clock)
        self.factory.app.dependency_overrides[upload_routes.get_parser] = lambda: parse_report  # type: ignore[attr-defined]
        self.data = fx.build_synthetic_20oct()

    def client(self, phone: str, password: str) -> Any:
        from tests.web_support import login

        c = self.factory()
        assert login(c, phone, password).status_code == 303
        return c

    def manager(self) -> Any:
        from tests.web_support import PASSWORD_A, PHONE_A

        return self.client(PHONE_A, PASSWORD_A)

    def staff(self) -> Any:
        from tests.web_support import PASSWORD_S, PHONE_STAFF_A

        return self.client(PHONE_STAFF_A, PASSWORD_S)

    def other_manager(self) -> Any:
        from tests.web_support import PASSWORD_B, PHONE_B

        return self.client(PHONE_B, PASSWORD_B)

    def link_staff(self, *signs: str) -> None:
        from tests.web_support import owner_session

        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.seeded.agency_a)
            for sign in signs:
                s.add(
                    StaffSignLink(
                        agency_id=self.seeded.agency_a,
                        user_id=self.seeded.staff_a,
                        staff_sign=sign,
                    )
                )
            s.commit()

    def upload(self, c: Any, data: bytes | None = None, name: str = "20OCT.xlsx") -> Any:
        from tests.web_support import csrf_for

        xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return c.post(
            "/uploads",
            data={"csrf_token": csrf_for(c, "/uploads/new")},
            files={"file": (name, self.data if data is None else data, xlsx)},
            follow_redirects=False,
        )

    def sell_report(
        self,
        data: bytes | None = None,
        *,
        skip: Sequence[str] = (),
        extra_cost: dict[str, int] | None = None,
        day: date = D20,
    ) -> None:
        """A direct sale for each TKTT line of the report, as the manager of agency A."""
        from app.parsing.amadeus import parse_report
        from tests.web_support import owner_session

        report = parse_report(self.data if data is None else data)
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.seeded.agency_a)
            manager = s.get(User, self.seeded.manager_a)
            assert manager is not None
            for t in report.tickets:
                if t.transaction_type != "TKTT" or t.ticket_no in skip:
                    continue
                cost = (t.cost or Decimal(0)) + (extra_cost or {}).get(t.ticket_no, 0)
                ticket = TicketInput(
                    doc_number=t.ticket_no,
                    airline=t.airline,
                    total_doc=cost,
                    pax_name=t.passenger_name,
                    pax_type=t.pax_type,
                    rloc=t.booking_ref,
                    trnc=t.transaction_type,
                )
                sales.create_sale(
                    s,
                    manager,
                    SaleInput(
                        tickets=[ticket],
                        ag_kind="direct",
                        pb="99999",
                        clt="Client Synth",
                        payment_method="cash",
                        staff_sign=t.staff_sign,
                    ),
                    day=day,
                    now=NOW,
                )
            s.commit()

    def flags_rows(self, agency_id: int | None = None) -> list[ComparisonFlag]:
        from tests.web_support import owner_session

        agency = agency_id or self.seeded.agency_a
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency)
            rows = list(
                s.scalars(
                    select(ComparisonFlag)
                    .where(ComparisonFlag.agency_id == agency)
                    .order_by(ComparisonFlag.id)
                )
            )
            s.expunge_all()
            return rows

    def set_all_status(self, status: str) -> None:
        from tests.web_support import owner_session

        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.seeded.agency_a)
            for row in s.scalars(select(ComparisonFlag)):
                row.status = status
            s.commit()
