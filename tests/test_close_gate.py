"""The close gate (B9): what must be settled before a day can be closed, and the 20 Oct story."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import approvals, days, flags, sales
from app.models import AuditEvent, ComparisonRun, DayAcknowledgement
from tests.approval_support import (
    answer,
    diff_world,
    partner_override_sale,
    validate,
)
from tests.flag_support import D20, NOW, FlagWorld


@pytest.fixture
def w(session: Session) -> FlagWorld:
    return FlagWorld(session)


def runs(w: FlagWorld) -> int:
    return w.db.scalar(select(func.count()).select_from(ComparisonRun)) or 0


def gate(w: FlagWorld, actor: object = None) -> list[str]:
    reasons = approvals.can_close(w.db, actor or w.accountant, w.day(), now=NOW)  # type: ignore[arg-type]
    w.db.commit()
    return reasons


def clear_day(w: FlagWorld) -> int:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    w.run()
    return upload_id


def test_no_report_blocks_the_close(w: FlagWorld) -> None:
    ticket = sales.TicketInput("3940000001", "199", 100, "SYNTH/PAX", "ADT", "ABCDEF", "TKTT")
    w.sell([ticket], sign="0004SV")
    reasons = gate(w)
    assert reasons == [approvals.GATE_NO_REPORT]


def test_a_clear_day_can_be_closed(w: FlagWorld) -> None:
    clear_day(w)
    assert gate(w) == []
    closed = approvals.close_day(w.db, w.accountant, w.day(), now=NOW)
    w.db.commit()
    assert closed.status == "closed" and closed.closed_by_user_id == w.accountant.id


def test_an_open_blocking_flag_blocks(w: FlagWorld) -> None:
    diff_world(w, "pax_name")
    reasons = gate(w)
    assert len(reasons) == 1 and "écart" in reasons[0] and "1" in reasons[0]
    with pytest.raises(approvals.CloseBlocked) as caught:
        approvals.close_day(w.db, w.accountant, w.day(), now=NOW)
    assert caught.value.reasons == reasons
    assert str(caught.value).startswith("La journée ne peut pas être clôturée")
    w.db.rollback()
    assert w.day().status != "closed"


def test_an_answered_flag_waiting_for_a_validator_blocks(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié")
    reasons = gate(w)
    assert len(reasons) == 1 and "validation" in reasons[0]
    validate(w, w.accountant, flag.id, True)
    assert gate(w) == []


def test_a_fee_override_not_validated_blocks(w: FlagWorld) -> None:
    upload_id = clear_day(w)
    assert upload_id
    sale = partner_override_sale(w, "3949999990")
    reasons = gate(w)  # the new sale also changed the day: the re-run saw it
    assert len(reasons) == 2  # the fee, and the partner ticket that is not in the report
    assert any("frais" in r for r in reasons) and any("liste" in r for r in reasons)
    approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)
    w.db.commit()
    assert [("liste" in r) for r in gate(w)] == [True]


def test_the_partner_list_must_be_seen(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    w.sell([w.ticket_of(w.lines(upload_id)[0], doc_number="3949999998")], partner=True)
    w.run()
    reasons = gate(w)
    assert len(reasons) == 1 and "liste" in reasons[0]
    approvals.acknowledge_partner_list(w.db, w.accountant, w.day(), now=NOW)
    w.db.commit()
    assert gate(w) == []
    rows = w.db.scalars(select(DayAcknowledgement)).all()
    assert len(rows) == 1 and rows[0].kind == "partner_list"


def test_every_reason_is_listed_at_once(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié")
    partner_override_sale(w, "3949999990")
    reasons = gate(w)
    assert any("validation" in r for r in reasons)
    assert any("frais" in r for r in reasons)
    assert any("liste" in r for r in reasons)


def test_a_sale_changed_since_the_last_run_makes_the_gate_compare_again(w: FlagWorld) -> None:
    clear_day(w)
    assert runs(w) == 1
    assert gate(w) == []
    assert runs(w) == 1  # nothing changed: no new run
    view = sales.sales_for_day_for(w.db, w.manager, D20)[0]
    ticket = view.tickets[0]
    edited = sales.SaleInput(
        tickets=[_input(ticket, "SYNTH/OTHER")],
        ag_kind="direct",
        pb=view.pb,
        clt=view.clt,
        payment_method=view.payment_method,
        staff_sign=view.staff_sign,
    )
    sales.edit_sale(w.db, w.manager, view.id, edited, "Nom corrigé", now=NOW)
    w.db.commit()
    reasons = gate(w)
    assert runs(w) == 2
    assert len(reasons) == 1 and "écart" in reasons[0]


def _input(ticket: sales.TicketLine, name: str) -> sales.TicketInput:
    return sales.TicketInput(
        doc_number=ticket.doc_number,
        airline=ticket.airline,
        total_doc=ticket.total_doc,
        pax_name=name,
        pax_type=ticket.pax_type,
        rloc=ticket.rloc,
        trnc=ticket.trnc,
    )


def test_a_newer_upload_makes_the_gate_compare_again(w: FlagWorld) -> None:
    clear_day(w)
    # a corrected report arrives (one name differs) after the last comparison
    w.upload(w.changed_20oct(1, **{"PAX NAME": "SYNTH/CHANGED"}), name="20OCT-v2.xlsx")
    before = runs(w)
    reasons = gate(w)
    assert runs(w) == before + 1
    assert len(reasons) == 1 and "écart" in reasons[0]


def test_a_closed_day_is_reported_as_such(w: FlagWorld) -> None:
    clear_day(w)
    approvals.close_day(w.db, w.accountant, w.day(), now=NOW)
    w.db.commit()
    assert gate(w) == [days.CLOSED]


def test_staff_cannot_close(w: FlagWorld) -> None:
    clear_day(w)
    with pytest.raises(days.DayError):
        approvals.close_day(w.db, w.staff, w.day(), now=NOW)
    w.db.rollback()
    assert w.day().status != "closed"
    assert any(
        e.event_type == "permission_denied" and e.details["action"] == "close_day"
        for e in w.db.scalars(select(AuditEvent))
    )


def test_only_the_manager_reopens(w: FlagWorld) -> None:
    clear_day(w)
    approvals.close_day(w.db, w.accountant, w.day(), now=NOW)
    w.db.commit()
    with pytest.raises(days.DayError):
        days.reopen_day(w.db, w.accountant, w.day(), "Erreur de saisie", now=NOW)
    w.db.rollback()
    days.reopen_day(w.db, w.manager, w.day(), "Erreur de saisie", now=NOW)
    w.db.commit()
    assert w.day().status == "compared"


# --- the whole story on the made-up 20 October day ----------------------------------------------


def test_a_cost_difference_goes_from_flag_to_closed_day(w: FlagWorld) -> None:
    flag, line = diff_world(w, "total_doc")
    assert gate(w) and w.day().status == "compared"
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié avec la compagnie")
    assert gate(w) != []  # waiting for a validator
    validate(w, w.accountant, flag.id, True)
    assert gate(w) == []
    mapping = approvals.corrections_for(w.db, w.agency_id, w.day().id)
    assert list(mapping) == [(line.doc_number, "TOTAL DOC")]
    original, approved, who = mapping[(line.doc_number, "TOTAL DOC")]
    assert (original, approved) == (str(int(w.cost_of(line))), str(int(w.cost_of(line)) + 100))
    assert who
    approvals.close_day(w.db, w.accountant, w.day(), now=NOW)
    w.db.commit()
    assert w.day().status == "closed"
    summary = flags.day_summary(w.db, w.manager, w.day())
    assert [f.status for f in summary.flags] == ["resolved"]
