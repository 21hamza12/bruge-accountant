"""A sale is filed under the day its ticket was issued (owner, 24-26 Oct 2026).

Service rules first (staff may file and correct an earlier day that is open, never a closed or a
future one, and the day is tied to the tickets' issue dates), then the comparison that depends on
it. The screens are in tests/test_sale_day_pages.py. Synthetic data only.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import days, sales
from app.models import ReportDay, Sale
from app.sales import InvalidReason, InvalidSale, SaleForbidden, SaleInput, SaleNotFound
from app.sales_rules import latest_issue_date, lines_from_json, ticket_json
from tests.flag_support import FlagWorld
from tests.test_sales import D1, D2, D3, NOW, TODAY, YESTERDAY, World, direct, ticket

TWO_DAYS_AGO = date(2026, 10, 20)
CLOSED_MESSAGE = "La journée du 21 octobre est clôturée : demandez au gérant ou à la comptable."


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


def issued(doc: str, day: date | None, **kw: Any) -> Any:
    return ticket(doc, issue_date=day, **kw)


def close(w: World, day: date) -> None:
    w.create(w.manager, direct(ticket(D3), staff_sign="0004SV"), day=day)
    found = days.get_day(w.db, w.agency_id, day)
    assert found is not None
    days.close_day(w.db, w.manager, found, now=NOW)


# --- the date is part of the ticket ----------


def test_the_issue_date_is_stored_with_the_ticket_and_read_back(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    assert [t.issue_date for t in sale.tickets] == [YESTERDAY]
    again = sales.get_sale_for(w.db, w.staff, sale.id)
    assert again is not None and again.tickets[0].issue_date == YESTERDAY
    assert w.history(sale.id)[0].tickets[0]["issue_date"] == "2026-10-21"


def test_a_ticket_stored_before_the_date_existed_reads_back_without_one() -> None:
    old = {
        "doc_number": D1, "airline": "199", "total_doc": "10.00", "pax_name": "X/Y",
        "pax_type": "ADT", "rloc": "ABC123", "trnc": "TKTT", "position": 1,
    }  # fmt: skip
    (line,) = lines_from_json([old])
    assert line.issue_date is None
    assert lines_from_json([ticket_json(line)])[0] == line


def test_the_newest_ticket_gives_the_day() -> None:
    older, newer = issued(D1, date(2026, 9, 23)), issued(D2, date(2026, 10, 21))
    assert latest_issue_date([older, newer]) == date(2026, 10, 21)
    assert latest_issue_date([ticket(D1)]) is None
    assert latest_issue_date([]) is None


def test_a_bad_issue_date_is_a_field_error(w: World) -> None:
    for bad in ("2026-10-21", 20261021, datetime(2026, 10, 21, tzinfo=UTC)):
        with pytest.raises(InvalidSale) as caught:
            w.create(w.manager, direct(issued(D1, bad), staff_sign="0004SV"))  # type: ignore[arg-type]
        assert [e.field for e in caught.value.errors] == ["tickets[0].issue_date"]
    assert w.count(Sale) == 0


# --- creating: the day follows the ticket ----------


def test_staff_file_a_ticket_under_the_day_it_was_issued(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    assert sale.day == YESTERDAY
    assert {d.day for d in w.db.scalars(select(ReportDay))} == {YESTERDAY}
    assert [v.id for v in sales.sales_for_day_for(w.db, w.staff, YESTERDAY)] == [sale.id]
    assert sales.sales_for_day_for(w.db, w.staff, TODAY) == []


def test_staff_may_name_the_issue_day_but_no_other(w: World) -> None:
    ok = w.create(w.staff, direct(issued(D1, YESTERDAY)), day=YESTERDAY).sale
    assert ok.day == YESTERDAY
    for other in (TODAY, TWO_DAYS_AGO):
        with pytest.raises(SaleForbidden, match="émission"):
            w.create(w.staff, direct(issued(D2, YESTERDAY)), day=other)
        with pytest.raises(SaleForbidden, match="émission"):
            sales.preview_sale(w.db, w.staff, direct(issued(D2, YESTERDAY)), day=other, now=NOW)


def test_staff_without_an_issue_date_keep_today_and_cannot_pick_a_day(w: World) -> None:
    assert w.create(w.staff, direct(ticket(D1))).sale.day == TODAY
    with pytest.raises(SaleForbidden, match="émission"):
        w.create(w.staff, direct(ticket(D2)), day=YESTERDAY)
    assert w.create(w.staff, direct(ticket(D2)), day=TODAY).sale.day == TODAY


def test_the_exchange_is_filed_under_its_new_ticket(w: World) -> None:
    sale = w.create(
        w.staff, direct(issued(D1, date(2026, 9, 23)), issued(D2, YESTERDAY), pb="30000")
    ).sale
    assert sale.day == YESTERDAY


def test_a_ticket_issued_in_the_future_is_refused_for_everybody(w: World) -> None:
    tomorrow = date(2026, 10, 23)
    for actor in (w.staff, w.manager):
        with pytest.raises(InvalidSale) as caught:
            w.create(actor, direct(issued(D1, tomorrow), staff_sign="0004SV"))
        assert caught.value.errors[0].field == "tickets[0].issue_date"
        assert "futur" in caught.value.errors[0].message
    assert w.count(Sale) == 0


def test_a_ticket_issued_on_a_closed_day_is_refused_to_staff_with_the_message(w: World) -> None:
    close(w, YESTERDAY)
    with pytest.raises(days.DayClosed) as caught:
        w.create(w.staff, direct(issued(D1, YESTERDAY)))
    assert str(caught.value) == CLOSED_MESSAGE
    with pytest.raises(days.DayClosed, match="clôturée"):
        sales.preview_sale(w.db, w.staff, direct(issued(D1, YESTERDAY)), now=NOW)
    assert sales.sales_for_day_for(w.db, w.staff, TODAY) == []


def test_the_closed_day_message_names_the_year_when_it_is_not_this_one(w: World) -> None:
    last_year = date(2025, 6, 25)
    close(w, last_year)
    with pytest.raises(days.DayClosed) as caught:
        w.create(w.staff, direct(issued(D1, last_year)))
    assert str(caught.value) == (
        "La journée du 25 juin 2025 est clôturée : demandez au gérant ou à la comptable."
    )


def test_a_manager_cannot_file_on_a_closed_day_either_until_it_is_reopened(w: World) -> None:
    close(w, YESTERDAY)
    with pytest.raises(days.DayClosed, match="rouvrir"):
        w.create(w.manager, direct(issued(D1, YESTERDAY), staff_sign="0004SV"))
    found = days.get_day(w.db, w.agency_id, YESTERDAY)
    assert found is not None
    days.reopen_day(w.db, w.manager, found, "erreur de saisie", now=NOW)
    sale = w.create(w.manager, direct(issued(D1, YESTERDAY), staff_sign="0004SV")).sale
    assert sale.day == YESTERDAY


def test_a_manager_may_name_another_day_than_the_issue_date(w: World) -> None:
    sale = w.create(
        w.manager, direct(issued(D1, YESTERDAY), staff_sign="0004SV"), day=TWO_DAYS_AGO
    ).sale
    assert sale.day == TWO_DAYS_AGO  # their choice, as before
    derived = w.create(w.manager, direct(issued(D2, YESTERDAY), staff_sign="0004SV")).sale
    assert derived.day == YESTERDAY  # no day named: the issue date, not today


def test_staff_cannot_file_on_a_future_or_too_old_day(w: World) -> None:
    with pytest.raises(days.DayError):
        w.create(w.staff, direct(issued(D1, date(2015, 1, 1))))


def test_another_signs_ticket_is_still_not_theirs_on_an_earlier_day(w: World) -> None:
    with pytest.raises(SaleForbidden):
        w.create(w.staff, direct(issued(D1, YESTERDAY), staff_sign="0006AC"))


# --- correcting and voiding an earlier day ----------


def test_staff_edit_and_void_their_own_sale_of_an_earlier_open_day(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    edited = w.edit(w.staff, sale.id, direct(issued(D1, YESTERDAY), pb="25000"), "prix corrigé")
    assert edited.sale.pb == 25000 and edited.sale.day == YESTERDAY
    voided = w.void(w.staff, sale.id)
    assert voided.status == "voided"


def test_staff_cannot_edit_or_void_a_sale_of_a_closed_day(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    found = days.get_day(w.db, w.agency_id, YESTERDAY)
    assert found is not None
    days.close_day(w.db, w.manager, found, now=NOW)
    with pytest.raises(days.DayClosed) as caught:
        w.edit(w.staff, sale.id, direct(issued(D1, YESTERDAY), pb="25000"), "")
    assert str(caught.value) == CLOSED_MESSAGE
    with pytest.raises(days.DayClosed):
        w.void(w.staff, sale.id)
    with pytest.raises(days.DayClosed):
        sales.preview_sale(
            w.db, w.staff, direct(issued(D1, YESTERDAY), pb="25000"), sale_id=sale.id, now=NOW
        )
    assert len(w.history(sale.id)) == 1


def test_staff_still_cannot_touch_another_signs_sale_on_an_earlier_day(w: World) -> None:
    sale = w.create(w.manager, direct(issued(D1, YESTERDAY), staff_sign="0006AC")).sale
    with pytest.raises(SaleNotFound):
        w.edit(w.staff, sale.id, direct(issued(D1, YESTERDAY), pb="25000"), "")
    with pytest.raises(SaleNotFound):
        w.void(w.staff, sale.id)


def test_an_edit_keeps_the_day_even_if_the_dates_change(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    edited = w.edit(w.staff, sale.id, direct(issued(D1, TWO_DAYS_AGO), pb="25000"), "correction")
    assert edited.sale.day == YESTERDAY  # only move_sale_day changes the day


def test_staff_must_give_a_reason_to_edit_a_sale_of_an_earlier_day(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    for missing in (None, "", "   ", "abc"):
        with pytest.raises(InvalidReason):
            w.edit(w.staff, sale.id, direct(issued(D1, YESTERDAY), pb="25000"), missing)
    assert len(w.history(sale.id)) == 1
    w.edit(w.staff, sale.id, direct(issued(D1, YESTERDAY), pb="25000"), "prix corrigé")
    versions = w.history(sale.id)
    assert len(versions) == 2 and versions[-1].reason == "prix corrigé"


def test_staff_edit_of_todays_own_sale_needs_no_reason(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1))).sale
    edited = w.edit(w.staff, sale.id, direct(ticket(D1), pb="25000"), None)
    assert edited.sale.pb == 25000 and edited.sale.version_no == 2


def test_staff_void_of_an_earlier_day_needs_a_reason(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    with pytest.raises(InvalidReason):
        w.void(w.staff, sale.id, "")
    assert w.void(w.staff, sale.id, "saisie en double").status == "voided"
    assert w.history(sale.id)[-1].reason == "saisie en double"


def test_staff_edit_keeps_each_tickets_issue_date(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY), issued(D2, TWO_DAYS_AGO), pb="30000"))
    forged = direct(
        issued(D1, TWO_DAYS_AGO), issued(D2, YESTERDAY), ticket(D3, issue_date=TWO_DAYS_AGO),
        pb="31000",
    )  # fmt: skip
    edited = w.edit(w.staff, sale.sale.id, forged, "ajout d'un billet")
    dates = {t.doc_number: t.issue_date for t in edited.sale.tickets}
    assert dates[D1] == YESTERDAY and dates[D2] == TWO_DAYS_AGO
    assert dates[D3] == TWO_DAYS_AGO  # a new row keeps what the web layer let through
    assert edited.sale.day == YESTERDAY


def test_staff_edit_matches_tickets_by_normalised_number(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    spaced = f"199-{D1}"
    edited = w.edit(w.staff, sale.id, direct(issued(spaced, TWO_DAYS_AGO), pb="25000"), "corrigé")
    assert edited.sale.tickets[0].issue_date == YESTERDAY


def test_staff_edit_cannot_add_a_date_to_a_ticket_that_had_none(w: World) -> None:
    sale = w.create(w.staff, direct(ticket(D1))).sale
    edited = w.edit(w.staff, sale.id, direct(issued(D1, TWO_DAYS_AGO), pb="25000"), None)
    assert edited.sale.tickets[0].issue_date is None


def test_a_manager_edit_may_change_the_issue_date(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    edited = w.edit(w.manager, sale.id, direct(issued(D1, TWO_DAYS_AGO)), "date corrigée")
    assert edited.sale.tickets[0].issue_date == TWO_DAYS_AGO


def test_the_preview_of_a_staff_edit_shows_the_kept_dates(w: World) -> None:
    sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
    result = sales.preview_sale(
        w.db, w.staff, direct(issued(D1, TWO_DAYS_AGO), pb="25000"), sale_id=sale.id, now=NOW
    )
    assert not isinstance(result, list) and result.tickets[0].issue_date == YESTERDAY


def test_a_staff_sale_filed_on_another_day_is_logged_with_ids_only(
    w: World, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="app.sales"):
        sale = w.create(w.staff, direct(issued(D1, YESTERDAY))).sale
        w.create(w.staff, direct(ticket(D2)))  # today: not logged
        w.create(w.manager, direct(issued(D3, YESTERDAY), staff_sign="0004SV"))  # not staff
    records = [r for r in caplog.records if "another day" in r.getMessage()]
    assert len(records) == 1
    r = records[0]
    assert (r.agency_id, r.user_id, r.sale_id) == (w.agency_id, w.staff.id, sale.id)  # type: ignore[attr-defined]
    assert "SYNTH" not in r.getMessage() and "Client" not in str(r.__dict__)


def test_void_blocker_is_only_about_a_closed_day(w: World) -> None:
    assert sales.void_blocker(w.staff, "open") is None
    assert sales.void_blocker(w.staff, None) is None
    assert sales.void_blocker(w.staff, "closed") == sales.VOID_DAY_CLOSED
    assert sales.void_blocker(w.manager, "closed") == sales.VOID_DAY_CLOSED


# --- the comparison of that day sees the sale ----------


@pytest.fixture
def fw(session: Session) -> FlagWorld:
    return FlagWorld(session)


def _sell_rest_and_pick(fw: FlagWorld) -> Any:
    """Upload the report, sell every issued line but one that belongs to the sign of ``fw.staff``
    (returned), as the manager does on the day itself."""
    upload = fw.upload()
    lines = [x for x in fw.lines(upload.upload_id) if x.trnc == "TKTT"]
    picked = next(x for x in lines if x.staff_sign == "0004SV")
    fw.sell_all_issued(upload.upload_id, skip=(picked.doc_number,))
    return picked


def test_a_sale_filed_under_its_issue_date_is_compared_with_that_days_report(
    fw: FlagWorld,
) -> None:
    from tests.flag_support import D20

    picked = _sell_rest_and_pick(fw)
    data = SaleInput(
        tickets=[fw.ticket_of(picked, issue_date=D20)],
        ag_kind="direct", pb="99999", clt="Client Synth", payment_method="cash",
    )  # fmt: skip
    made = sales.create_sale(fw.db, fw.staff, data, now=datetime(2026, 10, 22, 9, tzinfo=UTC))
    fw.db.commit()
    assert made.sale.day == D20
    fw.run(D20)
    assert [f for f in fw.live(D20) if f.kind == "not_sent"] == []
    assert fw.live(D20) == []


def test_without_the_issue_date_the_same_sale_is_flagged_on_another_day(fw: FlagWorld) -> None:
    from tests.flag_support import D20

    picked = _sell_rest_and_pick(fw)
    data = SaleInput(
        tickets=[fw.ticket_of(picked)],
        ag_kind="direct", pb="99999", clt="Client Synth", payment_method="cash",
    )  # fmt: skip
    sales.create_sale(fw.db, fw.staff, data, now=datetime(2026, 10, 22, 9, tzinfo=UTC))
    fw.db.commit()
    fw.run(D20)
    assert [(f.kind, f.doc_number) for f in fw.live(D20)] == [("elsewhere", picked.doc_number)]


def test_ticket_input_is_still_built_without_a_date() -> None:
    assert replace(ticket(D1)).issue_date is None
