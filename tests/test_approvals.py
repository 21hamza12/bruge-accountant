"""app/approvals.py: answering flags, validating, fee overrides, the corrections (B9).

Service level, on the made-up 20 October report. Decision 18: the issuing agent answers; a change
of cost, ticket number or ticket type also needs the accountant or the manager, never the person
who answered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app import approvals, days, sales
from app.db import make_engine, make_session_factory
from app.models import AuditEvent, ComparisonFlag, User
from app.sales import SaleInput, SaleView
from app.tenancy import rebind_tenant
from tests.approval_support import (
    OTHER_NAME,
    agent_for,
    answer,
    decisions,
    diff_world,
    partner_override_sale,
    sale_of,
    status_of,
    validate,
)
from tests.flag_support import D19, D20, NOW, FlagWorld


@pytest.fixture
def w(session: Session) -> FlagWorld:
    return FlagWorld(session)


def audit_types(w: FlagWorld) -> list[str]:
    return [e.event_type for e in w.db.scalars(select(AuditEvent).order_by(AuditEvent.id))]


# --- a fix the agent can make alone ------------------------------------------------------------


def test_a_name_fix_by_the_agent_alone_is_resolved(w: FlagWorld) -> None:
    flag, line = diff_world(w, "pax_name")
    result = answer(w, w.staff, flag.id, "mine_right", "Orthographe confirmée par le client")
    assert result.status == "resolved"
    assert status_of(w, flag.id) == "resolved"
    (d,) = decisions(w, flag.id)
    assert (d.stage, d.choice, d.actor_user_id) == ("agent", "mine_right", w.staff.id)
    assert (d.old_value, d.new_value) == (OTHER_NAME, OTHER_NAME)  # the app value stays
    assert line.pax_name != OTHER_NAME
    assert d.reason == "Orthographe confirmée par le client"


def test_amadeus_right_on_a_name_edits_the_sale_and_the_rerun_clears_the_flag(
    w: FlagWorld,
) -> None:
    flag, line = diff_world(w, "pax_name")
    assert flag.sale_id is not None
    before = sale_of(w, flag.sale_id)
    result = answer(w, w.staff, flag.id, "amadeus_right")
    assert result.applied
    after = sale_of(w, flag.sale_id)
    assert after.version_no == before.version_no + 1
    assert after.tickets[0].pax_name == line.pax_name
    assert after.source == "comparison" and after.reason == approvals.AMADEUS_REASON
    assert after.actor_user_id == w.staff.id
    assert w.live() == []  # the re-run found nothing left to flag
    assert status_of(w, flag.id) == "superseded"
    (d,) = decisions(w, flag.id)
    assert (d.choice, d.reason, d.old_value, d.new_value) == (
        "amadeus_right",
        None,
        OTHER_NAME,
        line.pax_name,
    )
    assert "flag_answered" in audit_types(w)


def test_a_staff_member_can_correct_a_sale_of_a_past_day(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "rloc")  # D20 is two days before the clock: staff normally cannot edit
    result = answer(w, w.staff, flag.id, "amadeus_right")
    assert result.applied and w.live() == []


def test_the_manager_can_answer_for_any_sign(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name", sign="0006AC")
    assert answer(w, w.manager, flag.id, "mine_right", "Le client confirme").status == "resolved"


# --- cost: the agent answers, a validator decides ----------------------------------------------


def test_a_cost_answer_waits_for_a_validator_and_then_resolves(w: FlagWorld) -> None:
    flag, line = diff_world(w, "total_doc")
    result = answer(w, w.staff, flag.id, "mine_right", "Tarif négocié confirmé")
    assert result.status == "answered"
    assert status_of(w, flag.id) == "answered"
    approved = validate(w, w.accountant, flag.id, True)
    assert approved.status == "resolved"
    assert status_of(w, flag.id) == "resolved"
    stages = [(d.stage, d.choice, d.actor_user_id) for d in decisions(w, flag.id)]
    assert stages == [
        ("agent", "mine_right", w.staff.id),
        ("validator", "validated", w.accountant.id),
    ]
    sale = sale_of(w, flag.sale_id or 0)
    assert sale.tickets[0].total_doc == w.cost_of(line) + 100  # the app value stays
    assert {"flag_answered", "flag_validated"} <= set(audit_types(w))


def test_amadeus_right_on_a_cost_is_applied_when_the_validator_approves(w: FlagWorld) -> None:
    flag, line = diff_world(w, "total_doc")
    assert flag.sale_id is not None
    before = sale_of(w, flag.sale_id)
    result = answer(w, w.staff, flag.id, "amadeus_right")
    assert result.status == "answered" and not result.applied
    assert sale_of(w, flag.sale_id).version_no == before.version_no  # nothing touched yet
    done = validate(w, w.manager, flag.id, True)
    assert done.applied
    after = sale_of(w, flag.sale_id)
    assert after.version_no == before.version_no + 1
    assert after.tickets[0].total_doc == w.cost_of(line)
    assert after.actor_user_id == w.manager.id and after.source == "comparison"
    assert w.live() == []


def test_a_rejection_reopens_the_flag_with_the_reason_and_leaves_the_sale(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    assert flag.sale_id is not None
    version = sale_of(w, flag.sale_id).version_no
    answer(w, w.staff, flag.id, "amadeus_right")
    result = validate(w, w.accountant, flag.id, False, "Vérifiez le billet d'abord")
    assert result.status == "open"
    assert status_of(w, flag.id) == "open"
    last = decisions(w, flag.id)[-1]
    assert (last.stage, last.choice, last.reason) == (
        "validator",
        "rejected",
        "Vérifiez le billet d'abord",
    )
    assert sale_of(w, flag.sale_id).version_no == version
    assert "flag_rejected" in audit_types(w)
    # the agent can answer again
    assert answer(w, w.staff, flag.id, "mine_right", "Tarif confirmé par mail").status == "answered"


def test_a_rejection_needs_a_reason(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif confirmé")
    with pytest.raises(approvals.ApprovalError):
        approvals.validate_flag(w.db, w.accountant, flag.id, False, "ok", now=NOW)
    w.db.rollback()
    assert status_of(w, flag.id) == "answered"


# --- who may do what ---------------------------------------------------------------------------


def test_staff_cannot_validate_and_it_is_audited(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif confirmé")
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.validate_flag(w.db, w.staff2, flag.id, True, None, now=NOW)
    w.db.rollback()
    assert status_of(w, flag.id) == "answered"
    denied = [e for e in w.db.scalars(select(AuditEvent)) if e.event_type == "permission_denied"]
    assert [e.details["action"] for e in denied] == ["validate_flag"]


def test_a_validator_cannot_validate_their_own_answer(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.manager, flag.id, "mine_right", "Tarif confirmé")
    with pytest.raises(approvals.ApprovalForbidden) as caught:
        approvals.validate_flag(w.db, w.manager, flag.id, True, None, now=NOW)
    assert "votre propre réponse" in str(caught.value)
    w.db.rollback()
    assert status_of(w, flag.id) == "answered"
    assert validate(w, w.accountant, flag.id, True).status == "resolved"  # somebody else can


def test_staff_cannot_answer_another_signs_flag(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name", sign="0006AC")
    with pytest.raises(approvals.ApprovalNotFound):
        approvals.answer_flag(
            w.db, w.staff, flag.id, "mine_right", "Je pense avoir raison", now=NOW
        )
    w.db.rollback()
    assert status_of(w, flag.id) == "open"


def test_another_agency_cannot_see_the_flag(w: FlagWorld, db_url: str) -> None:
    flag, _ = diff_world(w, "pax_name")
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as theirs:  # a session of the other agency
            rebind_tenant(theirs, w.other_agency_id)
            stranger = theirs.get(User, w.other_manager.id)
            assert stranger is not None
            with pytest.raises(approvals.ApprovalNotFound):
                approvals.answer_flag(
                    theirs, stranger, flag.id, "mine_right", "Je pense avoir raison", now=NOW
                )
            theirs.rollback()
            assert approvals.decisions_for(theirs, stranger, [flag.id]) == {}
    finally:
        engine.dispose()
    assert status_of(w, flag.id) == "open"


def test_a_validation_that_nobody_asked_for_is_refused(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")  # still open
    with pytest.raises(approvals.ApprovalError):
        approvals.validate_flag(w.db, w.accountant, flag.id, True, None, now=NOW)


def test_an_answered_flag_cannot_be_answered_again(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif confirmé")
    with pytest.raises(approvals.ApprovalError):
        approvals.answer_flag(w.db, w.staff, flag.id, "amadeus_right", None, now=NOW)


def test_a_closed_day_refuses_answers(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name")
    day = w.day()
    day.status, day.closed_at, day.closed_by_user_id = "closed", NOW, w.manager.id
    w.db.commit()
    with pytest.raises(days.DayClosed):
        approvals.answer_flag(w.db, w.manager, flag.id, "mine_right", "Le client confirme", now=NOW)


def test_reasons_are_required_where_the_schema_says_so(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name")
    for reason in (None, "", "   ", "ok"):
        with pytest.raises(approvals.ApprovalError):
            approvals.answer_flag(w.db, w.staff, flag.id, "mine_right", reason, now=NOW)
        w.db.rollback()
    with pytest.raises(approvals.ApprovalError):
        approvals.answer_flag(w.db, w.staff, flag.id, "nonsense", None, now=NOW)
    w.db.rollback()
    assert decisions(w, flag.id) == []


# --- the other kinds of flag -------------------------------------------------------------------


def not_in_amadeus(w: FlagWorld) -> tuple[ComparisonFlag, int]:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    extra = w.ticket_of(w.lines(upload_id)[0], doc_number="3949999999")
    sale = w.sell([extra], sign="0004SV")
    w.run()
    (flag,) = w.live()
    assert flag.kind == "not_in_amadeus"
    return flag, sale.id


def test_void_entry_waits_for_a_validator_who_voids_the_sale(w: FlagWorld) -> None:
    flag, sale_id = not_in_amadeus(w)
    result = answer(w, w.staff, flag.id, "void_entry", "Saisie par erreur, billet jamais émis")
    assert result.status == "answered" and not result.applied
    assert sale_of(w, sale_id).status == "active"  # the agent alone cannot void
    (d,) = decisions(w, flag.id)
    assert (d.choice, d.reason) == ("void_entry", "Saisie par erreur, billet jamais émis")
    assert d.sale_version_no == sale_of(w, sale_id).version_no
    done = validate(w, w.accountant, flag.id, True)
    assert done.applied
    assert sale_of(w, sale_id).status == "voided"
    assert sale_of(w, sale_id).actor_user_id == w.accountant.id
    assert w.live() == []


def test_a_void_cannot_be_validated_by_its_own_answerer(w: FlagWorld) -> None:
    flag, sale_id = not_in_amadeus(w)
    answer(w, w.manager, flag.id, "void_entry", "Saisie par erreur, billet jamais émis")
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.validate_flag(w.db, w.manager, flag.id, True, None, now=NOW)
    w.db.rollback()
    assert sale_of(w, sale_id).status == "active"


def test_a_rejected_void_leaves_the_sale_and_reopens_the_flag(w: FlagWorld) -> None:
    flag, sale_id = not_in_amadeus(w)
    answer(w, w.staff, flag.id, "void_entry", "Saisie par erreur, billet jamais émis")
    assert validate(w, w.accountant, flag.id, False, "Le billet existe bien").status == "open"
    assert sale_of(w, sale_id).status == "active"


def test_void_entry_needs_a_reason(w: FlagWorld) -> None:
    flag, sale_id = not_in_amadeus(w)
    with pytest.raises(approvals.ApprovalError):
        approvals.answer_flag(w.db, w.staff, flag.id, "void_entry", None, now=NOW)
    w.db.rollback()
    assert sale_of(w, sale_id).status == "active"


def test_a_void_of_a_multi_ticket_sale_is_refused(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    lines = w.lines(upload_id)
    extra = [
        w.ticket_of(lines[0], doc_number="3949999991"),
        w.ticket_of(lines[0], doc_number="3949999992"),
    ]
    w.sell(extra, sign="0004SV")
    w.run()
    flag = next(f for f in w.live() if f.doc_number == "3949999991")
    with pytest.raises(approvals.ApprovalError):
        approvals.answer_flag(
            w.db, w.staff, flag.id, "void_entry", "Saisie par erreur, jamais émis", now=NOW
        )


def test_waiting_for_a_later_report_records_it_and_keeps_the_flag_open(w: FlagWorld) -> None:
    flag, sale_id = not_in_amadeus(w)
    result = answer(w, w.staff, flag.id, "acknowledged")
    assert result.status == "open" and not result.applied
    assert [d.choice for d in decisions(w, flag.id)] == ["acknowledged"]
    assert sale_of(w, sale_id).status == "active"


def test_a_ticket_not_sent_is_answered_by_entering_the_sale(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    lines = w.lines(upload_id)
    missing = next(x for x in lines if x.trnc == "TKTT" and x.staff_sign == "0004SV")
    w.sell_all_issued(upload_id, skip=[missing.doc_number])
    w.run()
    (flag,) = w.live()
    assert flag.kind == "not_sent"
    for choice in ("amadeus_right", "mine_right", "void_entry"):
        with pytest.raises(approvals.ApprovalError):
            approvals.answer_flag(w.db, w.staff, flag.id, choice, "Une raison valable", now=NOW)
        w.db.rollback()
    prefill = approvals.prefill_for_not_sent(w.db, w.staff, flag.id)
    assert (prefill.doc_number, prefill.airline, prefill.pax_name) == (
        missing.doc_number,
        missing.airline,
        missing.pax_name,
    )
    assert (prefill.total_doc, prefill.rloc, prefill.trnc) == (
        missing.total_doc,
        missing.rloc,
        missing.trnc,
    )
    assert prefill.staff_sign == "0004SV"
    # entering the sale and comparing again resolves the flag
    w.sell([w.ticket_of(missing)], sign="0004SV")
    w.run()
    assert w.live() == []
    with pytest.raises(approvals.ApprovalNotFound):
        approvals.prefill_for_not_sent(w.db, w.staff2, flag.id)


def test_prefill_is_only_for_a_ticket_not_sent(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name")
    with pytest.raises(approvals.ApprovalError):
        approvals.prefill_for_not_sent(w.db, w.staff, flag.id)


# --- two validators at once --------------------------------------------------------------------


def test_two_validators_on_the_same_flag_only_one_wins(w: FlagWorld, db_url: str) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif confirmé")
    engine = make_engine(db_url)
    try:
        with make_session_factory(engine)() as other:
            rebind_tenant(other, w.agency_id)
            stale = other.get(ComparisonFlag, flag.id)
            assert stale is not None and stale.status == "answered"
            validate(w, w.accountant, flag.id, True)  # the first one wins and commits
            with pytest.raises(approvals.ApprovalError):  # the second finds it already settled
                second = other.get(User, w.manager.id)
                assert second is not None
                approvals.validate_flag(
                    other, second, flag.id, False, "Je refuse ce tarif", now=NOW
                )
            other.rollback()
            # even holding a stale copy, the status change itself is a compare-and-set
            other.expire_all()
            stale2 = other.get(ComparisonFlag, flag.id)
            assert stale2 is not None
            assert not approvals.claim(other, stale2, "answered", "open", NOW)
            other.rollback()
    finally:
        engine.dispose()
    assert status_of(w, flag.id) == "resolved"
    validators = [d for d in decisions(w, flag.id) if d.stage == "validator"]
    assert [d.choice for d in validators] == ["validated"]


# --- fee overrides -----------------------------------------------------------------------------


def test_an_override_is_queued_and_a_validator_approves_it(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    sale = partner_override_sale(w, "3949999990")
    day = w.day()
    queue = approvals.validation_queue(w.db, w.accountant, day)
    assert [s.id for s in queue.overrides] == [sale.id]
    assert approvals.pending_override_count(w.db, w.accountant, day) == 1
    approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)
    w.db.commit()
    assert approvals.validation_queue(w.db, w.accountant, day).overrides == ()
    assert "override_validated" in audit_types(w)
    assert sale_of(w, sale.id).version_no == sale.version_no  # approving changes nothing


def test_a_rejected_override_restores_the_standard_fee(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    sale = partner_override_sale(w, "3949999990", fee=300)
    assert sale.fee_applied == Decimal(300)
    with pytest.raises(approvals.ApprovalError):
        approvals.validate_override(w.db, w.accountant, sale.id, False, None, now=NOW)
    w.db.rollback()
    approvals.validate_override(w.db, w.accountant, sale.id, False, "Remise non autorisée", now=NOW)
    w.db.commit()
    after = sale_of(w, sale.id)
    assert after.version_no == sale.version_no + 1
    assert after.fee_applied == Decimal(500) and after.fee_override_reason is None
    assert after.reason == "Remise non autorisée" and after.actor_user_id == w.accountant.id
    assert approvals.validation_queue(w.db, w.accountant, w.day()).overrides == ()


def test_nobody_validates_their_own_override_and_staff_cannot_validate(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    sale = partner_override_sale(w, "3949999990", actor=w.accountant)
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)
    w.db.rollback()
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.validate_override(w.db, w.staff, sale.id, True, None, now=NOW)
    w.db.rollback()
    approvals.validate_override(w.db, w.manager, sale.id, True, None, now=NOW)
    w.db.commit()


def test_an_approval_stays_valid_after_an_unrelated_edit(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    sale = partner_override_sale(w, "3949999990")
    approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)
    w.db.commit()
    ticket = sale.tickets[0]
    edited = SaleInput(
        tickets=[
            w.ticket_of(w.lines(upload_id)[0], doc_number=ticket.doc_number, pax_name="SYNTH/NEW")
        ],
        ag_kind="partner",
        partner_id=w.badi.id,
        payment_method="credit",
        staff_sign="0004SV",
    )
    sales.edit_sale(w.db, w.manager, sale.id, edited, "Nom corrigé", now=NOW)
    w.db.commit()
    assert approvals.validation_queue(w.db, w.accountant, w.day()).overrides == ()


# --- the partner list --------------------------------------------------------------------------


def test_acknowledging_the_partner_list(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    w.sell([w.ticket_of(w.lines(upload_id)[0], doc_number="3949999998")], partner=True)
    w.run()
    day = w.day()
    assert not approvals.partner_list_seen(w.db, w.accountant, day)
    seen = approvals.partner_digest(w.db, w.accountant, day)
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.acknowledge_partner_list(w.db, w.staff, day, seen_digest=seen, now=NOW)
    w.db.rollback()
    approvals.acknowledge_partner_list(w.db, w.accountant, day, seen_digest=seen, now=NOW)
    w.db.commit()
    assert approvals.partner_list_seen(w.db, w.accountant, day)
    assert "partner_list_acknowledged" in audit_types(w)
    # a different list needs a new look
    w.sell([w.ticket_of(w.lines(upload_id)[0], doc_number="3949999997")], partner=True)
    w.run()
    assert not approvals.partner_list_seen(w.db, w.accountant, w.day())


def test_acknowledging_an_empty_list_is_refused(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    w.run()
    with pytest.raises(approvals.ApprovalError):
        approvals.acknowledge_partner_list(
            w.db, w.accountant, w.day(), seen_digest="0" * 16, now=NOW
        )


# --- the corrections for the export ------------------------------------------------------------


def test_corrections_for_lists_the_approved_cost(w: FlagWorld) -> None:
    flag, line = diff_world(w, "total_doc")
    assert approvals.corrections_for(w.db, w.agency_id, w.day().id) == {}  # nothing approved yet
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié")
    assert approvals.corrections_for(w.db, w.agency_id, w.day().id) == {}  # still waiting
    validate(w, w.accountant, flag.id, True)
    mapping = approvals.corrections_for(w.db, w.agency_id, w.day().id)
    who = w.accountant.display_name or f"Utilisateur {w.accountant.id}"
    assert mapping == {
        (line.doc_number, "TOTAL DOC"): (
            str(int(w.cost_of(line))),
            str(int(w.cost_of(line)) + 100),
            who,
        )
    }


def test_corrections_for_includes_a_name_the_agent_kept(w: FlagWorld) -> None:
    flag, line = diff_world(w, "pax_name")
    answer(w, w.staff, flag.id, "mine_right", "Orthographe du passeport")
    mapping = approvals.corrections_for(w.db, w.agency_id, w.day().id)
    assert list(mapping) == [(line.doc_number, "PAX NAME")]
    original, approved, _ = mapping[(line.doc_number, "PAX NAME")]
    assert (original, approved) == (line.pax_name, OTHER_NAME)


def test_a_value_taken_from_amadeus_is_no_correction(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name")
    answer(w, w.staff, flag.id, "amadeus_right")
    assert approvals.corrections_for(w.db, w.agency_id, w.day().id) == {}


# --- the counts the sidebar shows --------------------------------------------------------------


def test_the_sidebar_counts(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    assert approvals.to_answer_count(w.db, w.staff) == 1
    assert approvals.to_answer_count(w.db, w.staff2) == 0
    assert approvals.to_validate_count(w.db, w.accountant) == 0
    assert approvals.to_validate_count(w.db, w.staff) == 0
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié")
    assert approvals.to_answer_count(w.db, w.staff) == 0
    assert approvals.to_validate_count(w.db, w.accountant) == 1
    assert approvals.to_validate_count(w.db, w.staff) == 0
    assert approvals.to_validate_count(w.db, w.manager) == 1


def test_the_agent_sees_the_decisions_of_their_flags(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié")
    validate(w, w.accountant, flag.id, False, "Pas de justificatif")
    found = approvals.decisions_for(w.db, w.staff, [flag.id])
    assert [d.choice for d in found[flag.id]] == ["mine_right", "rejected"]
    assert approvals.decisions_for(w.db, w.staff2, [flag.id]) == {}


def test_the_agent_of_the_sale_edit_goes_through_the_normal_rules(w: FlagWorld) -> None:
    """The edit made for 'Amadeus a raison' is a plain ``sales.edit_sale``: a staff member cannot
    reach a sale of another sign even with a flag id (the flag is a 404 first)."""
    flag, _ = diff_world(w, "pax_name", sign="0006AC")
    assert agent_for(w, "0006AC").id == w.staff2.id
    with pytest.raises(approvals.ApprovalNotFound):
        approvals.answer_flag(w.db, w.staff, flag.id, "amadeus_right", None, now=NOW)


# --- review round: what needs a validator ----------------------------------------


def test_a_fare_class_difference_needs_a_validator(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_type")
    assert flag.field == "pax_type" and flag.needs_validation
    assert answer(w, w.staff, flag.id, "mine_right", "Tarif enfant confirmé").status == "answered"


def test_a_sign_difference_needs_a_validator_and_staff_cannot_ask_for_amadeus(
    w: FlagWorld,
) -> None:
    upload = w.upload()
    lines = w.lines(upload.upload_id)
    target = next(x for x in lines if x.trnc == "TKTT" and x.staff_sign == "0004SV")
    for line in lines:
        if line.trnc == "TKTT":
            sign = "0006AC" if line is target else line.staff_sign
            w.sell([w.ticket_of(line)], sign=sign)
    w.run()
    flag = next(f for f in w.live() if f.doc_number == target.doc_number)
    assert flag.field == "staff_sign" and flag.needs_validation
    with pytest.raises(approvals.ApprovalForbidden):  # staff cannot re-sign a sale to another sign
        approvals.answer_flag(w.db, w.staff2, flag.id, "amadeus_right", None, now=NOW)
    w.db.rollback()
    assert answer(w, w.manager, flag.id, "amadeus_right").status == "answered"
    assert validate(w, w.accountant, flag.id, True).applied
    assert w.live() == []


# --- review round: the sale is tied to the flag ----------------------------------------


def test_a_flag_made_stale_by_an_edit_is_refused_and_compared_again(w: FlagWorld) -> None:
    flag, line = diff_world(w, "pax_name")
    assert flag.sale_id is not None
    view = sale_of(w, flag.sale_id)
    t = view.tickets[0]
    edited = SaleInput(
        tickets=[
            w.ticket_of(
                line, doc_number=t.doc_number, total_doc=t.total_doc + 7, pax_name=t.pax_name
            )
        ],
        ag_kind="direct",
        pb=view.pb,
        clt=view.clt,
        payment_method=view.payment_method,
        staff_sign=view.staff_sign,
    )
    sales.edit_sale(w.db, w.manager, view.id, edited, "Prix corrigé", now=NOW)
    w.db.commit()
    with pytest.raises(approvals.SituationChanged) as caught:
        approvals.answer_flag(w.db, w.staff, flag.id, "amadeus_right", None, now=NOW)
    assert "La situation a changé" in str(caught.value)
    w.db.commit()  # the new comparison is kept
    assert status_of(w, flag.id) == "superseded"
    assert decisions(w, flag.id) == []


def test_a_sale_edited_after_the_answer_sends_the_flag_back_to_open(w: FlagWorld) -> None:
    flag, line = diff_world(w, "total_doc")
    assert flag.sale_id is not None
    first = sale_of(w, flag.sale_id)
    answer(w, w.staff, flag.id, "amadeus_right")
    (d,) = decisions(w, flag.id)
    assert d.sale_version_no == first.version_no
    # a change that is not part of the compared facts still moves the sale to a new version
    edited = SaleInput(
        tickets=[
            w.ticket_of(
                line, doc_number=first.tickets[0].doc_number, total_doc=first.tickets[0].total_doc
            )
        ],
        ag_kind="direct",
        pb=first.pb,
        clt="Autre Client",
        payment_method=first.payment_method,
        staff_sign=first.staff_sign,
    )
    sales.edit_sale(w.db, w.manager, first.id, edited, "Client corrigé", now=NOW)
    w.db.commit()
    result = validate(w, w.accountant, flag.id, True)
    assert result.status == "open" and not result.applied
    last = decisions(w, flag.id)[-1]
    assert (last.stage, last.choice) == ("validator", "rejected")
    assert "re-répondre" in (last.reason or "")
    assert sale_of(w, flag.sale_id).tickets[0].total_doc == first.tickets[0].total_doc


def test_the_sale_must_be_the_flags_own(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "pax_name")
    assert flag.sale_id is not None
    sales.move_sale_day(w.db, w.manager, flag.sale_id, D19, "Mauvais jour", now=NOW)
    w.db.commit()
    with pytest.raises(approvals.SituationChanged):
        approvals.answer_flag(w.db, w.manager, flag.id, "amadeus_right", None, now=NOW)


# --- review round: small rules ----------------------------------------


def test_waiting_twice_records_once(w: FlagWorld) -> None:
    flag, _ = not_in_amadeus(w)
    answer(w, w.staff, flag.id, "acknowledged")
    answer(w, w.staff, flag.id, "acknowledged")
    assert [d.choice for d in decisions(w, flag.id)] == ["acknowledged"]


def test_a_name_with_other_case_and_spacing_does_not_reopen_the_flag(w: FlagWorld) -> None:
    upload = w.upload(w.changed_20oct(1, **{"PAX NAME": "synth02/pax  jr"}))
    lines = w.lines(upload.upload_id)
    for line in lines:
        if line.trnc == "TKTT":
            name = "SYNTH/OTHER" if line.seq_order == 2 else line.pax_name
            w.sell([w.ticket_of(line, pax_name=name)], sign=line.staff_sign)
    w.run()
    (flag,) = w.live()
    answer(w, w.manager, flag.id, "amadeus_right")
    w.run()
    assert w.live() == []


def count_selects(w: FlagWorld, call: Callable[[], object]) -> int:
    seen: list[str] = []

    def spy(conn: object, cursor: object, statement: str, *rest: object) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "set_config" not in statement:
            seen.append(statement)

    engine = w.db.get_bind()
    event.listen(engine, "before_cursor_execute", spy)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    return len(seen)


def test_decisions_are_read_in_a_fixed_number_of_queries(w: FlagWorld) -> None:
    flag, _ = diff_world(w, "total_doc")
    answer(w, w.staff, flag.id, "mine_right", "Tarif négocié")
    ids = [flag.id] * 1
    small = count_selects(w, lambda: approvals.decisions_for(w.db, w.staff, ids))
    many = count_selects(w, lambda: approvals.decisions_for(w.db, w.staff, ids * 20 + [10**6]))
    assert small == many <= 3


def test_the_validation_badge_is_a_fixed_number_of_queries(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    partner_override_sale(w, "3949999990")
    one = count_selects(w, lambda: approvals.to_validate_count(w.db, w.accountant))
    partner_override_sale(w, "3949999989", fee=250)
    partner_override_sale(w, "3949999988", fee=200)
    assert approvals.to_validate_count(w.db, w.accountant) == 3
    assert count_selects(w, lambda: approvals.to_validate_count(w.db, w.accountant)) == one <= 3


# --- review round: override approvals ----------------------------------------


def edit_override(
    w: FlagWorld, sale: SaleView, actor: User, *, fee: int | None, cost: int = 0
) -> None:
    ticket = sale.tickets[0]
    data = SaleInput(
        tickets=[
            replace(
                w.ticket_of(w.lines(1)[0], doc_number=ticket.doc_number),
                total_doc=ticket.total_doc + cost,
            )
        ],
        ag_kind="partner",
        partner_id=w.badi.id,
        payment_method="credit",
        staff_sign="0004SV",
        fee_override=None if fee is None else Decimal(fee),
        fee_override_reason=None if fee is None else "Remise accordée au partenaire",
    )
    sales.edit_sale(w.db, actor, sale.id, data, "Correction", now=NOW)
    w.db.commit()


def test_an_approval_does_not_survive_a_changed_cost_or_fee(w: FlagWorld) -> None:
    w.sell_all_issued(w.upload().upload_id)
    sale = partner_override_sale(w, "3949999990")
    approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)
    w.db.commit()
    edit_override(w, sale_of(w, sale.id), w.manager, fee=300, cost=50)  # another cost
    assert approvals.to_validate_count(w.db, w.accountant) == 1


def test_an_approval_never_revives_when_the_digest_changes_and_changes_back(w: FlagWorld) -> None:
    w.sell_all_issued(w.upload().upload_id)
    sale = partner_override_sale(w, "3949999990", fee=300)
    approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)
    w.db.commit()
    edit_override(w, sale_of(w, sale.id), w.manager, fee=250)
    edit_override(w, sale_of(w, sale.id), w.manager, fee=300)  # back to the approved fee
    assert approvals.to_validate_count(w.db, w.accountant) == 1


def test_whoever_wrote_the_fee_in_any_version_cannot_approve_it(w: FlagWorld) -> None:
    w.sell_all_issued(w.upload().upload_id)
    sale = partner_override_sale(w, "3949999990", fee=300, actor=w.manager)
    edit_override(w, sale_of(w, sale.id), w.accountant, fee=300, cost=10)  # same fee, new version
    with pytest.raises(approvals.ApprovalForbidden):  # the manager wrote the fee
        approvals.validate_override(w.db, w.manager, sale.id, True, None, now=NOW)
    w.db.rollback()
    with pytest.raises(approvals.ApprovalForbidden):  # the accountant wrote a version carrying it
        approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)


def test_an_override_edited_by_the_would_be_validator_in_between_is_refused(w: FlagWorld) -> None:
    w.sell_all_issued(w.upload().upload_id)
    sale = partner_override_sale(w, "3949999990", fee=300, actor=w.manager)
    edit_override(w, sale_of(w, sale.id), w.accountant, fee=300, cost=10)  # same fee, other cost
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.validate_override(w.db, w.accountant, sale.id, True, None, now=NOW)


# --- review round: the partner list digest ----------------------------------------


def partner_day(w: FlagWorld, fee_sale: int | None = None) -> int:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    w.sell([w.ticket_of(w.lines(upload_id)[0], doc_number="3949999998")], partner=True)
    w.run()
    return upload_id


def test_the_list_digest_covers_the_amounts(w: FlagWorld) -> None:
    upload_id = partner_day(w)
    first = approvals.partner_digest(w.db, w.accountant, w.day())
    view = sales.sales_for_day_for(w.db, w.manager, D20)[-1]
    t = view.tickets[0]
    data = SaleInput(
        tickets=[
            w.ticket_of(w.lines(upload_id)[0], doc_number=t.doc_number, total_doc=t.total_doc + 9)
        ],
        ag_kind="partner",
        partner_id=w.badi.id,
        payment_method="credit",
        staff_sign=view.staff_sign,
    )
    sales.edit_sale(w.db, w.manager, view.id, data, "Prix corrigé", now=NOW)
    w.db.commit()
    assert approvals.partner_digest(w.db, w.accountant, w.day()) != first


def test_a_stale_digest_is_refused(w: FlagWorld) -> None:
    partner_day(w)
    with pytest.raises(approvals.ApprovalError) as caught:
        approvals.acknowledge_partner_list(
            w.db, w.accountant, w.day(), seen_digest="f" * 16, now=NOW
        )
    assert "La liste a changé" in str(caught.value)


def test_the_author_of_a_listed_sale_cannot_acknowledge_it(w: FlagWorld) -> None:
    partner_day(w)  # the manager wrote the partner sale
    seen = approvals.partner_digest(w.db, w.manager, w.day())
    with pytest.raises(approvals.ApprovalForbidden):
        approvals.acknowledge_partner_list(w.db, w.manager, w.day(), seen_digest=seen, now=NOW)
    w.db.rollback()
    approvals.acknowledge_partner_list(w.db, w.accountant, w.day(), seen_digest=seen, now=NOW)
