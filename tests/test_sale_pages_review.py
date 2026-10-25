"""B5 review follow-ups: ticket-number normalisation on the pages, the history without phones, the
void version, duplicate rows, the partner price block, anchors and the error summary, the mobile
bar, the looser preview limit and the double submit. Helpers come from test_sale_pages.py."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import ratelimit
from app.models import Sale, SaleTicket
from tests.page_support import Env
from tests.test_sale_pages import (
    D1,
    D2,
    SIGN,
    STAFF2_PHONE,
    TODAY,
    Dom,
    accountant,
    confirm_with,
    counts,
    enter,
    form,
    latest,
    make_partner,
    post,
    sale_id_of,
    staff2,
    table,
    text_of,
    tk,
)
from tests.test_sale_pages import env as env  # the fixture
from tests.web_support import PASSWORD_S, add_user

__all__ = ["env"]

CSS = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "app.css").read_text(
    encoding="utf-8"
)
REAL = "3940281465"  # a 10-digit number as the Amadeus export holds it


# --- ticket numbers on the pages ----------------------------------------------------------------


@pytest.mark.parametrize("typed", ["495-3940281465", "4953940281465", "495 3940281465", REAL])
def test_a_prefixed_number_is_read_back_and_stored_as_ten_digits(env: Env, typed: str) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(typed, airline="495")))
    assert r.status_code == 200
    text = text_of(r.text)
    assert REAL in text and "495" + REAL not in text
    sale_id = sale_id_of(confirm_with(c, r.text, "/sales"))
    assert [t.doc_number for t in table(env, SaleTicket)] == [REAL]
    assert latest(env, sale_id).tickets[0]["doc_number"] == REAL


def test_the_prefixed_and_plain_forms_of_a_number_are_the_same_ticket(env: Env) -> None:
    c = env.staff_a()
    first = enter(c, tk("495-3940281465", airline="495"))
    r = post(c, "/sales/preview", form(tk(REAL, airline="495")))
    assert r.status_code == 409 and f'href="/sales/{first}/edit"' in r.text
    other = staff2(env)
    r = post(other, "/sales/preview", form(tk("4953940281465", airline="495")))
    assert r.status_code == 409 and "autre agent" in r.text
    assert len(table(env, Sale)) == 1


def test_a_prefix_that_does_not_match_the_airline_is_refused(env: Env) -> None:
    r = post(env.staff_a(), "/sales/preview", form(tk("495-3940281465", airline="199")))
    assert r.status_code == 400
    assert "Le préfixe ne correspond pas à la compagnie (A/L)" in r.text
    assert counts(env) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("typed", ["394028146", "39402814651", "495394028146", "49539402814651"])
def test_numbers_of_other_lengths_are_refused(env: Env, typed: str) -> None:
    r = post(env.staff_a(), "/sales/preview", form(tk(typed, airline="495")))
    assert r.status_code == 400 and "10 chiffres" in r.text


# --- history: never a phone ---------------------------------------------------------------------


def test_the_history_shows_a_name_or_a_role_never_a_phone(env: Env) -> None:
    uid = add_user(env.db.owner_url, env.seeded.agency_a, STAFF2_PHONE, display_name="")
    env.link(uid, "0006AC")
    nameless = env.as_user(STAFF2_PHONE, PASSWORD_S)
    sale_id = enter(nameless, tk(D1))
    m = env.manager_a()
    html = m.get(f"/sales/{sale_id}").text
    assert STAFF2_PHONE not in html and STAFF2_PHONE.lstrip("+") not in html
    assert "Employé" in text_of(html)  # the role, in the screen's own wording
    named = env.staff_a()
    other = enter(named, tk(D2))
    assert "Staff A" in text_of(m.get(f"/sales/{other}").text)


# --- void needs the version ---------------------------------------------------------------------


@pytest.mark.parametrize("version", [None, "", "abc", "0", "-3"])
def test_a_void_without_a_usable_version_is_refused(env: Env, version: str | None) -> None:
    c = env.staff_a()
    sale_id = enter(c)
    data = {"reason": "saisie en double"}
    if version is not None:
        data["version_no"] = version
    r = post(c, f"/sales/{sale_id}/void", data)
    assert r.status_code == 400 and "rechargez la page" in r.text
    assert [s.status for s in table(env, Sale)] == ["active"]


# --- duplicates mark the row --------------------------------------------------------------------


def test_a_duplicate_ticket_marks_the_row_that_holds_it(env: Env) -> None:
    c = env.staff_a()
    enter(c, tk(D2))
    r = post(c, "/sales/preview", form(tk(D1), tk(D2)))
    assert r.status_code == 409
    dom = Dom.of(r.text)
    assert dom.invalid == [(1, "ticket-2")]
    assert [(row, ident) for row, ident, _ in dom.errors if row is not None] == [
        (1, "t1-doc_number-error")
    ]
    assert 'href="#ticket-2"' in r.text  # the summary points at it


def test_the_same_number_twice_in_one_sale_marks_the_second_row(env: Env) -> None:
    r = post(env.staff_a(), "/sales/preview", form(tk(D1), tk(D1)))
    assert r.status_code == 400
    dom = Dom.of(r.text)
    assert dom.invalid == [(1, "ticket-2")]
    assert "déjà dans cette vente" in text_of(r.text)


# --- the partner price block --------------------------------------------------------------------


def test_the_price_block_is_hidden_by_css_when_a_partner_is_chosen(env: Env) -> None:
    html = env.staff_a().get("/sales/new").text
    assert 'class="field field-pb"' in html and 'class="card sale-card"' in html
    assert 'aria-describedby="pb-help"' in html and 'id="pb-help"' in html
    rule = re.search(
        r"\.sale-card:has\(select#ag option:not\(\[value=\"direct\"\]\):checked\)"
        r" \.field-pb,\s*"
        r"\.sale-card:has\(#ag_answer_yes\[data-partner\]:checked\) \.field-pb,\s*"
        r"\.sale-card:has\(#ag_answer_other:checked\) \.field-pb\s*\{([^}]*)\}",
        CSS,
    )
    assert rule and "display: none" in rule.group(1)
    # The agency question is a fieldset (id "ag") holding the "Autre partenaire" list, whose empty
    # "Choisir…" option is selected: a rule on "#ag option" hid P.B there, even for a direct sale.
    assert ".sale-card:has(#ag option" not in CSS


def test_a_price_typed_for_a_partner_sale_is_called_out_not_dropped_silently(env: Env) -> None:
    pid = make_partner(env, "BADI", "500")
    c = env.staff_a()
    base = form(tk(D1, "1000"), ag=str(pid), payment_method="credit")
    base.pop("clt")
    typed = post(c, "/sales/preview", {**base, "pb": "9999"})
    assert typed.status_code == 200 and "Prix saisi ignoré : prix partenaire calculé" in typed.text
    clean = post(c, "/sales/preview", {**base, "pb": ""})
    assert "Prix saisi ignoré" not in clean.text


# --- anchors, the error summary, labels ---------------------------------------------------------


def test_add_a_row_posts_to_an_anchor_on_the_new_rows_first_input(env: Env) -> None:
    c = env.staff_a()
    html = c.get("/sales/new").text
    assert 'formaction="/sales/preview#ticket-2"' in html
    grown = post(c, "/sales/preview", form(tk(D1), add_row="1"))
    dom = Dom.of(grown.text)
    assert "ticket-2" in dom.ids and 'formaction="/sales/preview#ticket-3"' in grown.text


def test_the_error_summary_comes_first_and_links_to_the_fields(env: Env) -> None:
    r = post(env.staff_a(), "/sales/preview", form(tk(D1, "abc"), clt="", pb=""))
    dom = Dom.of(r.text)
    assert r.text.index('id="form-error"') < r.text.index('<form class="form form-wide"')
    assert "autofocus" not in r.text
    links = re.findall(r'<a href="#([^"]+)">', r.text)
    assert {"total_doc_0", "clt", "pb"} <= set(links)
    for target in links:
        assert target in dom.ids, target


def test_every_control_has_a_label_and_every_description_exists(env: Env) -> None:
    c = env.staff_a()
    pages = [
        c.get("/sales/new").text,
        post(c, "/sales/preview", form(tk(D1, "abc"), clt="", pb="", ap="1")).text,
    ]
    sale_id = enter(c, tk(D2))
    pages.append(c.get(f"/sales/{sale_id}/edit").text)
    pages.append(c.get(f"/sales/{sale_id}/void").text)
    for html in pages:
        dom = Dom.of(html)
        for tag, attrs, _ in dom.controls:
            if attrs.get("id") in ("q",):  # the search box has its own visually hidden label
                continue
            ident = attrs.get("id", "")
            assert ident in dom.label_for, (tag, attrs)
        for html_attrs in re.findall(r'aria-describedby="([^"]+)"', html):
            for target in html_attrs.split():
                assert target in dom.ids, target


def test_the_amadeus_labels_come_with_a_french_hint(env: Env) -> None:
    text = text_of(env.staff_a().get("/sales/new").text)
    for label in (
        "Numéro du billet (DOC NUMBER, 10 chiffres)",
        "Compagnie (A/L)",
        "Coût (TOTAL DOC)",
        "Passager (PAX NAME)",
        "Réservation (RLOC)",
        "Type de billet (TRNC)",
    ):
        assert label in text, label


def test_a_post_with_twenty_one_rows_of_which_some_are_blank_is_fine(env: Env) -> None:
    blank = tk("", "", pax_name="", rloc="", airline="")
    rows = [tk(f"39400000{n:02d}") for n in range(15)] + [blank] * 6
    r = post(env.staff_a(), "/sales/preview", form(*rows))
    assert r.status_code == 200


# --- edit: foreign id, inactive partner ---------------------------------------------------------


def test_the_back_to_form_post_of_an_edit_is_a_404_for_a_foreign_sale(env: Env) -> None:
    sale_id = enter(env.staff_a(), tk(D1))
    other_agency = post(env.manager_b(), f"/sales/{sale_id}/edit", form(version_no="1"))
    assert other_agency.status_code == 404
    other_sign = post(staff2(env), f"/sales/{sale_id}/edit", form(version_no="1"))
    assert other_sign.status_code == 404


def test_editing_a_partner_sale_keeps_a_partner_that_has_been_deactivated(env: Env) -> None:
    pid = make_partner(env, "BADI", "500")
    c = env.staff_a()
    base = form(tk(D1, "1000"), ag=str(pid), payment_method="credit")
    base.pop("clt")
    sale_id = sale_id_of(post(c, "/sales", base))
    m = env.manager_a()
    post(m, f"/partners/{pid}/deactivate", {}, via="/partners")
    page = c.get(f"/sales/{sale_id}/edit")
    assert page.status_code == 200
    assert re.search(rf'<option value="{pid}" selected>BADI</option>', page.text)
    # a new sale cannot name it any more
    assert "BADI" not in c.get("/sales/new").text
    edited = post(c, f"/sales/{sale_id}", {**base, "total_doc": ["2000"], "version_no": "1"})
    assert edited.status_code == 303
    assert latest(env, sale_id).pb == 2500


# --- the mobile bar -----------------------------------------------------------------------------


def _tabs(html: str) -> list[str]:
    bar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    return re.findall(r'<a class="tab"', bar)


def test_no_role_has_more_than_five_bottom_tabs(env: Env) -> None:
    env.load()
    for client in (env.staff_a(), env.manager_a(), accountant(env)):
        assert 3 <= len(_tabs(client.get("/").text)) <= 5


def test_the_pending_questions_stay_visible_on_the_staff_dashboard_tab(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    html = env.staff_a().get("/").text
    bar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    home = bar.split("</a>")[0]
    assert 'class="badge"' in home and "en attente" in home
    assert 'href="/questions"' not in bar
    # the managers keep their Questions tab and its badge
    manager_bar = env.manager_a().get("/").text.split('<nav class="tabbar"')[1]
    assert manager_bar.count('class="badge"') == 1


def test_the_sales_tabs_have_their_own_icon_not_the_cash_one(env: Env) -> None:
    html = env.staff_a().get("/").text
    bar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    mine = re.search(r'href="/sales"[^>]*>(.*?)</a>', bar, re.S)
    assert mine and "M8 6h13" in mine.group(1) and "circle" not in mine.group(1)


# --- limits and double submit -------------------------------------------------------------------


def test_read_backs_have_their_own_looser_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (ratelimit.SALE_PREVIEW_USER.max_hits, ratelimit.SALE_PREVIEW_USER.window_seconds) == (
        1000,
        3600,
    )
    monkeypatch.setattr(
        ratelimit, "SALE_PREVIEW_USER", ratelimit.Limit("sale_preview_user", 2, 3600)
    )
    c = env.staff_a()
    assert post(c, "/sales/preview", form()).status_code == 200
    assert post(c, "/sales/preview", form(add_row="1")).status_code == 200
    r = post(c, "/sales/preview", form())
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert post(c, "/sales", form()).status_code == 303  # saving has its own budget


def test_confirming_twice_goes_to_the_sale_already_created(env: Env) -> None:
    c = env.staff_a()
    preview = post(c, "/sales/preview", form(tk(D1)))
    first = confirm_with(c, preview.text, "/sales")
    second = confirm_with(c, preview.text, "/sales")
    assert first.status_code == 303 and second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert len(table(env, Sale)) == 1


def test_a_second_submit_much_later_or_of_other_content_is_still_a_duplicate(env: Env) -> None:
    c = env.staff_a()
    preview = post(c, "/sales/preview", form(tk(D1)))
    confirm_with(c, preview.text, "/sales")
    other = post(c, "/sales", form(tk(D1), tk(D2)))
    assert other.status_code == 409  # same ticket, different sale: not the same submit
    env.clock.advance(minutes=10)
    late = confirm_with(c, preview.text, "/sales")
    assert late.status_code == 409 and "Modifier la vente existante" in late.text


def test_another_users_sale_is_never_taken_for_my_double_submit(env: Env) -> None:
    enter(env.manager_a(), tk(D1), staff_sign=SIGN, day=TODAY)
    r = post(env.staff_a(), "/sales", form(tk(D1)))
    assert r.status_code == 409


def test_an_edited_sale_is_not_a_double_submit_target(env: Env) -> None:
    c: TestClient = env.staff_a()
    sale_id = enter(c, tk(D1))
    post(c, f"/sales/{sale_id}", form(tk(D1), pb="25000", version_no="1"))
    again = post(c, "/sales", form(tk(D1)))
    assert again.status_code == 409  # version 2 now: not "just created"
