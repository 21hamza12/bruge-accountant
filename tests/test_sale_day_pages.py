"""A sale is filed under the day its ticket was issued: the screens (owner, 24-26 Oct 2026).

The clock is 22 October 2026. The pasted fixtures were issued on 21 October (yesterday). Synthetic
data only; the service rules are in tests/test_sale_day.py.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import update

from app.issue_sig import IssueSigner
from app.models import DAY_CLOSED, ReportDay, Sale
from app.tenancy import rebind_tenant
from app.web.sale_forms import TICKET_FIELDS
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


KEY = "test-issue-signing-key"


@pytest.fixture
def env(web_db: WebDb, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv("ISSUE_SIGNING_KEY", KEY)  # the app signs with a key the tests know
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    e.clock.now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
    return e


def sig(env: Env, doc: str, on: str, *, user: int | None = None, agency: int | None = None) -> str:
    """The signature the server gives staff A (by default) for that ticket and date."""
    who = user if user is not None else env.seeded.staff_a
    ag = agency if agency is not None else env.seeded.agency_a
    try:
        day = date.fromisoformat(on)
    except ValueError:
        return ""
    return IssueSigner(KEY.encode(), ag, who).sign(doc, day)


def issued(env: Env, on: str = YESTERDAY, doc: str = D1, **kw: Any) -> dict[str, Any]:
    """Fields of a form whose single ticket was issued on ``on``, as the server signed it."""
    return {"issue_date": [on], "issue_sig": [sig(env, doc, on)], **kw}


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


def todays(text: str) -> str:
    """A booking fixture re-dated to today: a paste only reads today's tickets (decision 25)."""
    return text.replace("21OCT26/NKC", "22OCT26/NKC").replace("20OCT26/NKC", "22OCT26/NKC")


def test_a_pasted_booking_carries_its_issue_date_and_says_the_day(env: Env) -> None:
    html = paste(env.staff_a(), todays(one_passenger())).text
    assert values(html, "issue_date") == [TODAY]
    text = text_of(html)
    assert "Émis le 22 octobre 2026" in text
    assert "Vente du 22 octobre 2026" in text
    assert values(html, "day") == []  # staff do not pick the day


def test_the_managers_day_field_is_prefilled_with_the_issue_date(env: Env) -> None:
    html = paste(env.manager_a(), todays(one_passenger())).text
    assert values(html, "day") == [TODAY]
    assert values(html, "issue_date") == [TODAY]
    assert "Vente du 22 octobre 2026" in text_of(html)


def test_a_pasted_ticket_of_yesterday_is_not_read_even_for_the_manager(env: Env) -> None:
    for c in (env.staff_a(), env.manager_a()):
        r = paste(c, one_passenger())  # issued on the 21st, today is the 22nd
        assert r.status_code == 400
        assert "1 billet(s) émis avant aujourd'hui ignoré(s) : 3940299481" in text_of(r.text)
        assert values(r.text, "doc_number") == [""]


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
    sale_id = enter(c, tk(D1), **issued(env))
    assert day_of(env, sale_id) == YESTERDAY
    there = text_of(c.get(f"/sales?date={YESTERDAY}").text)
    assert f"Vente {sale_id}" in there and "Émis le" in there and "21 octobre 2026" in there
    assert f"Vente {sale_id}" not in text_of(c.get(f"/sales?date={TODAY}").text)
    assert latest(env, sale_id).tickets[0]["issue_date"] == YESTERDAY


def test_the_readback_says_the_day_and_the_confirm_files_it_there(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1), **issued(env)))
    assert r.status_code == 200
    assert "Vente du 21 octobre 2026" in text_of(r.text)
    done = confirm_with(c, r.text, "/sales")
    assert day_of(env, sale_id_of(done)) == YESTERDAY


def test_the_sale_page_shows_the_issue_date_and_staff_may_still_edit_it(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    page = c.get(f"/sales/{sale_id}")
    assert "21 octobre 2026" in text_of(page.text) and "Émis le" in text_of(page.text)
    assert f'href="/sales/{sale_id}/edit"' in page.text
    assert f"/sales/{sale_id}/void" in page.text


def test_a_sale_typed_without_a_date_goes_to_today(env: Env) -> None:
    assert day_of(env, enter(env.staff_a(), tk(D1))) == TODAY


def test_a_manager_without_a_posted_day_gets_the_issue_day(env: Env) -> None:
    m = env.manager_a()
    sale_id = enter(m, tk(D1), staff_sign=SIGN, **issued(env))
    assert day_of(env, sale_id) == YESTERDAY
    chosen = enter(m, tk(D2), staff_sign=SIGN, day=TWO_DAYS_AGO, **issued(env))
    assert day_of(env, chosen) == TWO_DAYS_AGO  # their choice


def test_the_readback_warns_a_manager_who_chose_another_day(env: Env) -> None:
    r = post(
        env.manager_a(),
        "/sales/preview",
        form(tk(D1), staff_sign=SIGN, day=TWO_DAYS_AGO, **issued(env)),
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
        r = post(c, path, form(tk(D1), **issued(env)))
        assert r.status_code == 409 and CLOSED_21 in text_of(r.text), path
    assert counts(env) == before


def test_a_manager_on_a_closed_day_must_have_it_reopened(env: Env) -> None:
    close_day(env, YESTERDAY)
    r = post(env.manager_a(), "/sales", form(tk(D1), staff_sign=SIGN, **issued(env)))
    assert r.status_code == 409 and "rouvrir" in text_of(r.text)
    a = accountant(env)
    r = post(a, "/sales", form(tk(D1), staff_sign=SIGN, **issued(env)))
    assert r.status_code == 409 and "rouvrir" in text_of(r.text)


def test_a_future_issue_date_is_refused(env: Env) -> None:
    for who in (env.staff_a(), env.manager_a()):
        r = post(who, "/sales/preview", form(tk(D1), staff_sign=SIGN, **issued(env, "2026-10-23")))
        assert r.status_code == 400 and "futur" in text_of(r.text)
        assert "Billet 1" in text_of(r.text)
    assert counts(env) == (0, 0, 0, 0, 0)


def test_a_tampered_issue_date_or_day_is_refused_for_staff(env: Env) -> None:
    c = env.staff_a()
    ignored = post(c, "/sales/preview", form(tk(D1), **issued(env, "hier")))  # staff: just ignored
    assert ignored.status_code == 200 and "Vente du 22 octobre 2026" in text_of(ignored.text)
    for path in ("/sales/preview", "/sales"):  # a manager's unreadable date is a mistake on its row
        bad = post(env.manager_a(), path, form(tk(D2), staff_sign=SIGN, **issued(env, "hier")))
        assert bad.status_code == 400 and "Billet 1" in text_of(bad.text), path
    # at the confirm a carried day that is not the tickets' day is tampering
    other_day = post(c, "/sales", form(tk(D1), day=TWO_DAYS_AGO, **issued(env)))
    assert other_day.status_code == 403 and "émission" in text_of(other_day.text)
    junk_day = post(c, "/sales", form(tk(D1), day="21/10/2026", **issued(env)))
    assert junk_day.status_code == 400
    typed_day = post(c, "/sales", form(tk(D1), day=YESTERDAY))  # a day with no ticket date
    assert typed_day.status_code == 403
    assert counts(env)[:4] == (0, 0, 0, 0)  # refusals are audited, nothing is written


def test_staff_preview_ignores_a_posted_day_and_shows_the_derived_one(env: Env) -> None:
    c = env.staff_a()
    for day in (TWO_DAYS_AGO, "21/10/2026", TODAY):
        r = post(c, "/sales/preview", form(tk(D1), day=day, **issued(env)))
        assert r.status_code == 200, day
        assert "Vente du 21 octobre 2026" in text_of(r.text)
    r = post(c, "/sales/preview", form(tk(D1), day=YESTERDAY))  # no ticket date: today
    assert r.status_code == 200 and "Vente du 22 octobre 2026" in text_of(r.text)


def test_removing_a_ticket_row_after_the_readback_gives_the_new_day_without_error(
    env: Env,
) -> None:
    c = env.staff_a()
    two = form(
        tk(D1),
        tk(D2),
        issue_date=[TWO_DAYS_AGO, YESTERDAY],
        issue_sig=[sig(env, D1, TWO_DAYS_AGO), sig(env, D2, YESTERDAY)],
        pb="40000",
    )  # the newest ticket decides: 21 October
    r = post(c, "/sales/preview", two)
    assert "Vente du 21 octobre 2026" in text_of(r.text)
    carried = dict(hidden(r.text, "/sales/new"))
    assert carried["day"] == YESTERDAY
    # "Modifier", then the newest row is removed and the form is previewed again
    one = form(
        tk(D1),
        issue_date=[TWO_DAYS_AGO],
        issue_sig=[sig(env, D1, TWO_DAYS_AGO)],
        day=YESTERDAY,
        pb="40000",
    )
    again = post(c, "/sales/preview", one)
    assert again.status_code == 200
    assert "Vente du 20 octobre 2026" in text_of(again.text)


def test_the_choice_screen_shows_each_proposals_issue_date(env: Env) -> None:
    html = paste(env.staff_a(), todays(pnr("layover_two_pages"))).text
    text = text_of(html)
    assert text.count("Émis le 22 octobre 2026") == 2


def test_a_tampered_hidden_day_on_the_readback_is_refused_at_the_confirm(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1), **issued(env)))
    refused = confirm_with(c, r.text, "/sales", day=TWO_DAYS_AGO)
    assert refused.status_code == 403
    future = confirm_with(
        c, r.text, "/sales", issue_date="2026-10-23", issue_sig=sig(env, D1, "2026-10-23")
    )
    assert future.status_code == 400
    assert counts(env)[0] == 0


def test_the_issue_date_row_shows_the_error_next_to_the_ticket(env: Env) -> None:
    r = post(env.staff_a(), "/sales/preview", form(tk(D1), **issued(env, "2026-10-23")))
    dom = Dom.of(r.text)
    assert any(error_id.startswith("t0-issue_date") for _, error_id, _ in dom.errors)


# --- correcting and voiding an earlier day ----------


def test_staff_edit_and_void_their_sale_of_an_earlier_open_day(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    assert c.get(f"/sales/{sale_id}/edit").status_code == 200
    edited = post(
        c,
        f"/sales/{sale_id}",
        form(tk(D1), **issued(env), pb="30000", version_no="1", reason="prix corrigé"),
    )
    assert edited.status_code == 303 and latest(env, sale_id).version_no == 2
    assert latest(env, sale_id).pb == 30000
    voided = post(c, f"/sales/{sale_id}/void", {"reason": "saisie en double", "version_no": "2"})
    assert voided.status_code == 303 and latest(env, sale_id).voided


def test_staff_cannot_edit_or_void_a_sale_of_a_closed_day(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    close_day(env, YESTERDAY)
    page = c.get(f"/sales/{sale_id}")
    assert f'href="/sales/{sale_id}/edit"' not in page.text
    edit = c.get(f"/sales/{sale_id}/edit")
    assert CLOSED_21 in text_of(edit.text)
    data = form(tk(D1), **issued(env), pb="30000", version_no="1", reason="correction")
    r = post(c, f"/sales/{sale_id}", data)
    assert r.status_code == 409 and CLOSED_21 in text_of(r.text)
    r = post(c, f"/sales/{sale_id}/void", {"reason": "saisie en double", "version_no": "1"})
    assert r.status_code == 409 and CLOSED_21 in text_of(r.text)
    assert latest(env, sale_id).version_no == 1


def test_the_edit_form_keeps_the_issue_date(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    html = c.get(f"/sales/{sale_id}/edit").text
    assert values(html, "issue_date") == [YESTERDAY]
    assert "Émis le 21 octobre 2026" in text_of(html)


# --- flags: a ticket that was not sent ----------


def test_the_form_of_a_not_sent_ticket_files_it_under_the_report_day(env: Env) -> None:
    # the entry form of a flag is covered with the flags (tests/test_approval_pages.py); here the
    # shape it relies on: an issue date alone decides the day for staff
    c = env.staff_a()
    r = post(c, "/sales", form(tk(D1), **issued(env, TWO_DAYS_AGO)))
    assert day_of(env, sale_id_of(r)) == TWO_DAYS_AGO


# --- the issue date is bound to what the server read (security review) ----------


def values_to_form(html: str) -> dict[str, Any]:
    """What the paste page's form posts to the read-back (its inputs, plus the answers)."""
    names = (*TICKET_FIELDS, "source", "ag_proposed", "ag_text", "ag_match", "pb_read")
    data: dict[str, Any] = {name: values(html, name) for name in names}
    rows = len(data["doc_number"])
    data.update({"pax_type": ["ADT"] * rows, "trnc": ["TKTT"] * rows})  # selects, not inputs
    data.update({"clt": "Client Synth", "pb": "5400", "payment_method": "cash"})
    return data


def one_sale_day(env: Env, r: Any) -> str:
    assert r.status_code == 303, r.text[:300]
    return day_of(env, sale_id_of(r))


def test_a_forged_issue_date_without_a_signature_files_the_sale_today(env: Env) -> None:
    c = env.staff_a()
    forged = form(tk(D1), issue_date=[TWO_DAYS_AGO])
    preview = post(c, "/sales/preview", forged)
    assert preview.status_code == 200 and "Vente du 22 octobre 2026" in text_of(preview.text)
    assert set(values(preview.text, "issue_date")) == {""}
    assert one_sale_day(env, post(c, "/sales", forged)) == TODAY


def test_a_wrong_signature_is_ignored_not_an_error(env: Env) -> None:
    c = env.staff_a()
    for bad in ("", "0" * 64, "zz", "x" * 5000, sig(env, D1, YESTERDAY).upper()[:-1]):
        r = post(c, "/sales", form(tk(D1), issue_date=[TWO_DAYS_AGO], issue_sig=[bad]))
        assert r.status_code == 303, bad
        assert day_of(env, sale_id_of(r)) == TODAY
        post(c, f"/sales/{sale_id_of(r)}/void", {"reason": "essai répété", "version_no": "1"})


def test_a_signature_of_another_date_user_agency_or_doc_does_not_count(env: Env) -> None:
    c = env.staff_a()
    other_user = env.seeded.manager_a
    other_agency = env.seeded.agency_a + 999
    wrong = {
        "date": sig(env, D1, YESTERDAY),  # signed for the 21st, posted with the 20th
        "user": sig(env, D1, TWO_DAYS_AGO, user=other_user),
        "agency": sig(env, D1, TWO_DAYS_AGO, agency=other_agency),
        "doc": sig(env, D2, TWO_DAYS_AGO),
    }
    for why, bad in wrong.items():
        data = form(tk(D1), issue_date=[TWO_DAYS_AGO], issue_sig=[bad])
        r = post(c, "/sales", data)
        assert r.status_code == 303 and day_of(env, sale_id_of(r)) == TODAY, why
        post(c, f"/sales/{sale_id_of(r)}/void", {"reason": "essai répété", "version_no": "1"})


def test_a_signature_travels_with_its_own_ticket_number_even_when_formatted(env: Env) -> None:
    c = env.staff_a()
    spaced = f"199-{D1}"
    r = post(c, "/sales", form(tk(spaced), **issued(env, TWO_DAYS_AGO, D1)))
    assert one_sale_day(env, r) == TWO_DAYS_AGO


def test_a_valid_signed_date_files_the_sale_on_that_day(env: Env) -> None:
    c = env.staff_a()
    assert one_sale_day(env, post(c, "/sales", form(tk(D1), **issued(env, TWO_DAYS_AGO)))) == (
        TWO_DAYS_AGO
    )


def test_a_pasted_booking_is_signed_and_the_signature_survives_readback_and_confirm(
    env: Env,
) -> None:
    c = env.staff_a()
    html = paste(c, todays(one_passenger())).text
    assert values(html, "issue_sig") != [""]
    (doc,) = values(html, "doc_number")
    assert values(html, "issue_sig") == [sig(env, doc, TODAY)]  # today's tickets are still signed
    r = post(c, "/sales/preview", {**values_to_form(html), "ag_answer": "direct"})
    assert r.status_code == 200, text_of(r.text)[:900]
    assert "Vente du 22 octobre 2026" in text_of(r.text)
    done = confirm_with(c, r.text, "/sales")
    assert one_sale_day(env, done) == TODAY


def test_an_edited_date_on_the_readback_loses_its_signature_and_goes_today(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1), **issued(env)))
    done = confirm_with(c, r.text, "/sales", issue_date=TWO_DAYS_AGO, day="")  # date forged
    assert one_sale_day(env, done) == TODAY


def test_a_typed_row_has_no_issue_date_so_today(env: Env) -> None:
    assert one_sale_day(env, post(env.staff_a(), "/sales", form(tk(D1)))) == TODAY


def test_the_manager_keeps_the_day_field_and_needs_no_signature(env: Env) -> None:
    c = env.manager_a()
    r = post(
        c, "/sales", form(tk(D1), issue_date=[TWO_DAYS_AGO], day=TWO_DAYS_AGO, staff_sign=SIGN)
    )
    assert one_sale_day(env, r) == TWO_DAYS_AGO
    other = post(c, "/sales", form(tk(D2), day=YESTERDAY, staff_sign=SIGN))
    assert one_sale_day(env, other) == YESTERDAY  # any open day, as before


def test_the_manager_sees_the_day_field_prefilled_and_signatures_are_harmless(env: Env) -> None:
    html = paste(env.manager_a(), todays(one_passenger())).text
    assert values(html, "day") == [TODAY]


def test_a_forged_date_on_the_choice_screen_flow_is_ignored_too(env: Env) -> None:
    c = env.staff_a()
    html = paste(c, todays(pnr("layover_two_pages"))).text
    from tests.test_sale_paste_batch import offered, open_proposal

    # the list carries the proposals signed; each one opens with its rows' signed dates
    for proposal in offered(html):
        opened = open_proposal(c, proposal).text
        assert values(opened, "issue_date") == [TODAY, TODAY]
        assert all(values(opened, "issue_sig"))
    # a date forged on the opened form loses its signature: the sale goes to today
    form = values_to_form(open_proposal(c, offered(html)[0]).text)
    form.update({"issue_date": [TWO_DAYS_AGO, TWO_DAYS_AGO], "ag_answer": "direct"})
    r = post(c, "/sales/preview", form)
    assert r.status_code == 200, text_of(r.text)[:900]
    assert one_sale_day(env, confirm_with(c, r.text, "/sales")) == TODAY


# --- reasons and kept dates on edit ----------


def test_staff_need_a_reason_to_edit_a_sale_of_an_earlier_day(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    edit_page = c.get(f"/sales/{sale_id}/edit").text
    assert "Motif de la modification (facultatif)" not in text_of(edit_page)
    assert "required" in edit_page.split('id="reason"')[1].split(">")[0]
    for bad in ("", "abc"):
        data = form(tk(D1), **issued(env), pb="30000", version_no="1", reason=bad)
        for path in (f"/sales/{sale_id}/preview-edit", f"/sales/{sale_id}"):
            r = post(c, path, data, via=f"/sales/{sale_id}/edit")
            assert r.status_code == 400 and "motif" in text_of(r.text).lower(), path
    assert latest(env, sale_id).version_no == 1
    good = form(tk(D1), **issued(env), pb="30000", version_no="1", reason="prix corrigé")
    ok = post(c, f"/sales/{sale_id}", good, via=f"/sales/{sale_id}/edit")
    assert ok.status_code == 303
    assert latest(env, sale_id).version_no == 2 and latest(env, sale_id).reason == "prix corrigé"


def test_staff_edit_of_todays_sale_needs_no_reason(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    edit_page = c.get(f"/sales/{sale_id}/edit").text
    assert "Motif de la modification (facultatif)" in text_of(edit_page)
    r = post(c, f"/sales/{sale_id}", form(tk(D1), pb="30000", version_no="1"))
    assert r.status_code == 303 and latest(env, sale_id).version_no == 2


def test_a_staff_edit_cannot_move_a_tickets_issue_date(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    for fields in (
        {"issue_date": [TWO_DAYS_AGO]},  # forged, unsigned
        {"issue_date": [TWO_DAYS_AGO], "issue_sig": [sig(env, D1, TWO_DAYS_AGO)]},  # signed
        {"issue_date": [""]},
    ):
        version = latest(env, sale_id).version_no
        data = form(tk(D1), pb=str(30000 + version), version_no=str(version), reason="correction")
        r = post(c, f"/sales/{sale_id}", {**data, **fields}, via=f"/sales/{sale_id}/edit")
        assert r.status_code == 303, fields
        assert latest(env, sale_id).tickets[0]["issue_date"] == YESTERDAY, fields
        assert day_of(env, sale_id) == YESTERDAY


def test_a_ticket_added_in_an_edit_gets_no_date_unless_signed(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    data = form(
        tk(D1), tk(D2), issue_date=[YESTERDAY, TWO_DAYS_AGO], pb="40000", version_no="1",
        reason="billet oublié",
    )  # fmt: skip
    r = post(c, f"/sales/{sale_id}", data, via=f"/sales/{sale_id}/edit")
    assert r.status_code == 303
    by_doc = {t["doc_number"]: t.get("issue_date") for t in latest(env, sale_id).tickets}
    assert by_doc == {D1: YESTERDAY, D2: None}


def test_staff_void_of_an_earlier_day_still_needs_a_reason(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1), **issued(env))
    r = post(c, f"/sales/{sale_id}/void", {"reason": "", "version_no": "1"})
    assert r.status_code == 400 and not latest(env, sale_id).voided


# --- an old issue date is not read (decision 25 replaced the "ancienne" notice) ----------


def test_a_very_old_issue_date_is_ignored_like_any_earlier_day(env: Env) -> None:
    old = one_passenger().replace("21OCT26/NKC", "25JUN19/NKC")  # a typo such as 25JUN19
    text = text_of(paste(env.staff_a(), old).text)
    assert "1 billet(s) émis avant aujourd'hui ignoré(s) : 3940299481" in text
    assert "Date d'émission ancienne" not in text
