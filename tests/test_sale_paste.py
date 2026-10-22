"""B6 wiring: the paste box, the prefilled forms, the agency question, partner aliases.

Synthetic PNR texts only (tests/fixtures/pnr). Nothing a paste does writes anything; the sale is
written by the same confirm step as a typed one, which validates everything again.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import ratelimit
from app.models import (
    ALIAS_APPROVED,
    ALIAS_PENDING,
    ALIAS_REJECTED,
    AuditEvent,
    PartnerAlias,
    Sale,
    SaleVersion,
)
from tests.page_support import Env, fr
from tests.test_sale_pages import (
    SIGN,
    TICKET_FIELDS,
    Dom,
    accountant,
    counts,
    hidden,
    post,
    sale_id_of,
    table,
    text_of,
)
from tests.web_support import WebDb

FIXTURES = Path(__file__).parent / "fixtures" / "pnr"
REND = "REND TOURISME ET VOYAGES"


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    return e


def pnr(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def one_passenger() -> str:
    """The three-adult fixture cut down to its first passenger."""
    lines = pnr("three_adults_rm").splitlines()
    keep = [
        line
        for line in lines
        if not re.match(r"\s*(10|11) FA|\s+1381/S4/P[23]", line)  # the FA lines of passengers 2, 3
    ]
    text = "\n".join(keep)
    text = text.replace("1.SYNTHA/ONE   2.SYNTHB/TWO   3.SYNTHC/THREE", "1.SYNTHA/ONE")
    return text.replace("HK3", "HK1")


def paste(c: TestClient, text: str) -> Any:
    return post(c, "/sales/paste", {"pnr_text": text})


def no_inline_code(html: str) -> bool:
    """No <style>, style attribute, inline <script> or event handler (the CSP forbids them)."""
    return not re.search(r"<style|\sstyle=|<script(?![^>]*\ssrc=)|\son[a-z]+=", html)


def values(html: str, name: str) -> list[str]:
    """The values of the inputs called ``name`` (text and hidden), in page order."""
    found = []
    for tag in re.findall(r"<input[^>]*>", html):
        if re.search(rf'name="{name}"', tag):
            match = re.search(r'value="([^"]*)"', tag)
            found.append(match.group(1) if match else "")
    return found


def checked(html: str, value: str) -> bool:
    for tag in re.findall(r'<input[^>]*name="ag_answer"[^>]*>', html):
        if f'value="{value}"' in tag:
            return " checked" in tag
    return False  # no such radio (nothing to answer Oui to)


def partner(env: Env, name: str, fee: str | None = "500") -> int:
    from tests.test_sale_pages import make_partner

    return make_partner(env, name, fee)


# --- the paste box ------------------------------------------------------------------------------


def test_the_entry_page_has_a_paste_box(env: Env) -> None:
    html = env.staff_a().get("/sales/new").text
    assert "Coller le texte Amadeus" in html
    assert 'action="/sales/paste"' in html and 'name="pnr_text"' in html
    assert "<textarea" in html
    assert no_inline_code(html)


def test_one_passenger_gives_the_prefilled_form_and_writes_nothing(env: Env) -> None:
    c = env.staff_a()
    before = counts(env)
    r = paste(c, one_passenger())
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    html = r.text
    assert values(html, "doc_number") == ["3940299481"]
    assert values(html, "airline") == ["495"]
    assert values(html, "total_doc") == ["5022"]
    assert values(html, "pax_name") == ["SYNTHA/ONE"]
    assert values(html, "rloc") == ["ZX6AGG"]
    assert values(html, "pb") == ["5400"]  # the RM price of a direct sale
    assert values(html, "clt") == ["SYNTHCLIENT PAYEUR"]
    assert values(html, "staff_sign") == [SIGN]  # the agent's own sign, not the text's "SV"
    assert values(html, "source") == ["paste"]
    assert 'action="/sales/preview"' in html
    assert counts(env) == before


def test_the_pasted_text_is_not_kept_anywhere(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    c = env.staff_a()
    with caplog.at_level("DEBUG"):
        r = paste(c, one_passenger())
    assert "ZX6AGG ZX6AGG" not in r.text and one_passenger() not in r.text
    assert "3940299481" not in caplog.text and "SYNTHCLIENT" not in caplog.text
    events = [e for e in table(env, AuditEvent) if "paste" in e.event_type]
    assert events == []
    for event in table(env, AuditEvent):
        assert "SYNTH" not in str(event.details) and "3940299481" not in str(event.details)


def test_three_adults_give_three_proposals_each_opens_a_prefilled_form(env: Env) -> None:
    c = env.staff_a()
    r = paste(c, pnr("three_adults_rm"))
    assert r.status_code == 200
    text = text_of(r.text)
    assert text.count("Saisir cette vente") == 3
    for name in ("SYNTHA/ONE", "SYNTHB/TWO", "SYNTHC/THREE"):
        assert name in text
    for doc in ("3940299481", "3940299482", "3940299483"):
        assert doc in text
    assert fr(5400) in text
    # each button carries its proposal; pressing the second one opens that passenger's form
    forms = re.findall(
        r'<form class="inline-form" method="post" action="/sales/new">.*?</form>', r.text, re.S
    )
    assert len(forms) == 3
    second = forms[1]
    fields = re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', second)
    data: dict[str, list[str]] = {}
    for key, value in fields:
        data.setdefault(key, []).append(value)
    opened = c.post("/sales/new", data=data, follow_redirects=False)
    assert opened.status_code == 200
    assert values(opened.text, "doc_number") == ["3940299482"]
    assert values(opened.text, "pax_name") == ["SYNTHB/TWO"]
    assert values(opened.text, "pb") == ["5400"]


def test_a_layover_is_two_proposals_of_two_tickets_with_the_same_price(env: Env) -> None:
    r = paste(env.staff_a(), pnr("layover_two_pages"))
    text = text_of(r.text)
    assert text.count("Saisir cette vente") == 2
    assert text.count(fr(51300)) == 2
    assert "3940299469" in text and "3940299471" in text and "3940299470" in text


def test_infants_have_their_own_proposals_with_the_infant_price(env: Env) -> None:
    r = paste(env.staff_a(), pnr("adults_with_infants"))
    text = text_of(r.text)
    assert text.count("Saisir cette vente") == 4
    assert text.count("SYNTHD/BABYONE") >= 1 and text.count("SYNTHE/BABYTWO") >= 1
    assert fr(900) in text and fr(5400) in text


def test_reader_warnings_are_shown_on_the_form(env: Env) -> None:
    c = env.staff_a()
    exchange = paste(c, pnr("exchange"))
    assert exchange.status_code == 200
    assert "échange : vérifier le prix" in text_of(exchange.text)
    emd = paste(c, pnr("emd"))
    assert "EMD" in text_of(emd.text)
    assert "montant non remboursable" in text_of(emd.text)


def test_a_booking_without_tickets_is_an_error_and_the_form_stays_blank(env: Env) -> None:
    r = paste(
        env.staff_a(), "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n  1.SYNTHZ/ONE\n"
    )
    assert r.status_code == 400
    assert "Aucune vente lisible" in r.text and 'action="/sales/preview"' in r.text


@pytest.mark.parametrize("text", ["", "   \n  ", "garbage with no PNR in it"])
def test_nothing_to_read_is_a_french_error(env: Env, text: str) -> None:
    r = paste(env.staff_a(), text)
    assert r.status_code == 400
    assert "Collez le texte" in r.text or "Aucune vente lisible" in r.text


def test_oversize_text_is_refused_before_parsing(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.web.sale_paste as paste_module

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("the text must not be parsed")

    monkeypatch.setattr(paste_module, "parse_pnr_text", boom)
    c = env.staff_a()
    r = paste(c, "A" * (16 * 1024 + 1))
    assert r.status_code == 400
    assert "Texte trop long" in r.text
    # the limit is in bytes: 8 193 two-byte characters are 16 386 bytes
    assert paste(c, "é" * 8193).status_code == 400


def test_text_at_the_limit_is_read(env: Env) -> None:
    text = one_passenger() + "\n" + "X" * 100
    text = text + " " * (16 * 1024 - len(text.encode()))
    assert len(text.encode()) == 16 * 1024
    r = paste(env.staff_a(), text)
    assert r.status_code in (200, 400) and "Texte trop long" not in r.text


def test_paste_needs_csrf_and_a_sales_user(env: Env) -> None:
    c = env.staff_a()
    assert c.post("/sales/paste", data={"pnr_text": one_passenger()}).status_code == 403
    assert (
        c.post("/sales/paste", data={"pnr_text": one_passenger(), "csrf_token": "x"}).status_code
        == 403
    )
    anonymous = env.factory()
    assert anonymous.post("/sales/paste", data={"pnr_text": "x"}).status_code in (303, 401, 403)


def test_the_paste_has_a_per_user_rate_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ratelimit, "SALE_PREVIEW_USER", ratelimit.Limit("sale_preview_user", 2, 3600)
    )
    c = env.staff_a()
    assert paste(c, one_passenger()).status_code == 200
    assert paste(c, one_passenger()).status_code == 200
    r = paste(c, one_passenger())
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_the_manager_and_the_accountant_get_the_known_sign_not_a_free_one(env: Env) -> None:
    for c in (env.manager_a(), accountant(env)):
        r = paste(c, one_passenger())
        assert r.status_code == 200
        assert values(r.text, "staff_sign") == [SIGN]  # "SV" stands for the known sign 0004SV


def test_a_sign_that_is_not_theirs_is_a_hint_only_for_staff(env: Env) -> None:
    text = one_passenger().replace("SV/SU", "ZZ/SU")
    c = env.staff_a()
    r = paste(c, text)
    assert values(r.text, "staff_sign") == [SIGN]
    assert "Signe du PNR : ZZ" in text_of(r.text)
    # the confirm still refuses a sign that is not theirs (B5 rules)
    data = {name: values(r.text, name) for name in TICKET_FIELDS}
    data["ag_answer"] = ["direct"]
    data["staff_sign"] = ["0009ZZ"]
    data["source"] = ["paste"]
    data["clt"] = ["Client"]
    data["pb"] = ["5400"]
    data["payment_method"] = ["cash"]
    assert post(c, "/sales", data).status_code in (403, 404, 400)
    assert table(env, Sale) == []


# --- the agency question ------------------------------------------------------------------------


def test_an_exact_partner_is_proposed_with_its_fee_but_nothing_is_pre_answered(env: Env) -> None:
    partner(env, REND, "500")
    r = paste(env.staff_a(), pnr("partner_no_rm"))
    assert r.status_code == 200
    text = text_of(r.text)
    assert f"Agence : {REND}." in text
    assert f"Reconnue comme partenaire {REND} (frais 500 MRU/billet)" in text
    assert "C'est bien ce partenaire ?" in text
    for value in ("yes", "other", "direct"):
        assert not checked(r.text, value)  # the agent always answers
    assert "Vente directe BRUGE" in text and "Non, choisir un autre partenaire" in text
    assert 'class="field ask-agency"' in r.text  # exact: no warning style
    assert values(r.text, "pb") == [""]  # a partner sale: the price is computed


def test_a_prefix_match_is_in_warning_style_and_not_preselected(env: Env) -> None:
    partner(env, "REND", "300")
    r = paste(env.staff_a(), pnr("partner_no_rm"))
    text = text_of(r.text)
    assert "Reconnue comme partenaire REND (frais 300 MRU/billet)" in text
    assert f"déduit de « {REND} »" in text
    assert "ask-agency notice" in r.text
    assert not checked(r.text, "yes")


def test_a_close_name_is_a_suggestion_never_preselected(env: Env) -> None:
    partner(env, "RAND TOURISME ET VOYAGES", "400")
    r = paste(env.staff_a(), pnr("partner_no_rm"))
    text = text_of(r.text)
    assert "Vouliez-vous dire RAND TOURISME ET VOYAGES" in text
    assert "nom proche" in text
    assert not checked(r.text, "yes")
    assert "ask-agency notice" in r.text


def test_an_unknown_agency_asks_to_choose(env: Env) -> None:
    partner(env, "ZZZ AUTRE")
    r = paste(env.staff_a(), pnr("partner_no_rm"))
    text = text_of(r.text)
    assert "Aucun partenaire reconnu" in text
    assert 'id="ag_answer_yes"' not in r.text  # nothing to say Oui to
    assert not checked(r.text, "other")


def test_a_direct_booking_is_recognised_as_bruge(env: Env) -> None:
    r = paste(env.staff_a(), one_passenger())
    assert "Reconnue comme vente directe BRUGE" in text_of(r.text)
    assert not checked(r.text, "yes")
    assert 'id="ag_answer_yes"' in r.text


def test_the_choice_list_holds_the_active_partners_only(env: Env) -> None:
    from tests.test_sale_pages import make_partner

    make_partner(env, "BADI")
    old = make_partner(env, "ANCIEN")
    m = env.manager_a()
    post(m, f"/partners/{old}/deactivate", {}, via="/partners")
    html = paste(env.staff_a(), pnr("partner_no_rm")).text
    select = re.search(r'<select id="ag_other".*?</select>', html, re.S)
    assert select and "BADI" in select.group(0) and "ANCIEN" not in select.group(0)


def form_fields(html: str) -> dict[str, list[str]]:
    """Every input of the main form (text and hidden), ticket columns as lists."""
    data: dict[str, list[str]] = {}
    form = re.search(
        r'<form class="form form-wide" method="post" action="/sales/preview">(.*)', html, re.S
    )
    assert form
    for tag in re.findall(r"<input[^>]*>", form.group(1)):
        n = re.search(r'name="([^"]+)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if not n or 'type="radio"' in tag or 'type="checkbox"' in tag or n.group(1) == "csrf_token":
            continue
        data.setdefault(n.group(1), []).append(v.group(1) if v else "")
    data["pax_type"] = ["ADT"]
    data["trnc"] = ["TKTT"]
    data["payment_method"] = ["cash"]
    return data


def preview(c: TestClient, html: str, **answer: str) -> Any:
    data = form_fields(html)
    for key, value in answer.items():
        data[key] = [value]
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    return c.post("/sales/preview", data=data, follow_redirects=False)


def test_the_sale_cannot_be_checked_or_confirmed_until_the_agency_is_answered(env: Env) -> None:
    partner(env, "REND", "300")
    c = env.staff_a()
    form = paste(c, pnr("partner_no_rm")).text  # a prefix match: nothing is pre-answered
    before = counts(env)
    r = preview(c, form)
    assert r.status_code == 400
    assert "Confirmez l'agence" in text_of(r.text)
    # confirming straight away (tampered: the preview skipped) is refused the same way
    data = form_fields(form)
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    r = c.post("/sales", data=data, follow_redirects=False)
    assert r.status_code == 400 and "Confirmez l'agence" in text_of(r.text)
    assert counts(env) == before and table(env, Sale) == []
    # "other" without a partner is refused too
    r = preview(c, form, ag_answer="other")
    assert r.status_code == 400 and "Choisissez le partenaire dans la liste" in text_of(r.text)
    assert preview(c, form, ag_answer="yes").status_code == 200


def test_the_read_back_repeats_the_question_and_the_answer(env: Env) -> None:
    partner(env, "REND", "300")
    c = env.staff_a()
    form = paste(c, pnr("partner_no_rm")).text
    r = preview(c, form, ag_answer="yes")
    assert r.status_code == 200
    text = text_of(r.text)
    assert "Agence du texte collé" in text
    assert f"Agence : {REND}." in text
    assert "Reconnue comme partenaire REND (frais 300 MRU/billet)" in text
    assert "Votre réponse" in text and "Oui, c'est bien cette agence" in text
    assert 'class="card notice" aria-labelledby="ask-title"' in r.text  # not exact: warning style
    assert ("source", "paste") in hidden(r.text, "/sales")
    # back to the form keeps the answer
    back = c.post(
        "/sales/new", data=dict(hidden(r.text, "/sales/new")) | {}, follow_redirects=False
    )
    assert back.status_code == 200 and checked(back.text, "yes")  # their own answer is kept


def test_confirming_a_pasted_sale_labels_it_paste_and_prices_it_on_the_server(env: Env) -> None:
    partner(env, REND, "500")
    c = env.staff_a()
    form = paste(c, pnr("partner_no_rm")).text
    r = preview(c, form)  # exact: yes is pre-checked in the form... post it as the browser would
    data = form_fields(form)
    data["ag_answer"] = ["yes"]
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    r = c.post("/sales", data=data, follow_redirects=False)
    sale_id = sale_id_of(r)
    (version,) = [v for v in table(env, SaleVersion) if v.sale_id == sale_id]
    assert version.source == "paste"
    assert version.ag_label == REND
    assert version.pb == version.cost_total + 500  # cost + the partner's fee: the server's price


def test_a_typed_sale_is_still_labelled_form(env: Env) -> None:
    from tests.test_sale_pages import enter

    sale_id = enter(env.staff_a())
    (version,) = [v for v in table(env, SaleVersion) if v.sale_id == sale_id]
    assert version.source == "form"


def test_an_invalid_source_is_refused(env: Env) -> None:
    c = env.staff_a()
    from tests.test_sale_pages import form as sale_form

    for path in ("/sales/preview", "/sales"):
        r = post(c, path, sale_form(source="comparison"))
        assert r.status_code == 400 and "Origine" in text_of(r.text)
    assert table(env, Sale) == []


def test_a_hand_made_paste_post_needs_the_answer_too(env: Env) -> None:
    from tests.test_sale_pages import form as sale_form

    c = env.staff_a()
    r = post(c, "/sales", sale_form(source="paste"))
    assert r.status_code == 400 and "Confirmez l'agence" in text_of(r.text)
    ok = post(c, "/sales", sale_form(source="paste", ag_answer="direct"))
    assert ok.status_code == 303


# --- aliases: proposing -------------------------------------------------------------------------


def confirm_paste(env: Env, c: TestClient, **answer: str) -> Any:
    form = paste(c, pnr("partner_no_rm")).text
    data = form_fields(form)
    data.update({k: [v] for k, v in answer.items()})
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    return c.post("/sales", data=data, follow_redirects=False)


def aliases(env: Env) -> list[PartnerAlias]:
    return table(env, PartnerAlias)


def test_a_non_exact_match_confirmed_proposes_a_pending_alias(env: Env) -> None:
    pid = partner(env, "REND", "300")
    r = confirm_paste(env, env.staff_a(), ag_answer="yes")
    assert r.status_code == 303
    (alias,) = aliases(env)
    assert (alias.alias, alias.status, alias.partner_id) == (REND, ALIAS_PENDING, pid)
    assert alias.proposed_by_user_id == env.seeded.staff_a
    (event,) = [e for e in table(env, AuditEvent) if e.event_type == "alias_proposed"]
    assert event.details == {"alias_id": alias.id, "partner_id": pid}


def test_an_exact_match_or_a_direct_choice_proposes_nothing(env: Env) -> None:
    partner(env, REND, "300")
    assert confirm_paste(env, env.staff_a(), ag_answer="yes").status_code == 303
    assert aliases(env) == []


def test_choosing_another_partner_for_an_unknown_agency_proposes_it(env: Env) -> None:
    pid = partner(env, "BADI", "300")
    r = confirm_paste(env, env.staff_a(), ag_answer="other", ag_other=str(pid))
    assert r.status_code == 303
    (alias,) = aliases(env)
    assert (alias.alias, alias.partner_id, alias.status) == (REND, pid, ALIAS_PENDING)


def test_choosing_the_direct_sale_proposes_nothing(env: Env) -> None:
    partner(env, "REND", "300")
    r = confirm_paste(env, env.staff_a(), ag_answer="direct", pb="5000", clt="Client")
    assert r.status_code == 303
    assert aliases(env) == []


def test_a_pending_alias_never_matches_a_later_paste(env: Env) -> None:
    pid = partner(env, "BADI", "300")
    c = env.staff_a()
    confirm_paste(env, c, ag_answer="other", ag_other=str(pid))
    assert aliases(env)[0].status == ALIAS_PENDING
    again = paste(c, pnr("partner_no_rm").replace("3940281472", "3940281473"))
    assert "Aucun partenaire reconnu" in text_of(again.text)
    assert not checked(again.text, "yes")


# --- aliases: the manager's decisions ------------------------------------------------------------


def propose_one(env: Env, partner_name: str = "REND") -> tuple[int, int]:
    pid = partner(env, partner_name, "300")
    confirm_paste(env, env.staff_a(), ag_answer="yes")
    (alias,) = aliases(env)
    return alias.id, pid


def decide(c: TestClient, alias_id: int, action: str, **data: str) -> Any:
    return post(c, f"/partners/aliases/{alias_id}/{action}", data, via="/partners")


def test_the_partners_page_lists_pending_names_and_the_sidebar_counts_them(env: Env) -> None:
    alias_id, _ = propose_one(env)
    m = env.manager_a()
    html = m.get("/partners").text
    text = text_of(html)
    assert "Noms d'agence à valider" in text and REND in text
    assert f"/partners/aliases/{alias_id}/approve" in html
    assert re.search(
        r'class="badge">1<span class="visually-hidden"> noms d\'agence à valider', html
    )
    assert "noms d'agence à valider" not in text_of(env.staff_a().get("/sales/new").text)


def test_the_section_is_absent_without_pending_names(env: Env) -> None:
    partner(env, "REND")
    html = env.manager_a().get("/partners").text
    assert "Noms d'agence à valider" not in html
    assert "noms d'agence à valider" not in text_of(html)  # no badge either


def test_approval_makes_the_next_paste_match_exactly(env: Env) -> None:
    alias_id, pid = propose_one(env)
    m = env.manager_a()
    r = decide(m, alias_id, "approve")
    assert r.status_code == 303 and r.headers["location"] == "/partners"
    (alias,) = aliases(env)
    assert alias.status == ALIAS_APPROVED and alias.decided_by_user_id == env.seeded.manager_a
    # the booking whose agency text was not an exact name now matches exactly, through the alias
    again = paste(env.staff_a(), pnr("partner_no_rm"))
    text = text_of(again.text)
    assert "Reconnue comme partenaire REND (frais 300 MRU/billet)" in text
    assert not checked(again.text, "yes")  # never pre-answered, even for an exact match
    assert "ask-agency notice" not in again.text and "déduit de" not in text
    assert [e.event_type for e in table(env, AuditEvent) if e.event_type.startswith("alias_")] == [
        "alias_proposed",
        "alias_approved",
    ]
    assert pid


def test_an_approved_alias_of_another_name_matches_exactly(env: Env) -> None:
    pid = partner(env, "BADI", "300")
    confirm_paste(env, env.staff_a(), ag_answer="other", ag_other=str(pid))
    (alias,) = aliases(env)
    assert decide(env.manager_a(), alias.id, "approve").status_code == 303
    again = paste(env.staff_a(), pnr("partner_no_rm").replace("3940281472", "3940281473"))
    text = text_of(again.text)
    assert "Reconnue comme partenaire BADI (frais 300 MRU/billet)" in text
    assert not checked(again.text, "yes") and "ask-agency notice" not in again.text


def test_a_rejected_alias_never_matches_and_is_never_proposed_again(env: Env) -> None:
    pid = partner(env, "BADI", "300")
    c = env.staff_a()
    confirm_paste(env, c, ag_answer="other", ag_other=str(pid))
    (alias,) = aliases(env)
    assert decide(env.manager_a(), alias.id, "reject").status_code == 303
    assert aliases(env)[0].status == ALIAS_REJECTED
    again = paste(c, pnr("partner_no_rm").replace("3940281472", "3940281473"))
    assert "Aucun partenaire reconnu" in text_of(again.text) and not checked(again.text, "yes")
    # a second sale for the same text: no new proposal, the rejected row stays as it is
    data = form_fields(again.text)
    data.update({"ag_answer": ["other"], "ag_other": [str(pid)]})
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    assert c.post("/sales", data=data, follow_redirects=False).status_code == 303
    assert [(a.status, a.partner_id) for a in aliases(env)] == [(ALIAS_REJECTED, pid)]
    assert [e.event_type for e in table(env, AuditEvent) if e.event_type == "alias_proposed"] == [
        "alias_proposed"
    ]


def test_reassigning_approves_the_name_for_another_partner(env: Env) -> None:
    alias_id, _ = propose_one(env)
    other = partner(env, "BADI", "100")
    r = decide(env.manager_a(), alias_id, "reassign", partner_id=str(other))
    assert r.status_code == 303
    (alias,) = aliases(env)
    assert (alias.status, alias.partner_id) == (ALIAS_APPROVED, other)
    for bad in ("", "abc", "999999"):
        again = propose_second(env, other)
        assert decide(env.manager_a(), again, "reassign", partner_id=bad).status_code == 400


def propose_second(env: Env, pid: int) -> int:
    """Another pending alias (a new agency text, a new ticket) for partner ``pid``."""
    c = env.staff_a()
    n = len(aliases(env))
    form = paste(c, pnr("partner_no_rm").replace(REND, f"AUTRE {n}")).text
    data = form_fields(form)
    data["ag_answer"] = ["other"]
    data["ag_other"] = [str(pid)]
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    data["doc_number"] = [str(3940280000 + n)]
    assert c.post("/sales", data=data, follow_redirects=False).status_code == 303
    return max(aliases(env), key=lambda a: a.id).id


def test_an_already_decided_name_cannot_be_decided_again(env: Env) -> None:
    alias_id, _ = propose_one(env)
    m = env.manager_a()
    assert decide(m, alias_id, "approve").status_code == 303
    assert decide(m, alias_id, "approve").status_code == 400
    assert decide(m, alias_id, "reject").status_code == 400
    assert aliases(env)[0].status == ALIAS_APPROVED


def test_the_partner_page_lists_approved_names_and_removing_keeps_the_row(env: Env) -> None:
    alias_id, pid = propose_one(env)
    m = env.manager_a()
    decide(m, alias_id, "approve")
    html = m.get(f"/partners/{pid}").text
    assert "Autres noms reconnus" in html and f"/partners/aliases/{alias_id}/remove" in html
    r = post(m, f"/partners/aliases/{alias_id}/remove", {"back": str(pid)}, via="/partners")
    assert r.status_code == 303 and r.headers["location"] == f"/partners/{pid}"
    (alias,) = aliases(env)
    assert alias.status == ALIAS_REJECTED  # the row is kept
    assert f"/partners/aliases/{alias_id}/remove" not in m.get(f"/partners/{pid}").text
    assert post(m, f"/partners/aliases/{alias_id}/remove", {}, via="/partners").status_code == 400


def test_the_accountant_may_decide_too(env: Env) -> None:
    alias_id, _ = propose_one(env)
    assert decide(accountant(env), alias_id, "approve").status_code == 303


def test_staff_cannot_decide_and_nothing_changes(env: Env) -> None:
    alias_id, _ = propose_one(env)
    c = env.staff_a()
    for action in ("approve", "reject", "reassign", "remove"):
        r = post(c, f"/partners/aliases/{alias_id}/{action}", {"partner_id": "1"}, via="/sales/new")
        assert r.status_code == 403, action
    assert aliases(env)[0].status == ALIAS_PENDING


def test_deciding_needs_a_csrf_token(env: Env) -> None:
    alias_id, _ = propose_one(env)
    m = env.manager_a()
    r = m.post(f"/partners/aliases/{alias_id}/approve", follow_redirects=False)
    assert r.status_code == 403
    assert aliases(env)[0].status == ALIAS_PENDING


def test_another_agency_sees_and_decides_nothing(env: Env) -> None:
    alias_id, _ = propose_one(env)
    other = env.manager_b()
    assert REND not in other.get("/partners").text
    for action in ("approve", "reject", "remove"):
        assert decide(other, alias_id, action).status_code == 404
    assert decide(other, alias_id, "reassign", partner_id="1").status_code == 404
    assert aliases(env)[0].status == ALIAS_PENDING
    assert table(env, PartnerAlias, env.seeded.agency_b) == []


def test_an_unknown_alias_id_is_a_404(env: Env) -> None:
    assert decide(env.manager_a(), 987654, "approve").status_code == 404


def test_the_pending_section_has_no_inline_style_or_script(env: Env) -> None:
    propose_one(env)
    html = env.manager_a().get("/partners").text
    assert no_inline_code(html)


def test_the_dom_of_the_question_block_is_accessible(env: Env) -> None:
    partner(env, "REND", "300")
    html = paste(env.staff_a(), pnr("partner_no_rm")).text
    dom = Dom.of(html)
    ids = {a["id"] for tag, a, _ in dom.controls if "id" in a}
    assert {"ag_answer_yes", "ag_answer_other", "ag_answer_direct", "ag_other"} <= ids
    for control_id in ("ag_answer_yes", "ag_answer_other", "ag_answer_direct", "ag_other"):
        assert control_id in dom.label_for
    assert "<legend>" in html


def test_pasted_text_is_escaped_everywhere_it_is_shown(env: Env) -> None:
    nasty = "<script>alert(1)</script>"
    text = pnr("partner_no_rm").replace(REND, nasty)
    c = env.staff_a()
    html = paste(c, text).text
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    three = pnr("three_adults_rm").replace("BRUGE", nasty).replace("SYNTHA/ONE", "<b>A</b>/ONE")
    page = paste(c, three).text
    assert "<script>alert" not in page and "<b>A</b>" not in page


# --- review round: every pasted sale needs the agent's explicit answer -----------------------


def test_even_an_exact_match_is_refused_until_the_agent_answers(env: Env) -> None:
    partner(env, REND, "500")
    c = env.staff_a()
    form = paste(c, pnr("partner_no_rm")).text
    r = preview(c, form)
    assert r.status_code == 400 and "Confirmez l'agence" in text_of(r.text)
    assert preview(c, form, ag_answer="yes").status_code == 200
    direct = paste(c, one_passenger()).text
    assert preview(c, direct).status_code == 400
    assert preview(c, direct, ag_answer="yes").status_code == 200


@pytest.mark.parametrize("forged", ["\u00b2", "9" * 40, "-3", "0", "1e3", "\uff11", " "])
def test_a_forged_proposed_agency_is_never_a_500(env: Env, forged: str) -> None:
    c = env.staff_a()
    form = paste(c, one_passenger()).text
    for answer in (
        {"ag_proposed": forged, "ag_answer": "yes"},
        {"ag_other": forged, "ag_answer": "other"},
    ):
        r = preview(c, form, **answer)
        assert r.status_code == 400, (forged, r.status_code)
    page = c.post(
        "/sales/new",
        data={**{k: v for k, v in hidden(form, "/sales/preview")}, "ag_proposed": forged},
        follow_redirects=False,
    )
    assert page.status_code == 200
    assert table(env, Sale) == []


def test_another_agencys_or_an_inactive_partner_cannot_be_confirmed(env: Env) -> None:
    from tests.test_sale_pages import make_partner

    post(env.manager_b(), "/partners", {"name": "AUTRE B"}, via="/partners")
    foreign = _partner_ids(env, env.seeded.agency_b)[0]
    old = make_partner(env, "ANCIEN")
    post(env.manager_a(), f"/partners/{old}/deactivate", {}, via="/partners")
    c = env.staff_a()
    form = paste(c, one_passenger()).text
    for pid in (foreign, old):
        for answer in (
            {"ag_proposed": str(pid), "ag_answer": "yes"},
            {"ag_other": str(pid), "ag_answer": "other"},
        ):
            r = preview(c, form, **answer)
            assert r.status_code in (400, 404), (pid, r.status_code)
            data = form_fields(form)
            data.update({k: [v] for k, v in answer.items()})
            data["csrf_token"] = [
                c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
            ]
            assert c.post("/sales", data=data, follow_redirects=False).status_code in (400, 404)
    assert table(env, Sale) == []


def _partner_ids(env: Env, agency: int) -> list[int]:
    from app.models import Partner

    return [p.id for p in table(env, Partner, agency)]


def test_a_payment_hint_is_never_proposed_as_an_alias(env: Env) -> None:
    pid = partner(env, "REND", "300")
    text = pnr("partner_no_rm").replace(" - REND TOURISME ET VOYAGES - A", "")
    c = env.staff_a()
    form = paste(c, text).text
    assert "indication de paiement" in text_of(form) or "déduit" in text_of(form)
    data = form_fields(form)
    assert data["ag_match"] == ["fp"]
    data.update({"ag_answer": ["other"], "ag_other": [str(pid)]})
    data["csrf_token"] = [
        c.get("/sales/new").text.split('name="csrf_token" value="')[1].split('"')[0]
    ]
    assert c.post("/sales", data=data, follow_redirects=False).status_code == 303
    assert aliases(env) == []


def test_a_rejected_name_can_be_reactivated_and_is_audited(env: Env) -> None:
    alias_id, _ = propose_one(env)
    m = env.manager_a()
    decide(m, alias_id, "reject")
    page = text_of(m.get("/partners").text)
    assert (
        "Noms refusés" in page
        and f"/partners/aliases/{alias_id}/reactivate" in m.get("/partners").text
    )
    assert decide(m, alias_id, "reactivate").status_code == 303
    (alias,) = aliases(env)
    assert (alias.status, alias.decided_at, alias.decided_by_user_id) == (ALIAS_PENDING, None, None)
    assert [e.event_type for e in table(env, AuditEvent) if e.event_type.startswith("alias_")][
        -1
    ] == "alias_reactivated"
    assert decide(m, alias_id, "reactivate").status_code == 400  # only a rejected one


def test_the_pending_list_names_who_proposed_and_the_cap_is_shown(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.partner_aliases as module

    propose_one(env)
    html = text_of(env.manager_a().get("/partners").text)
    assert "Staff A" in html  # the proposer
    monkeypatch.setattr(module, "MAX_PENDING", 1)
    assert "trop de noms en attente" in text_of(env.manager_a().get("/partners").text)


def test_approving_a_name_that_became_a_partner_name_is_refused(env: Env) -> None:
    alias_id, _ = propose_one(env)
    post(env.manager_a(), "/partners", {"name": REND.lower()}, via="/partners")
    r = decide(env.manager_a(), alias_id, "approve")
    assert r.status_code == 400 and "déjà le nom d'un partenaire" in text_of(r.text)
    assert aliases(env)[0].status == ALIAS_PENDING


def test_a_remove_error_shows_the_partner_page(env: Env) -> None:
    alias_id, pid = propose_one(env)
    m = env.manager_a()
    r = post(m, f"/partners/aliases/{alias_id}/remove", {"back": str(pid)}, via="/partners")
    assert r.status_code == 400 and f'action="/partners/{pid}"' in r.text


def test_alias_decisions_share_the_write_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    alias_id, _ = propose_one(env)
    monkeypatch.setattr(ratelimit, "SALE_WRITE_USER", ratelimit.Limit("sale_write_user", 1, 3600))
    m = env.manager_a()
    assert decide(m, alias_id, "approve").status_code == 303
    assert decide(m, alias_id, "remove").status_code == 429


def test_a_failing_alias_proposal_never_undoes_the_sale(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.partner_aliases as module

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("SECRET-TEXT")

    monkeypatch.setattr(module, "propose", boom)
    partner(env, "REND", "300")
    r = confirm_paste(env, env.staff_a(), ag_answer="yes")
    assert r.status_code == 303 and len(table(env, Sale)) == 1


def test_the_edit_routes_ignore_the_paste_fields(env: Env) -> None:
    from tests.test_sale_pages import enter
    from tests.test_sale_pages import form as sale_form

    c = env.staff_a()
    sale_id = enter(c)
    r = post(
        c,
        f"/sales/{sale_id}/preview-edit",
        sale_form(source="paste", version_no="1"),
        via="/sales/new",
    )
    assert r.status_code == 200 and "Agence du texte collé" not in r.text
    (version,) = [v for v in table(env, SaleVersion) if v.sale_id == sale_id]
    assert version.source == "form"


def test_long_hidden_texts_are_clipped(env: Env) -> None:
    from starlette.datastructures import FormData

    from app.web.sale_forms import read

    data = FormData([("source", "paste"), ("ag_text", "x" * 500), ("pnr_warning", "y" * 500)])
    form = read(data)
    assert len(form.ag_text) == 120 and len(form.notices[0]) == 120
