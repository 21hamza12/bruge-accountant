"""app/partner_refunds.py: RFND lines proposed from the Amadeus report (phase F, slice F4).

The report is imported and compared through the real path (``ingest_report``, then
``flags.run_comparison``), on SQLite. Synthetic numbers and made-up names only
("Partenaire Test", "Partenaire Deux")."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, partner_book, partner_refunds, partners, sales
from app.models import AuditEvent, ComparisonRun, Partner, PartnerEntry, ReportDay
from app.partner_refunds import proposal_date, propose_refunds, request_key
from app.sales import SaleInput, TicketInput
from tests.builders import amadeus_line
from tests.flag_support import NOW, FlagWorld

D3 = date(2026, 10, 3)
D14 = date(2026, 10, 14)
D16 = date(2026, 10, 16)
D20 = date(2026, 10, 20)
OCT_1 = date(2026, 10, 1)
A_DOC, DIRECT_DOC, B_DOC, POSITIVE_DOC, EMPTY_DOC = (
    "1990000001",
    "1990000002",
    "1990000003",
    "1990000004",
    "1990000005",
)


def _ticket(doc: str, cost: str = "21368") -> TicketInput:
    return TicketInput(
        doc_number=doc,
        airline="199",
        total_doc=cost,
        pax_name="SYNTH01/PAX",
        pax_type="ADT",
        rloc="ABC123",
        trnc="TKTT",
    )


class RefundWorld(FlagWorld):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.partner_a = self._partner("Partenaire Test")
        self.partner_b = self._partner("Partenaire Deux")

    def _partner(self, name: str) -> Partner:
        partner = partners.create_partner(self.db, self.manager, name, now=NOW)
        partners.set_fee(self.db, partner, Decimal("300"), date(2026, 1, 1), self.manager, now=NOW)
        self.db.commit()
        return partner

    def partner_sale(self, partner: Partner, doc: str, day: date = D3) -> int:
        data = SaleInput(
            tickets=(_ticket(doc),),
            ag_kind="partner",
            partner_id=partner.id,
            payment_method="credit",
            staff_sign="0004SV",
        )
        result = sales.create_sale(self.db, self.manager, data, day=day, now=NOW)
        self.db.commit()
        return result.sale.id

    def direct_sale(self, doc: str, day: date = D3) -> int:
        data = SaleInput(
            tickets=(_ticket(doc),),
            ag_kind="direct",
            pb="25000",
            clt="Client Synth",
            payment_method="cash",
            staff_sign="0004SV",
        )
        result = sales.create_sale(self.db, self.manager, data, day=day, now=NOW)
        self.db.commit()
        return result.sale.id

    def close_days(self) -> None:
        for row in self.db.scalars(select(ReportDay).where(ReportDay.agency_id == self.agency_id)):
            if row.status != "closed":
                row.status, row.closed_at, row.closed_by_user_id = "closed", NOW, self.manager.id
        self.db.commit()

    def refund_report(self, lines: list[dict[str, Any]], range_raw: str = "20OCT") -> bytes:
        return self.report(lines, range_raw)

    def compare(self, day: date = D20) -> list[PartnerEntry]:
        """What the upload and « Relancer » do: the comparison, committed, then the proposals in
        a transaction of their own."""
        self.run(day)
        return partner_refunds.propose_after_comparison(self.db, self.manager, day, now=NOW)

    def import_and_compare(self, data: bytes, day: date = D20) -> None:
        self.upload(data, name=f"{day.isoformat()}.xlsx")
        self.compare(day)

    def proposals(self) -> list[PartnerEntry]:
        return list(
            self.db.scalars(
                select(PartnerEntry)
                .where(PartnerEntry.agency_id == self.agency_id, PartnerEntry.source == "amadeus")
                .order_by(PartnerEntry.id)
            )
        )


def refund(doc: str, total: Any, seq: str = "000001") -> dict[str, Any]:
    return amadeus_line(seq=seq, doc=doc, total=total, trnc="RFND", airline="199")


@pytest.fixture
def w(session: Session) -> RefundWorld:
    return RefundWorld(session)


@pytest.fixture
def mixed(w: RefundWorld) -> RefundWorld:
    """Sales filed on 3 October (days then closed): partner A, a direct sale, partner B, two more
    of partner A; a 20 October report with an RFND line for each."""
    w.partner_sale(w.partner_a, A_DOC)
    w.direct_sale(DIRECT_DOC)
    w.partner_sale(w.partner_b, B_DOC)
    w.partner_sale(w.partner_a, POSITIVE_DOC)
    w.partner_sale(w.partner_a, EMPTY_DOC)
    w.close_days()
    return w


def mixed_report(w: RefundWorld) -> bytes:
    return w.refund_report(
        [
            refund(A_DOC, -2505, "000001"),
            refund(DIRECT_DOC, -1000, "000002"),
            refund(B_DOC, "-750.50", "000003"),
            refund(POSITIVE_DOC, 1200, "000004"),
            refund(EMPTY_DOC, None, "000005"),
            refund("1990000099", -300, "000006"),  # no sale holds it
        ]
    )


# --- on import ----------------------------------------------------------------------------------


def test_an_rfnd_of_a_partner_ticket_is_proposed_on_import(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    found = {(e.partner_id, e.doc_number): e for e in w.proposals()}
    assert set(found) == {(w.partner_a.id, A_DOC), (w.partner_b.id, B_DOC)}
    a = found[(w.partner_a.id, A_DOC)]
    assert (a.kind, a.source, a.amount, a.entry_date) == ("RFND", "amadeus", Decimal("-2505"), D20)
    assert a.request_key == request_key(A_DOC) == "amadeus-rfnd-1990000001"
    assert a.created_by_user_id == w.manager.id and A_DOC in a.reason
    assert found[(w.partner_b.id, B_DOC)].amount == Decimal("-750.50")
    events = list(
        w.db.scalars(select(AuditEvent).where(AuditEvent.event_type == partner_refunds.PROPOSED))
    )
    assert len(events) == 2
    assert set(events[0].details) == {
        "partner_id",
        "entry_id",
        "sale_id",
        "report_day_id",
        "amount",
        "entry_date",
    }


def test_a_direct_sale_or_an_unknown_ticket_proposes_nothing(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    docs = {e.doc_number for e in w.proposals()}
    assert DIRECT_DOC not in docs and "1990000099" not in docs


def test_another_partners_ticket_goes_to_that_partner_only(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    by_partner = {(e.partner_id, e.doc_number) for e in w.proposals()}
    assert (w.partner_a.id, B_DOC) not in by_partner
    assert (w.partner_b.id, A_DOC) not in by_partner


def test_a_missing_or_positive_amount_proposes_nothing_and_logs_ids_only(
    mixed: RefundWorld, caplog: pytest.LogCaptureFixture
) -> None:
    w = mixed
    with caplog.at_level(logging.WARNING, logger="app.partner_refunds"):
        w.import_and_compare(mixed_report(w))
    docs = {e.doc_number for e in w.proposals()}
    assert POSITIVE_DOC not in docs and EMPTY_DOC not in docs
    skipped = [r for r in caplog.records if r.getMessage() == "amadeus refund not proposed"]
    assert len(skipped) == 2
    for record in skipped:
        assert record.why == "amount"  # type: ignore[attr-defined]
        assert isinstance(record.sale_id, int)  # type: ignore[attr-defined]
        assert POSITIVE_DOC not in record.getMessage() and "1200" not in str(record.__dict__)


def test_reimporting_and_recomparing_never_proposes_twice(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    first = [(e.id, e.doc_number) for e in w.proposals()]
    w.import_and_compare(mixed_report(w))  # the same bytes again
    w.compare(D20)  # and the comparison once more
    changed = w.refund_report([refund(A_DOC, -9999, "000001")])  # a new upload, another amount
    w.import_and_compare(changed)
    assert [(e.id, e.doc_number) for e in w.proposals()] == first


def test_a_rejected_proposal_is_never_proposed_again(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    entry = next(e for e in w.proposals() if e.doc_number == A_DOC)
    partner_book.decide_line(
        w.db, w.accountant, w.partner_a.id, entry.id, decision="reject", note="déjà remboursé"
    )
    w.db.commit()
    w.compare(D20)
    assert [e.doc_number for e in w.proposals()].count(A_DOC) == 1


def test_the_database_key_holds_one_proposal_per_ticket(
    mixed: RefundWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two comparisons racing: both checks before the insert miss the other's row, the second
    insert hits the unique request key, its savepoint is rolled back and the comparison goes on
    (the outer transaction still commits)."""
    w = mixed
    lines = [_Line(A_DOC, "199", "RFND", Decimal("-2505"))]
    first = propose_refunds(w.db, w.manager, D20, lines, now=NOW)
    w.db.commit()
    real = partner_refunds._entry_by_key
    calls: list[str] = []

    def blind_once(db: Session, agency_id: int, key: str) -> PartnerEntry | None:
        calls.append(key)
        return None if len(calls) == 1 else real(db, agency_id, key)

    monkeypatch.setattr(partner_refunds, "_already_proposed", lambda *a: set())
    monkeypatch.setattr(partner_refunds, "_entry_by_key", blind_once)
    second = propose_refunds(w.db, w.manager, D20, lines, now=NOW)
    w.db.commit()
    assert len(first) == 1 and second == [] and len(calls) == 2  # checked again after the refusal
    assert len(w.proposals()) == 1


@dataclass(frozen=True)
class _Line:
    doc_number: str
    airline: str
    trnc: str
    total_doc: Decimal | None


# --- the date: never in a closed period ---------------------------------------------------------


@pytest.mark.parametrize(
    ("report_day", "closed_end", "opening", "expected"),
    [
        (D20, None, None, D20),  # nothing closed: the report day
        (D20, date(2026, 10, 15), None, D20),  # after the closed end
        (D14, date(2026, 10, 15), None, NOW.date()),  # inside a closed period: today
        (date(2026, 10, 15), date(2026, 10, 15), None, NOW.date()),  # on the closed end
        (date(2026, 10, 23), None, None, NOW.date()),  # never after today
        (D14, None, date(2026, 10, 16), None),  # before the switch-over day: nothing
    ],
)
def test_the_proposal_date(
    report_day: date, closed_end: date | None, opening: date | None, expected: date | None
) -> None:
    assert proposal_date(report_day, NOW.date(), closed_end, opening) == expected


def test_a_late_report_of_a_closed_period_is_dated_today(w: RefundWorld) -> None:
    w.partner_sale(w.partner_a, A_DOC)
    w.close_days()
    partner_book.close_period(
        w.db, w.accountant, w.partner_a.id, OCT_1, request_key=partner_book.new_request_key(),
        now=NOW,
    )  # fmt: skip
    w.db.commit()
    assert partner_book.latest_closed_end(w.db, w.agency_id, w.partner_a.id) == date(2026, 10, 15)
    w.import_and_compare(w.refund_report([refund(A_DOC, -2505)], "14OCT"), D14)
    (entry,) = w.proposals()
    assert entry.entry_date == NOW.date() > date(2026, 10, 15)


# --- counted only once confirmed ----------------------------------------------------------------


def test_a_proposal_counts_only_once_confirmed(mixed: RefundWorld) -> None:
    w = mixed
    today = NOW.date()
    before = partner_book.account(w.db, w.agency_id, w.partner_a, today).balance
    w.import_and_compare(mixed_report(w))
    pending = partner_book.account(w.db, w.agency_id, w.partner_a, today)
    assert pending.balance == before
    (waiting,) = [e for p in pending.periods for e in p.pending]
    assert waiting.entry.doc_number == A_DOC
    rows = partner_book.account_rows(w.db, w.manager, today)
    row = next(r for r in rows if r.partner.id == w.partner_a.id)
    assert [e.doc_number for e in row.pending_refunds] == [A_DOC]
    partner_book.decide_line(
        w.db, w.accountant, w.partner_a.id, waiting.entry.id, decision="confirm"
    )
    w.db.commit()
    after = partner_book.account(w.db, w.agency_id, w.partner_a, today).balance
    assert after == before - Decimal("2505")
    row = next(
        r
        for r in partner_book.account_rows(w.db, w.manager, today)
        if r.partner.id == w.partner_a.id
    )
    assert row.pending_refunds == ()


def test_proposals_stay_in_the_agency_of_the_report(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    other = list(
        w.db.scalars(select(PartnerEntry).where(PartnerEntry.agency_id == w.other_agency_id))
    )
    assert other == []
    assert all(e.agency_id == w.agency_id for e in w.proposals())


# --- after the comparison, in a transaction of its own (review, 28 Oct) -------------------------


def test_the_comparison_itself_proposes_nothing(mixed: RefundWorld) -> None:
    """The run never takes a partner's advisory lock while it holds the day locks: it writes no
    proposal; the caller proposes once the run is committed."""
    w = mixed
    w.upload(mixed_report(w), name="20OCT.xlsx")
    w.run(D20)
    assert w.proposals() == []
    written = partner_refunds.propose_after_comparison(w.db, w.manager, D20, now=NOW)
    assert {e.doc_number for e in written} == {A_DOC, B_DOC}
    w.db.rollback()  # nothing left to roll back: the proposals were committed
    assert {e.doc_number for e in w.proposals()} == {A_DOC, B_DOC}


def test_a_failed_proposal_leaves_the_comparison_and_logs_ids_only(
    mixed: RefundWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    w = mixed
    w.upload(mixed_report(w), name="20OCT.xlsx")
    run = w.run(D20)

    def boom(*args: Any, **kwargs: Any) -> list[PartnerEntry]:
        raise RuntimeError("Partenaire Test 1990000001 -2505")

    monkeypatch.setattr(partner_refunds, "propose_refunds", boom)
    with caplog.at_level(logging.ERROR, logger="app.partner_refunds"):
        assert partner_refunds.propose_after_comparison(w.db, w.manager, D20, now=NOW) == []
    assert w.db.get(ComparisonRun, run.id) is not None  # the committed run stays
    assert w.day(D20).status in ("compared", "approved")
    (record,) = [r for r in caplog.records if r.getMessage() == "amadeus refunds not proposed"]
    assert record.error == "RuntimeError"  # type: ignore[attr-defined]
    assert "Partenaire" not in str(record.__dict__) and "2505" not in str(record.__dict__)
    monkeypatch.undo()
    assert {e.doc_number for e in w.compare(D20)} == {A_DOC, B_DOC}  # the next run retries


def test_a_day_without_a_report_proposes_nothing(w: RefundWorld) -> None:
    assert partner_refunds.propose_after_comparison(w.db, w.manager, D20, now=NOW) == []


def test_the_audit_event_name_is_registered() -> None:
    assert partner_refunds.PROPOSED == audit.PARTNER_REFUND_PROPOSED == "partner_refund_proposed"
    names = [v for k, v in vars(audit).items() if k.isupper() and isinstance(v, str)]
    assert names.count("partner_refund_proposed") == 1


def test_the_reason_says_it_comes_from_the_amadeus_report(mixed: RefundWorld) -> None:
    w = mixed
    w.import_and_compare(mixed_report(w))
    entry = next(e for e in w.proposals() if e.doc_number == A_DOC)
    assert entry.reason == (
        "Remboursement du billet 1990000001 proposé depuis le rapport Amadeus du 20 octobre 2026 "
        ": montant Amadeus, à vérifier"
    )


# --- a refund or a cancellation never moves a sale (review, 28 Oct) -----------------------------

D14_SALE_DOC = "1990000021"


@pytest.mark.parametrize("trnc", ["RFND", "CANX"])
def test_a_refund_or_cancellation_line_never_moves_a_sale(w: RefundWorld, trnc: str) -> None:
    """A partner sale filed on 14 October (an open day without a report); the 16 October report
    lists its ticket as RFND / CANX: the sale stays on the 14th (its partner period), the line is
    ELSEWHERE, and an RFND is still proposed."""
    sale_id = w.partner_sale(w.partner_a, D14_SALE_DOC, D14)
    w.upload(w.refund_report([refund(D14_SALE_DOC, -2505)], "16OCT") if trnc == "RFND" else
             w.refund_report([amadeus_line(doc=D14_SALE_DOC, total=-2505, trnc="CANX")], "16OCT"),
             name="16OCT.xlsx")  # fmt: skip
    proposed = w.compare(D16)
    view = sales.agency_sales_by_id(w.db, w.agency_id, [sale_id])[0]
    assert view.day == D14
    assert [(f.kind, f.doc_number) for f in w.live(D16)] == [("elsewhere", D14_SALE_DOC)]
    assert [e.doc_number for e in proposed] == ([D14_SALE_DOC] if trnc == "RFND" else [])


@pytest.mark.parametrize("trnc", ["TKTT", "EMDA"])
def test_an_issue_line_still_moves_the_sale(w: RefundWorld, trnc: str) -> None:
    sale_id = w.partner_sale(w.partner_a, D14_SALE_DOC, D14)
    w.upload(
        w.refund_report([amadeus_line(doc=D14_SALE_DOC, total=21368, trnc=trnc)], "16OCT"),
        name="16OCT.xlsx",
    )
    w.compare(D16)
    assert sales.agency_sales_by_id(w.db, w.agency_id, [sale_id])[0].day == D16
