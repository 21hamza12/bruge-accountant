"""A sale is filed under the day its ticket was issued: the screens (owner, 24-26 Oct 2026).

The clock is 22 October 2026. The pasted fixtures were issued on 21 October (yesterday). Synthetic
data only; the service rules are in tests/test_sale_day.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import update

from app.models import DAY_CLOSED, ReportDay, Sale
from app.tenancy import rebind_tenant
from tests.page_support import Env
from tests.test_sale_pages import (
    D1,
    D2,
    SIGN,
    Dom,
    accountant,
    confirm_with,
    counts,
    enter,
    form,
    hidden,
    latest,
    post,
    sale_id_of,
    table,
    text_of,
    tk,
)
from tests.test_sale_paste import one_passenger, paste, pnr, values
from tests.web_support import T0, WebDb, owner_session

TODAY = "2026-10-22"
YESTERDAY = "2026-10-21"
TWO_DAYS_AGO = "2026-10-20"
CLOSED_21 = "La journée du 21 octobre est clôturée : demandez au gérant ou à la comptable."


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    e.clock.now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    return e


def issued(on: str = YESTERDAY, **kw: Any) -> dict[str, Any]:
    """Fields of a form whose single ticket was issued on ``on``."""
    return {"issue_date": [on], **kw}


def day_of(env: Env, sale_id: int) -> str:
    sale = next(s for s in table(env, Sale) if s.id == sale_id)
    day: date = next(d.day for d in table(env, ReportDay) if d.id == sale.report_day_id)
    return day.isoformat()


def close_day(env: Env, day: str) -> None:
    """Close that day (opening it first when nobody wrote to it)."""

    wanted = date.fromisoformat(day)
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        found = [d for d in table(env, ReportDay) if d.day == wanted]
        if not found:
            s.add(
                ReportDay(agency_id=env.seeded.agency_a, day=wanted, created_at=T0, updated_at=T0)
            )
            s.flush()
        s.execute(
            update(ReportDay)
            .where(ReportDay.day == wanted)
            .values(status=DAY_CLOSED, closed_at=T0, closed_by_user_id=env.seeded.manager_a)
        )
        s.commit()


# --- the paste prefills the date ----------


def test_a_pasted_booking_carries_its_issue_date_and_says_the_day(env: Env) -> None:
    html = paste(env.staff_a(), one_passenger()).text
    assert values(html, "issue_date") == [YESTERDAY]
    text = text_of(html)
    assert "Émis le 21 octobre 2026" in text
    assert "Vente du 21 octobre 2026" in text
    assert values(html, "day") == []  # staff do not pick the day


def test_the_managers_day_field_is_prefilled_with_the_issue_date(env: Env) -> None:
    html = paste(env.manager_a(), one_passenger()).text
    assert values(html, "day") == [YESTERDAY]
    assert values(html, "issue_date") == [YESTERDAY]
    assert "Vente du 21 octobre 2026" in text_of(html)


def test_an_unreadable_issue_date_falls_back_to_today_with_a_notice(env: Env) -> None:
    text = one_passenger().replace("21OCT26/NKC", "99XYZ26/NKC")
    html = paste(env.manager_a(), text).text
    assert values(html, "issue_date") == [""]
    assert values(html, "day") == [TODAY]
    assert "d'émission illisible" in text_of(html) and "journée d'aujourd'hui" in text_of(html)
    assert "Vente du 22 octobre 2026" in text_of(html)


def test_a_typed_form_has_no_issue_date_and_keeps_today(env: Env) -> None:
    html = env.manager_a().get("/sales/new").text
    assert values(html, "issue_date") == [""]
    assert values(html, "day") == [TODAY]
    assert "Émis le" not in text_of(html)


# --- the sale goes to the issue day ----------


def test_staff_file_a_sale_under_its_issue_day_and_it_shows_there_not_today(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued())
    assert day_of(env, sale_id) == YESTERDAY
    there = text_of(c.get(f"/sales?date={YESTERDAY}").text)
    assert f"Vente {sale_id}" in there and "Émis le" in there and "21 octobre 2026" in there
    assert f"Vente {sale_id}" not in text_of(c.get(f"/sales?date={TODAY}").text)
    assert latest(env, sale_id).tickets[0]["issue_date"] == YESTERDAY


def test_the_readback_says_the_day_and_the_confirm_files_it_there(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1), **issued()))
    assert r.status_code == 200
    assert "Vente du 21 octobre 2026" in text_of(r.text)
    done = confirm_with(c, r.text, "/sales")
    assert day_of(env, sale_id_of(done)) == YESTERDAY


def test_the_sale_page_shows_the_issue_date_and_staff_may_still_edit_it(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued())
    page = c.get(f"/sales/{sale_id}")
    assert "21 octobre 2026" in text_of(page.text) and "Émis le" in text_of(page.text)
    assert f'href="/sales/{sale_id}/edit"' in page.text
    assert f"/sales/{sale_id}/void" in page.text


def test_a_sale_typed_without_a_date_goes_to_today(env: Env) -> None:
    assert day_of(env, enter(env.staff_a(), tk(D1))) == TODAY


def test_a_manager_without_a_posted_day_gets_the_issue_day(env: Env) -> None:
    m = env.manager_a()
    sale_id = enter(m, tk(D1), staff_sign=SIGN, **issued())
    assert day_of(env, sale_id) == YESTERDAY
    chosen = enter(m, tk(D2), staff_sign=SIGN, day=TWO_DAYS_AGO, **issued())
    assert day_of(env, chosen) == TWO_DAYS_AGO  # their choice


def test_the_readback_warns_a_manager_who_chose_another_day(env: Env) -> None:
    r = post(
        env.manager_a(),
        "/sales/preview",
        form(tk(D1), staff_sign=SIGN, day=TWO_DAYS_AGO, **issued()),
    )
    assert r.status_code == 200
    text = text_of(r.text)
    assert "Vente du 20 octobre 2026" in text
    assert "n'est pas la date d'émission" in text


# --- refused ----------


def test_staff_on_a_closed_issue_day_get_the_message_and_nothing_is_written(env: Env) -> None:
    close_day(env, YESTERDAY)
    c = env.staff_a()
    before = counts(env)
    for path in ("/sales/preview", "/sales"):
        r = post(c, path, form(tk(D1), **issued()))
        assert r.status_code == 409 and CLOSED_21 in text_of(r.text), path
    assert counts(env) == before


def test_a_manager_on_a_closed_day_must_have_it_reopened(env: Env) -> None:
    close_day(env, YESTERDAY)
    r = post(env.manager_a(), "/sales", form(tk(D1), staff_sign=SIGN, **issued()))
    assert r.status_code == 409 and "rouvrir" in text_of(r.text)
    a = accountant(env)
    r = post(a, "/sales", form(tk(D1), staff_sign=SIGN, **issued()))
    assert r.status_code == 409 and "rouvrir" in text_of(r.text)


def test_a_future_issue_date_is_refused(env: Env) -> None:
    for who in (env.staff_a(), env.manager_a()):
        r = post(who, "/sales/preview", form(tk(D1), staff_sign=SIGN, **issued("2026-10-23")))
        assert r.status_code == 400 and "futur" in text_of(r.text)
        assert "Billet 1" in text_of(r.text)
    assert counts(env) == (0, 0, 0, 0, 0)


def test_a_tampered_issue_date_or_day_is_refused_for_staff(env: Env) -> None:
    c = env.staff_a()
    for path in ("/sales/preview", "/sales"):
        garbage = post(c, path, form(tk(D1), **issued("hier")))
        assert garbage.status_code == 400 and "Billet 1" in text_of(garbage.text), path
    # at the confirm a carried day that is not the tickets' day is tampering
    other_day = post(c, "/sales", form(tk(D1), day=TWO_DAYS_AGO, **issued()))
    assert other_day.status_code == 403 and "émission" in text_of(other_day.text)
    junk_day = post(c, "/sales", form(tk(D1), day="21/10/2026", **issued()))
    assert junk_day.status_code == 400
    typed_day = post(c, "/sales", form(tk(D1), day=YESTERDAY))  # a day with no ticket date
    assert typed_day.status_code == 403
    assert counts(env)[:4] == (0, 0, 0, 0)  # refusals are audited, nothing is written


def test_staff_preview_ignores_a_posted_day_and_shows_the_derived_one(env: Env) -> None:
    c = env.staff_a()
    for day in (TWO_DAYS_AGO, "21/10/2026", TODAY):
        r = post(c, "/sales/preview", form(tk(D1), day=day, **issued()))
        assert r.status_code == 200, day
        assert "Vente du 21 octobre 2026" in text_of(r.text)
    r = post(c, "/sales/preview", form(tk(D1), day=YESTERDAY))  # no ticket date: today
    assert r.status_code == 200 and "Vente du 22 octobre 2026" in text_of(r.text)


def test_removing_a_ticket_row_after_the_readback_gives_the_new_day_without_error(
    env: Env,
) -> None:
    c = env.staff_a()
    two = form(
        tk(D1), tk(D2), issue_date=[TWO_DAYS_AGO, YESTERDAY], pb="40000"
    )  # the newest ticket decides: 21 October
    r = post(c, "/sales/preview", two)
    assert "Vente du 21 octobre 2026" in text_of(r.text)
    carried = dict(hidden(r.text, "/sales/new"))
    assert carried["day"] == YESTERDAY
    # "Modifier", then the newest row is removed and the form is previewed again
    one = form(tk(D1), issue_date=[TWO_DAYS_AGO], day=YESTERDAY, pb="40000")
    again = post(c, "/sales/preview", one)
    assert again.status_code == 200
    assert "Vente du 20 octobre 2026" in text_of(again.text)


def test_the_choice_screen_shows_each_proposals_issue_date(env: Env) -> None:
    html = paste(env.staff_a(), pnr("layover_two_pages")).text
    text = text_of(html)
    assert text.count("Émis le 20 octobre 2026") == 2


def test_a_tampered_hidden_day_on_the_readback_is_refused_at_the_confirm(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1), **issued()))
    refused = confirm_with(c, r.text, "/sales", day=TWO_DAYS_AGO)
    assert refused.status_code == 403
    refused = confirm_with(c, r.text, "/sales", issue_date="2026-10-23")
    assert refused.status_code == 400
    assert counts(env)[0] == 0


def test_the_issue_date_row_shows_the_error_next_to_the_ticket(env: Env) -> None:
    r = post(env.staff_a(), "/sales/preview", form(tk(D1), **issued("2026-10-23")))
    dom = Dom.of(r.text)
    assert any(error_id.startswith("t0-issue_date") for _, error_id, _ in dom.errors)


# --- correcting and voiding an earlier day ----------


def test_staff_edit_and_void_their_sale_of_an_earlier_open_day(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued())
    assert c.get(f"/sales/{sale_id}/edit").status_code == 200
    edited = post(c, f"/sales/{sale_id}", form(tk(D1), **issued(), pb="30000", version_no="1"))
    assert edited.status_code == 303 and latest(env, sale_id).version_no == 2
    assert latest(env, sale_id).pb == 30000
    voided = post(c, f"/sales/{sale_id}/void", {"reason": "saisie en double", "version_no": "2"})
    assert voided.status_code == 303 and latest(env, sale_id).voided


def test_staff_cannot_edit_or_void_a_sale_of_a_closed_day(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued())
    close_day(env, YESTERDAY)
    page = c.get(f"/sales/{sale_id}")
    assert f'href="/sales/{sale_id}/edit"' not in page.text
    edit = c.get(f"/sales/{sale_id}/edit")
    assert CLOSED_21 in text_of(edit.text)
    r = post(c, f"/sales/{sale_id}", form(tk(D1), **issued(), pb="30000", version_no="1"))
    assert r.status_code == 409 and CLOSED_21 in text_of(r.text)
    r = post(c, f"/sales/{sale_id}/void", {"reason": "saisie en double", "version_no": "1"})
    assert r.status_code == 409 and CLOSED_21 in text_of(r.text)
    assert latest(env, sale_id).version_no == 1


def test_the_edit_form_keeps_the_issue_date(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued())
    html = c.get(f"/sales/{sale_id}/edit").text
    assert values(html, "issue_date") == [YESTERDAY]
    assert "Émis le 21 octobre 2026" in text_of(html)


# --- flags: a ticket that was not sent ----------


def test_the_form_of_a_not_sent_ticket_files_it_under_the_report_day(env: Env) -> None:
    # the entry form of a flag is covered with the flags (tests/test_approval_pages.py); here the
    # shape it relies on: an issue date alone decides the day for staff
    c = env.staff_a()
    r = post(c, "/sales", form(tk(D1), **issued(TWO_DAYS_AGO)))
    assert day_of(env, sale_id_of(r)) == TWO_DAYS_AGO
