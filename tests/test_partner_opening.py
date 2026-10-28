"""app/partner_opening.py: the switch-over of phase F, slice F5 (SQLite).

The opening balance is entered once by the manager, never edited, never over a closed period;
the check page compares the app's balance at the end of the first half-month with the
spreadsheet's. Names are made up ("Partenaire Test"); the numbers are BADI-style."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, partner_book, partner_opening, permissions
from app.models import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_STAFF, AuditEvent, PartnerEntry
from app.partner_book import AccountConflict, AccountForbidden, AccountNotFound
from app.partner_opening import (
    SIGN_AGENCY_OWES,
    SIGN_PARTNER_OWES,
    NeedsConfirmation,
    OpeningError,
)
from tests.test_partner_book import NOW, World, key, partner_sale, ticket

OCT_3, OCT_19, OCT_20, OCT_21 = (
    date(2026, 10, 3),
    date(2026, 10, 19),
    date(2026, 10, 20),
    date(2026, 10, 21),
)


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


def enter(w: World, amount: str = "50000", **kw: Any) -> partner_book.Written:
    actor = kw.pop("actor", w.manager)
    partner_id = kw.pop("partner_id", w.partner.id)
    kw.setdefault("sign", SIGN_PARTNER_OWES)
    kw.setdefault("switch_date", OCT_20.isoformat())
    kw.setdefault("reason", "solde du fichier Excel au 19/10")
    kw.setdefault("request_key", key())
    written = partner_opening.enter_opening(
        w.db, actor, partner_id, amount=amount, now=kw.pop("now", NOW), **kw
    )
    w.db.commit()
    return written


def enter_confirmed(w: World, amount: str = "50000", **kw: Any) -> partner_book.Written:
    """Both steps: the preview, then the same form ticked with the list it showed."""
    kw.setdefault("request_key", key())
    try:
        return enter(w, amount, **kw)
    except NeedsConfirmation as asked:
        w.db.rollback()
        return enter(w, amount, confirmed=True, acknowledged=asked.dropped.fingerprint, **kw)


def openings(w: World) -> list[PartnerEntry]:
    return w.entries("OPENING")


def events(w: World, event_type: str) -> list[AuditEvent]:
    return list(
        w.db.scalars(
            select(AuditEvent).where(AuditEvent.event_type == event_type).order_by(AuditEvent.id)
        )
    )


# --- who --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "enters", "checks"),
    [(ROLE_MANAGER, True, True), (ROLE_ACCOUNTANT, False, True), (ROLE_STAFF, False, False)],
)
def test_permissions(w: World, role: str, enters: bool, checks: bool) -> None:
    user = {ROLE_MANAGER: w.manager, ROLE_ACCOUNTANT: w.accountant, ROLE_STAFF: w.staff}[role]
    assert permissions.can_enter_partner_opening(user) is enters
    assert permissions.can_check_partner_switchover(user) is checks


@pytest.mark.parametrize("who", ["accountant", "staff"])
def test_only_the_manager_enters_an_opening(w: World, who: str) -> None:
    with pytest.raises(AccountForbidden, match="Seul le gérant"):
        enter(w, actor=getattr(w, who))
    w.db.rollback()
    assert openings(w) == []
    [refused] = events(w, audit.PERMISSION_DENIED)
    assert refused.details == {"action": "enter_partner_opening"}


def test_an_inactive_manager_is_refused(w: World) -> None:
    w.manager.is_active = False
    w.db.commit()
    with pytest.raises(AccountForbidden, match="désactivé"):
        enter(w)
    w.db.rollback()
    assert openings(w) == []


def test_a_partner_of_another_agency_is_not_found(w: World) -> None:
    with pytest.raises(AccountNotFound):
        enter(w, partner_id=w.foreign.id)
    assert openings(w) == []


def test_staff_cannot_read_the_check_rows(w: World) -> None:
    with pytest.raises(AccountForbidden):
        partner_opening.check_rows(w.db, w.staff, NOW.date())


def test_the_check_rows_and_the_missing_list_are_per_agency(w: World) -> None:
    enter(w)
    rows = partner_opening.check_rows(w.db, w.other_manager, NOW.date())
    assert rows == []
    missing = partner_opening.without_opening(w.db, w.other_manager)
    assert [p.id for p in missing] == [w.foreign.id]


# --- entering it ------------------------------------------------------------------------------


def test_the_opening_is_written_once_with_its_sign_and_audited_without_names(w: World) -> None:
    written = enter(w, "50 000")
    assert written.created
    [row] = openings(w)
    assert (row.kind, row.amount, row.entry_date, row.source) == (
        "OPENING",
        Decimal("50000.00"),
        OCT_20,
        "manual",
    )
    assert row.reason == "solde du fichier Excel au 19/10"
    assert row.created_by_user_id == w.manager.id
    [event] = events(w, partner_opening.OPENING_ENTERED)
    assert event.details == {
        "partner_id": w.partner.id,
        "entry_id": row.id,
        "amount": "50000.00",
        "dropped_lines": 0,
        "dropped_payments": 0,
        "dropped_sales": 0,
        "dropped_sum": "0.00",
    }
    text = str(event.details)
    assert "Partenaire" not in text and "Excel" not in text
    assert w.account().opening_amount == Decimal("50000.00")


def test_the_agency_owing_the_partner_is_negative(w: World) -> None:
    enter(w, "12 500.50", sign=SIGN_AGENCY_OWES)
    [row] = openings(w)
    assert row.amount == Decimal("-12500.50")
    assert w.account().balance == Decimal("-12500.50")


def test_a_zero_opening_needs_the_tick_and_no_sign(w: World) -> None:
    enter(w, "0", sign="", zero_balance=True)
    [row] = openings(w)
    assert row.amount == Decimal("0.00")


def test_a_zero_opening_without_the_tick_is_refused(w: World) -> None:
    """L1: an empty or 0 amount must be meant: « Solde nul » is ticked."""
    with pytest.raises(OpeningError, match="Solde nul"):
        enter(w, "0", sign="")
    assert openings(w) == []


def test_the_tick_with_an_amount_is_refused(w: World) -> None:
    with pytest.raises(OpeningError, match="pas zéro"):
        enter(w, "100", zero_balance=True)
    assert openings(w) == []


def test_the_tick_alone_is_a_zero_opening(w: World) -> None:
    enter(w, "", sign="", zero_balance=True)
    [row] = openings(w)
    assert row.amount == Decimal("0.00")


def test_a_nonzero_amount_needs_a_sign(w: World) -> None:
    with pytest.raises(OpeningError, match="qui doit à qui"):
        enter(w, "100", sign="")
    assert openings(w) == []


@pytest.mark.parametrize("amount", ["-5", "abc", "1,500", ""])
def test_a_bad_amount_is_refused(w: World, amount: str) -> None:
    with pytest.raises(OpeningError):
        enter(w, amount)
    assert openings(w) == []


def test_the_reason_is_required(w: World) -> None:
    with pytest.raises(OpeningError):
        enter(w, reason="    ")
    assert openings(w) == []


def test_a_future_date_is_refused(w: World) -> None:
    with pytest.raises(OpeningError, match="futur"):
        enter(w, switch_date="2026-10-28")
    assert openings(w) == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_date_is_refused_not_defaulted(w: World, blank: str) -> None:
    """M2: the date is required; the server never picks today on its own."""
    with pytest.raises(OpeningError, match="Saisissez la date du passage"):
        enter(w, switch_date=blank)
    assert openings(w) == []


@pytest.mark.parametrize("typo", ["2002-10-20", "0001-01-01", "2025-09-21"])
def test_a_date_more_than_400_days_back_is_refused(w: World, typo: str) -> None:
    """H1: a typo in the year would make every account walk hundreds of half-months."""
    with pytest.raises(OpeningError, match="400 jours"):
        enter(w, switch_date=typo)
    assert openings(w) == []


def test_400_days_back_is_the_oldest_date_accepted(w: World) -> None:
    oldest = partner_opening.earliest_switch_day(NOW.date())
    assert oldest == date(2025, 9, 22)
    enter(w, switch_date=oldest.isoformat())
    [row] = openings(w)
    assert row.entry_date == oldest


def test_the_floor_is_never_before_2020() -> None:
    assert partner_opening.earliest_switch_day(date(2020, 3, 1)) == date(2020, 1, 1)


def test_an_inactive_partner_is_refused(w: World) -> None:
    """L3: no opening for a partner who was deactivated."""
    w.partner.is_active = False
    w.db.commit()
    with pytest.raises(AccountConflict, match="désactivé"):
        enter(w)
    assert openings(w) == []
    assert partner_opening.refusal_for(w.db, w.partner) == partner_opening.PARTNER_INACTIVE


def test_refusal_for_names_why_the_form_is_not_shown(w: World) -> None:
    assert partner_opening.refusal_for(w.db, w.partner) is None
    enter(w)
    assert "déjà un solde" in (partner_opening.refusal_for(w.db, w.partner) or "")


def test_a_second_opening_is_refused(w: World) -> None:
    enter(w)
    with pytest.raises(AccountConflict, match="déjà un solde d'ouverture"):
        enter(w, "1", switch_date=OCT_21.isoformat())
    w.db.rollback()
    [row] = openings(w)
    assert row.amount == Decimal("50000.00")
    assert len(events(w, partner_opening.OPENING_ENTERED)) == 1


def test_an_opening_after_a_closed_period_is_refused(w: World) -> None:
    """No rewriting of history: once a period is closed, the opening would change its opening
    balance after the statement was sent, so it is refused whatever its date."""
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)
    w.set_days("closed")
    w.close()
    for day in ("2026-10-01", OCT_20.isoformat()):
        with pytest.raises(AccountConflict, match="déjà clôturée"):
            enter(w, switch_date=day)
        w.db.rollback()
    assert openings(w) == []


def test_the_message_says_an_error_is_fixed_by_an_adjustment(w: World) -> None:
    enter(w)
    with pytest.raises(AccountConflict, match="ajustement"):
        enter(w)


# --- double clicks ----------------------------------------------------------------------------


def test_the_same_key_replayed_returns_the_stored_opening(w: World) -> None:
    same = key()
    first = enter(w, request_key=same)
    again = enter(w, request_key=same)
    assert first.created and not again.created
    assert again.row.id == first.row.id
    assert len(openings(w)) == 1
    assert len(events(w, partner_opening.OPENING_ENTERED)) == 1


@pytest.mark.parametrize(
    "change",
    [{"amount": "50001"}, {"sign": SIGN_AGENCY_OWES}, {"switch_date": OCT_19.isoformat()}],
)
def test_the_same_key_with_other_values_is_refused(w: World, change: dict[str, str]) -> None:
    same = key()
    enter(w, request_key=same)
    args: dict[str, Any] = {"request_key": same, **change}
    with pytest.raises(partner_book.StaleRequestKey):
        enter(w, args.pop("amount", "50000"), **args)
    assert len(openings(w)) == 1


def test_a_key_of_another_line_is_refused(w: World) -> None:
    used = key()
    w.line("ADM", "1500", request_key=used, entry_date=OCT_20.isoformat())
    with pytest.raises(partner_book.StaleRequestKey):
        enter(w, request_key=used)
    assert openings(w) == []


def test_a_bad_request_key_is_refused(w: World) -> None:
    with pytest.raises(OpeningError, match="rechargez"):
        enter(w, request_key="court")


def _twin_commits_while_we_wait(
    w: World, monkeypatch: pytest.MonkeyPatch, same_key: str, amount: Decimal
) -> None:
    """While this post waits for the lock, a twin post with the same key commits its row."""

    def lock(db: Session, partner_id: int, *, shared: bool) -> None:
        db.add(
            PartnerEntry(
                agency_id=w.agency_id,
                partner_id=partner_id,
                entry_date=OCT_20,
                kind="OPENING",
                amount=amount,
                reason="solde du fichier Excel au 19/10",
                source="manual",
                created_by_user_id=w.manager.id,
                request_key=same_key,
            )
        )
        db.flush()

    monkeypatch.setattr(partner_opening, "lock_partner", lock)


def test_a_twin_post_that_won_the_lock_is_returned_not_refused(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: the key is looked up again under the lock, before the "already opened" check."""
    same = key()
    _twin_commits_while_we_wait(w, monkeypatch, same, Decimal("50000.00"))
    written = enter(w, request_key=same)
    assert not written.created
    assert len(openings(w)) == 1
    assert events(w, partner_opening.OPENING_ENTERED) == []  # the twin audits its own


def test_a_twin_with_other_values_under_the_lock_is_a_stale_key(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    same = key()
    _twin_commits_while_we_wait(w, monkeypatch, same, Decimal("1.00"))
    with pytest.raises(partner_book.StaleRequestKey):
        enter(w, request_key=same)


# --- what the account counts ------------------------------------------------------------------


def test_sales_before_the_switch_over_are_left_out_and_on_or_after_counted(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)  # 21 368 + 300
    w.sell(partner_sale(w.partner, ticket("3940000002", "10000")), OCT_19)  # 10 300
    w.sell(partner_sale(w.partner, ticket("3940000003", "5000")), OCT_20)  # 5 300
    w.sell(partner_sale(w.partner, ticket("3940000004", "1000")), OCT_21)  # 1 300
    enter_confirmed(w, "50000")
    acc = w.account()
    assert [p.start for p in acc.periods] == [date(2026, 10, 16)]
    [period] = acc.periods
    assert [s.day for s in period.sales] == [OCT_20, OCT_21]
    assert period.opening == Decimal("50000.00")
    assert acc.balance == Decimal("56600.00")


def test_lines_and_payments_before_the_switch_over_are_refused_afterwards(w: World) -> None:
    enter(w)
    with pytest.raises(partner_book.PartnerAccountError, match="antérieure au solde"):
        w.line("ADM", "1500", entry_date=OCT_19.isoformat())


# --- what stops counting: preview, then confirmation ----------------------------------------


def _earlier_items(w: World) -> None:
    """Before the 20th: a sale (21 668), an ADM (1 500), a pending AJUSTEMENT (+200, not
    counted) and a payment (20 000). Counted today: 21 668 + 1 500 - 20 000 = 3 168."""
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)
    w.line("ADM", "1500", entry_date=OCT_19.isoformat())
    w.line("AJUSTEMENT", "200", direction="plus", reason="frais oubliés", entry_date="2026-10-18")
    w.pay("20000", paid_on=OCT_19.isoformat())


def test_the_first_post_shows_what_stops_counting_and_writes_nothing(w: World) -> None:
    _earlier_items(w)
    with pytest.raises(NeedsConfirmation) as asked:
        enter(w)
    w.db.rollback()
    dropped = asked.value.dropped
    assert not asked.value.changed
    assert (len(dropped.lines), len(dropped.payments), len(dropped.sales)) == (2, 1, 1)
    assert {v.entry.kind for v in dropped.lines} == {"ADM", "AJUSTEMENT"}
    assert [v.pending for v in dropped.lines if v.entry.kind == "AJUSTEMENT"] == [True]
    assert dropped.counted_sum == Decimal("3168.00")
    assert dropped.effect == Decimal("-3168.00")
    assert dropped.summary("MRU") == (
        "2 lignes, 1 paiement et 1 vente datés avant le 20/10 ne seront plus comptés "
        "(effet net : -3 168 MRU)"
    )
    assert openings(w) == []
    assert events(w, partner_opening.OPENING_ENTERED) == []


def test_the_tick_without_the_list_it_was_given_for_is_not_enough(w: World) -> None:
    _earlier_items(w)
    with pytest.raises(NeedsConfirmation):
        enter(w, confirmed=True)
    w.db.rollback()
    with pytest.raises(NeedsConfirmation):
        enter(w, confirmed=False, acknowledged=partner_opening.dropped_before(
            w.db, w.agency_id, w.partner.id, OCT_20).fingerprint)  # fmt: skip
    assert openings(w) == []


def test_the_confirmed_opening_is_written_and_audited_with_numbers_only(w: World) -> None:
    _earlier_items(w)
    written = enter_confirmed(w)
    assert written.created
    [event] = events(w, partner_opening.OPENING_ENTERED)
    assert event.details == {
        "partner_id": w.partner.id,
        "entry_id": written.row.id,
        "amount": "50000.00",
        "dropped_lines": 2,
        "dropped_payments": 1,
        "dropped_sales": 1,
        "dropped_sum": "3168.00",
    }
    assert w.account().balance == Decimal("50000.00")


def test_a_list_that_changed_since_the_preview_is_refused_with_the_new_one(w: World) -> None:
    _earlier_items(w)
    same = key()
    with pytest.raises(NeedsConfirmation) as asked:
        enter(w, request_key=same)
    w.db.rollback()
    seen = asked.value.dropped.fingerprint
    w.pay("500", paid_on="2026-10-18")  # recorded by someone else meanwhile
    with pytest.raises(NeedsConfirmation) as again:
        enter(w, request_key=same, confirmed=True, acknowledged=seen)
    w.db.rollback()
    assert again.value.changed
    assert len(again.value.dropped.payments) == 2
    assert "a changé" in str(again.value)
    assert openings(w) == []


def test_nothing_to_drop_needs_one_step(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_21)  # after: still counted
    assert partner_opening.dropped_before(w.db, w.agency_id, w.partner.id, OCT_20).empty
    assert enter(w).created


def test_accounts_of_refuses_a_partner_of_another_agency(w: World) -> None:
    """L4: never a mixed result."""
    with pytest.raises(ValueError, match="not of agency"):
        partner_book.accounts_of(w.db, w.agency_id, [w.partner, w.foreign], NOW.date())


# --- the check page ---------------------------------------------------------------------------


def test_the_check_row_is_the_balance_at_the_end_of_the_first_half_month(w: World) -> None:
    w.sell(partner_sale(w.partner, ticket("3940000001")), OCT_3)  # before: left out
    w.sell(partner_sale(w.partner, ticket("3940000002", "10000")), OCT_21)  # 10 300
    enter_confirmed(w, "50000")
    w.pay("20000", paid_on=OCT_21.isoformat())
    [row] = partner_opening.check_rows(w.db, w.accountant, NOW.date())
    assert row.partner.id == w.partner.id
    assert row.opening.amount == Decimal("50000.00")
    assert (row.start, row.end) == (date(2026, 10, 16), date(2026, 10, 31))
    assert row.app_balance == Decimal("40300.00")
    assert row.provisional  # the period is still open
    w.set_days("closed", date(2026, 10, 16), date(2026, 10, 31))
    w.close(date(2026, 10, 16), now=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    [closed] = partner_opening.check_rows(w.db, w.manager, date(2026, 11, 2))
    assert not closed.provisional and closed.app_balance == Decimal("40300.00")


def test_partners_without_an_opening_are_not_on_the_check_page(w: World) -> None:
    assert partner_opening.check_rows(w.db, w.manager, NOW.date()) == []
    missing = partner_opening.without_opening(w.db, w.manager)
    assert {p.id for p in missing} == {w.partner.id, w.second.id}
    enter(w)
    assert [p.id for p in partner_opening.without_opening(w.db, w.manager)] == [w.second.id]


@pytest.mark.parametrize(
    ("typed", "sign", "difference", "label"),
    [
        ("40300", SIGN_PARTNER_OWES, Decimal("0.00"), "Identique"),
        ("40000", SIGN_PARTNER_OWES, Decimal("300.00"), "Écart de 300 MRU"),
        ("40500", SIGN_PARTNER_OWES, Decimal("-200.00"), "Écart de 200 MRU"),
        ("100", SIGN_AGENCY_OWES, Decimal("40400.00"), "Écart de 40 400 MRU"),
    ],
)
def test_the_comparison(typed: str, sign: str, difference: Decimal, label: str) -> None:
    result = partner_opening.compare(Decimal("40300.00"), typed, sign, currency="MRU")
    assert result.difference == difference
    assert result.label == label
    assert result.same is (difference == 0)


def test_a_comparison_with_a_bad_amount_is_a_french_error() -> None:
    with pytest.raises(OpeningError):
        partner_opening.compare(Decimal("1"), "abc", SIGN_PARTNER_OWES, currency="MRU")
    with pytest.raises(OpeningError, match="qui doit à qui"):
        partner_opening.compare(Decimal("1"), "5", "", currency="MRU")


def test_the_audit_event_name_is_registered() -> None:
    assert partner_opening.OPENING_ENTERED == audit.PARTNER_OPENING_ENTERED
