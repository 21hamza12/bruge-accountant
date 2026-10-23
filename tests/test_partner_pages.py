"""The "Partenaires" pages: manager and accountant manage partners and fees; staff get nothing."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import partners
from app.models import ROLE_ACCOUNTANT, Partner, PartnerFee
from app.tenancy import rebind_tenant
from tests.page_support import Env
from tests.web_support import (
    PASSWORD_S,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    owner_session,
)

ACCOUNTANT_PHONE = "+22212345690"
NNBSP = " "


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def accountant(env: Env) -> TestClient:
    add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        ACCOUNTANT_PHONE,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )
    return env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)


def post(c: TestClient, path: str, **data: str) -> httpx.Response:
    token = csrf_for(c, "/partners")
    response: httpx.Response = c.post(
        path, data={**data, "csrf_token": token}, follow_redirects=False
    )
    return response


def partner_rows(env: Env, agency_id: int | None = None) -> list[Partner]:
    agency = agency_id or env.seeded.agency_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, agency)
        return list(s.scalars(select(Partner).where(Partner.agency_id == agency)))


def fee_rows(env: Env, agency_id: int | None = None) -> list[PartnerFee]:
    agency = agency_id or env.seeded.agency_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, agency)
        query = select(PartnerFee).where(PartnerFee.agency_id == agency)
        return list(s.scalars(query.order_by(PartnerFee.id)))


def make_partner(env: Env, c: TestClient, name: str = "BADI", phone: str = "") -> int:
    response = post(c, "/partners", name=name, phone=phone)
    assert response.status_code == 303, response.text
    match = re.fullmatch(r"/partners/(\d+)", response.headers["location"])
    assert match
    return int(match.group(1))


def foreign_partner(env: Env) -> int:
    """A partner of agency B, created through agency B's manager."""
    return make_partner(env, env.manager_b(), "AUTRE")


# --- who may -----------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_manager_and_accountant_can_list_create_and_set_a_fee(env: Env, who: str) -> None:
    c = env.manager_a() if who == "manager" else accountant(env)
    empty = c.get("/partners")
    assert empty.status_code == 200
    assert "Aucun partenaire pour le moment." in empty.text
    pid = make_partner(env, c, "BADI", "22 33 44 55")
    listing = c.get("/partners")
    assert "BADI" in listing.text and "+22222334455" in listing.text
    response = post(c, f"/partners/{pid}/fees", fee="1 500,50", valid_from="2026-01-01", note="x")
    assert response.status_code == 303 and response.headers["location"] == f"/partners/{pid}"
    assert [f.fee for f in fee_rows(env)] == [Decimal("1500.50")]
    detail = c.get(f"/partners/{pid}")
    assert detail.status_code == 200
    assert f"1{NNBSP}500,50" in detail.text


def test_the_acting_user_is_recorded_and_audited(env: Env) -> None:
    c = accountant(env)
    pid = make_partner(env, c)
    post(c, f"/partners/{pid}/fees", fee="500", valid_from="2026-01-01")
    post(c, f"/partners/{pid}", name="BADI Voyages", phone="")
    post(c, f"/partners/{pid}/deactivate")
    for kind in ("partner_created", "partner_fee_set", "partner_updated", "partner_deactivated"):
        (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, kind)
        assert event.details["partner_id"] == pid
    assert (
        fee_rows(env)[0].set_by_user_id
        == audit_events(env.db.owner_url, env.seeded.agency_a, "partner_fee_set")[0].actor_user_id
    )


STAFF_GETS = ["/partners", "/partners/{p}", "/partners/{p}/deactivate"]
STAFF_POSTS = [
    "/partners",
    "/partners/{p}",
    "/partners/{p}/fees",
    "/partners/{p}/deactivate",
    "/partners/{p}/reactivate",
    "/partners/aliases/{p}/approve",
    "/partners/aliases/{p}/reassign",
    "/partners/aliases/{p}/reject",
    "/partners/aliases/{p}/remove",
    "/partners/aliases/{p}/reactivate",
]


@pytest.mark.parametrize("path", STAFF_GETS)
def test_staff_get_403_and_an_audit_event(env: Env, path: str) -> None:
    pid = make_partner(env, env.manager_a())
    c = env.staff_a()
    response = c.get(path.format(p=pid))
    assert response.status_code == 403
    assert "BADI" not in response.text
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert event.actor_user_id == env.seeded.staff_a
    assert event.details["path"] == path.format(p=pid)


@pytest.mark.parametrize("path", STAFF_POSTS)
def test_staff_post_403_audit_and_nothing_changes(env: Env, path: str) -> None:
    pid = make_partner(env, env.manager_a())
    c = env.staff_a()
    response = c.post(
        path.format(p=pid),
        data={
            "name": "Pirate",
            "phone": "",
            "fee": "1",
            "valid_from": "2026-01-01",
            "csrf_token": csrf_for(c),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    rows = partner_rows(env)
    assert [(p.name, p.is_active) for p in rows] == [("BADI", True)]
    assert fee_rows(env) == []


def test_every_partner_route_uses_the_partner_admin_dependency() -> None:
    from fastapi.routing import APIRoute

    from app.web.routes import partners as partner_routes

    routes = [r for r in partner_routes.router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(STAFF_GETS) + len(STAFF_POSTS) == 13
    for route in routes:
        calls = [d.call for d in route.dependant.dependencies]
        assert partner_routes.require_partner_admin in calls, route.path
        assert partner_routes.require_partner_admin.__name__ == "require_can_manage_partners"


# --- the sidebar ---------------------------------------------------------------------------------


def test_the_sidebar_entry_shows_for_manager_and_accountant_not_for_staff(env: Env) -> None:
    for client in (env.manager_a(), accountant(env)):
        html = client.get("/").text
        assert 'href="/partners"' in html and "Partenaires" in html
    staff = env.staff_a().get("/").text
    assert 'href="/partners"' not in staff and "Partenaires" not in staff


def test_the_bottom_bar_reaches_partners_for_an_accountant_and_via_the_team_page_for_a_manager(
    env: Env,
) -> None:
    bar = accountant(env).get("/").text.split('<nav class="tabbar"')[1].split("</nav>")[0]
    assert 'href="/partners"' in bar and bar.count('class="tab"') == 5
    manager = env.manager_a()
    bar = manager.get("/").text.split('<nav class="tabbar"')[1].split("</nav>")[0]
    assert 'href="/partners"' not in bar and bar.count('class="tab"') == 5  # five fit 320px
    assert 'href="/partners"' in manager.get("/staff").text


def test_the_entry_is_current_on_the_partner_pages(env: Env) -> None:
    html = env.manager_a().get("/partners").text
    assert re.search(r'href="/partners" aria-current="page"', html)


# --- tenancy ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/partners/{p}"),
        ("post", "/partners/{p}"),
        ("post", "/partners/{p}/fees"),
        ("get", "/partners/{p}/deactivate"),
        ("post", "/partners/{p}/deactivate"),
        ("post", "/partners/{p}/reactivate"),
    ],
)
def test_another_agencys_partner_is_a_404(env: Env, method: str, path: str) -> None:
    other = foreign_partner(env)
    c = env.manager_a()
    token = csrf_for(c, "/partners")
    url = path.format(p=other)
    if method == "get":
        response = c.get(url)
    else:
        response = c.post(
            url,
            data={"name": "Vol", "fee": "1", "valid_from": "2026-01-01", "csrf_token": token},
            follow_redirects=False,
        )
    assert response.status_code == 404
    mine = partner_rows(env, env.seeded.agency_b)
    assert [(p.name, p.is_active) for p in mine] == [("AUTRE", True)]
    assert fee_rows(env, env.seeded.agency_b) == []


def test_the_list_shows_only_my_agencys_partners(env: Env) -> None:
    foreign_partner(env)
    c = env.manager_a()
    make_partner(env, c, "BADI")
    html = c.get("/partners").text
    assert "BADI" in html and "AUTRE" not in html


def test_the_agency_comes_from_the_session_not_the_form(env: Env) -> None:
    c = env.manager_a()
    response = c.post(
        "/partners",
        data={
            "name": "BADI",
            "agency_id": str(env.seeded.agency_b),
            "csrf_token": csrf_for(c, "/partners"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(partner_rows(env)) == 1
    assert partner_rows(env, env.seeded.agency_b) == []


@pytest.mark.parametrize("pid", ["0", "99999999", "9223372036854775808", "-1"])
def test_unknown_or_out_of_range_ids_are_404(env: Env, pid: str) -> None:
    assert env.manager_a().get(f"/partners/{pid}").status_code == 404


def test_a_non_numeric_id_is_refused(env: Env) -> None:
    assert env.manager_a().get("/partners/abc").status_code in (400, 404, 422)


# --- CSRF and caching -----------------------------------------------------------------------


@pytest.mark.parametrize("path", STAFF_POSTS)
def test_post_without_a_csrf_token_is_403_and_changes_nothing(env: Env, path: str) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    response = c.post(
        path.format(p=pid),
        data={"name": "X", "fee": "1", "valid_from": "2026-01-01"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert [(p.name, p.is_active) for p in partner_rows(env)] == [("BADI", True)]
    assert fee_rows(env) == []


def test_a_wrong_csrf_token_is_403(env: Env) -> None:
    c = env.manager_a()
    response = c.post("/partners", data={"name": "X", "csrf_token": "nope"})
    assert response.status_code == 403
    assert partner_rows(env) == []


def test_pages_are_no_store(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    assert c.get("/partners").headers["cache-control"] == "no-store"
    assert c.get(f"/partners/{pid}").headers["cache-control"] == "no-store"
    failed = post(c, "/partners", name="")
    assert failed.status_code == 400 and failed.headers["cache-control"] == "no-store"


# --- content -------------------------------------------------------------------------------------


def test_the_html_is_escaped(env: Env) -> None:
    c = env.manager_a()
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    pid = make_partner(env, c, evil)
    post(c, f"/partners/{pid}/fees", fee="1", valid_from="2026-01-01", note="<b>gras</b>")
    for html in (c.get("/partners").text, c.get(f"/partners/{pid}").text):
        assert "<script>alert" not in html
        assert "<img src=x" not in html and "<b>gras</b>" not in html
        assert "&lt;" in html


def test_the_error_form_echoes_the_input_escaped(env: Env) -> None:
    c = env.manager_a()
    response = post(c, "/partners", name="Bruge", phone='"><script>x</script>')
    assert response.status_code == 400
    assert "<script>x" not in response.text and "&lt;script&gt;x" in response.text


def test_the_pages_are_in_french_and_have_no_inline_style_or_script(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    listing, detail = c.get("/partners").text, c.get(f"/partners/{pid}").text
    for label in ("Partenaires", "Ajouter un partenaire", "Frais actuels", "Nom", "Téléphone"):
        assert label in listing, label
    for label in (
        "Historique des frais",
        "Nouveau tarif",
        "Frais par billet",
        "Applicable à partir du",
        "Désactiver ce partenaire",
        "Retour aux partenaires",
    ):
        assert label in detail, label
    for html in (listing, detail):
        assert not re.search(r"\sstyle\s*=", html, re.I)
        assert not re.findall(r"<script(?![^>]*\bsrc=)", html)
        assert not re.search(r"\son[a-z]+\s*=", html, re.I)
        assert html.count('name="csrf_token"') >= 1


def test_every_post_form_carries_a_csrf_field(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    html = c.get(f"/partners/{pid}").text
    forms = re.findall(r'<form[^>]*action="/partners[^>]*>.*?</form>', html, re.S)
    assert len(forms) == 2  # edit and new fee; deactivating is a link to a confirmation page
    assert all('name="csrf_token"' in f and 'method="post"' in f for f in forms)


def test_the_detail_page_shows_the_history_newest_first_and_the_current_fee(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    for fee, day in (("500", "2026-01-01"), ("300", "2026-07-16"), ("400", "2026-03-01")):
        assert post(c, f"/partners/{pid}/fees", fee=fee, valid_from=day).status_code == 303
    html = c.get(f"/partners/{pid}").text
    history = html.split('id="history-title"')[1]
    order = [history.index(x) for x in ("16 juillet 2026", "1 mars 2026", "1 janvier 2026")]
    assert order == sorted(order)
    # the clock of the tests is 18 Oct 2026: the fee of 16 July is the one in force
    summary = html.split('id="who-title"')[1].split("</section>")[0]
    assert "300" in summary and "16 juillet 2026" in summary


def test_a_future_fee_is_listed_but_not_current(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    post(c, f"/partners/{pid}/fees", fee="500", valid_from="2026-01-01")
    post(c, f"/partners/{pid}/fees", fee="900", valid_from="2027-01-01")
    html = c.get(f"/partners/{pid}").text
    summary = html.split('id="who-title"')[1].split("</section>")[0]
    assert "500" in summary and "900" not in summary
    assert "900" in html.split('id="history-title"')[1]


def test_the_list_shows_the_current_fee(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    post(c, f"/partners/{pid}/fees", fee="500", valid_from="2026-01-01")
    row = c.get("/partners").text.split("<tbody>")[1]
    assert "500" in row


# --- mistakes ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "obligatoire"),
        ("   ", "obligatoire"),
        (" Bruge ", "vente directe"),
        ("BRUGE", "vente directe"),
        ("x" * 121, "120"),
    ],
)
def test_create_mistakes_give_a_400_with_a_french_message(
    env: Env, name: str, message: str
) -> None:
    response = post(env.manager_a(), "/partners", name=name)
    assert response.status_code == 400
    assert message in response.text
    assert partner_rows(env) == []


def test_a_duplicate_name_is_refused(env: Env) -> None:
    c = env.manager_a()
    make_partner(env, c, "BADI")
    response = post(c, "/partners", name="  badi ")
    assert response.status_code == 400 and "existe déjà" in response.text
    assert len(partner_rows(env)) == 1


def test_a_bad_phone_is_refused_and_the_form_keeps_the_name(env: Env) -> None:
    response = post(env.manager_a(), "/partners", name="BADI", phone="abc")
    assert response.status_code == 400
    assert "téléphone" in response.text and 'value="BADI"' in response.text


@pytest.mark.parametrize(
    ("fee", "day", "message"),
    [
        ("-5", "2026-01-01", "négatif"),
        ("abc", "2026-01-01", "montant"),
        ("", "2026-01-01", "montant"),
        ("1,234", "2026-01-01", "milliers"),
        ("1000000000000", "2026-01-01", "trop grand"),
        ("500", "", "date"),
        ("500", "01/02/2026", "date"),
        ("500", "2026-02-30", "date"),
        ("500", "20260101", "date"),
    ],
)
def test_fee_mistakes_give_a_400_and_write_nothing(
    env: Env, fee: str, day: str, message: str
) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    response = post(c, f"/partners/{pid}/fees", fee=fee, valid_from=day)
    assert response.status_code == 400
    assert message in response.text
    assert fee_rows(env) == []
    assert audit_events(env.db.owner_url, env.seeded.agency_a, "partner_fee_set") == []


def test_an_over_long_note_is_refused(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    response = post(c, f"/partners/{pid}/fees", fee="1", valid_from="2026-01-01", note="n" * 501)
    assert response.status_code == 400 and "500" in response.text


def test_the_edit_form_renames_and_rejects_duplicates(env: Env) -> None:
    c = env.manager_a()
    one = make_partner(env, c, "BADI")
    make_partner(env, c, "SAHEL")
    assert post(c, f"/partners/{one}", name="Badi Voyages", phone="12 34 56 78").status_code == 303
    assert "Badi Voyages" in c.get(f"/partners/{one}").text
    clash = post(c, f"/partners/{one}", name="sahel", phone="")
    assert clash.status_code == 400 and "existe déjà" in clash.text
    names = sorted(p.name for p in partner_rows(env))
    assert names == ["Badi Voyages", "SAHEL"]


def test_deactivating_keeps_the_history_and_closes_the_forms(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    post(c, f"/partners/{pid}/fees", fee="500", valid_from="2026-01-01")
    response = post(c, f"/partners/{pid}/deactivate")
    assert response.status_code == 303 and response.headers["location"] == "/partners"
    assert len(fee_rows(env)) == 1
    detail = c.get(f"/partners/{pid}").text
    assert "Désactivé" in detail and "500" in detail
    assert "Désactiver ce partenaire" not in detail and "Enregistrer le tarif" not in detail
    again = post(c, f"/partners/{pid}/deactivate")
    assert again.status_code == 400 and "déjà" in again.text
    blocked = post(c, f"/partners/{pid}/fees", fee="1", valid_from="2026-02-01")
    assert blocked.status_code == 400 and "désactivé" in blocked.text
    assert len(fee_rows(env)) == 1


def test_the_page_fee_matches_fee_on(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c, "BADI")
    post(c, f"/partners/{pid}/fees", fee="500", valid_from="2026-01-01")
    post(c, f"/partners/{pid}/fees", fee="300", valid_from="2026-07-16")
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        a = partners.fee_on(s, env.seeded.agency_a, pid, date(2026, 7, 15))
        b = partners.fee_on(s, env.seeded.agency_a, pid, date(2026, 7, 16))
    assert a is not None and a.fee == Decimal("500.00")
    assert b is not None and b.fee == Decimal("300.00")


def test_deactivation_needs_the_confirmation_page_and_a_get_changes_nothing(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    detail = c.get(f"/partners/{pid}").text
    assert f'href="/partners/{pid}/deactivate"' in detail
    assert f'action="/partners/{pid}/deactivate"' not in detail  # no one-click form
    confirm = c.get(f"/partners/{pid}/deactivate")
    assert confirm.status_code == 200 and confirm.headers["cache-control"] == "no-store"
    assert "Oui, désactiver" in confirm.text and "Annuler" in confirm.text
    assert 'name="csrf_token"' in confirm.text
    assert not re.search(r"<script(?![^>]*\bsrc=)|\sstyle\s*=|\son[a-z]+\s*=", confirm.text)
    assert [p.is_active for p in partner_rows(env)] == [True]
    assert post(c, f"/partners/{pid}/deactivate").status_code == 303
    assert [p.is_active for p in partner_rows(env)] == [False]
    assert "déjà désactivé" in c.get(f"/partners/{pid}/deactivate").text


def test_reactivating_brings_the_forms_back_and_is_audited(env: Env) -> None:
    c = accountant(env)
    pid = make_partner(env, c)
    post(c, f"/partners/{pid}/deactivate")
    detail = c.get(f"/partners/{pid}").text
    assert "Réactiver ce partenaire" in detail and "Enregistrer le tarif" not in detail
    response = post(c, f"/partners/{pid}/reactivate")
    assert response.status_code == 303 and response.headers["location"] == f"/partners/{pid}"
    assert [p.is_active for p in partner_rows(env)] == [True]
    assert "Enregistrer le tarif" in c.get(f"/partners/{pid}").text
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "partner_reactivated")
    assert event.details["partner_id"] == pid
    again = post(c, f"/partners/{pid}/reactivate")
    assert again.status_code == 400 and "déjà actif" in again.text


def test_over_long_input_gets_the_french_form_error_not_a_422(env: Env) -> None:
    c = env.manager_a()
    assert "120" in post(c, "/partners", name="x" * 600).text
    pid = make_partner(env, c)
    response = post(c, f"/partners/{pid}/fees", fee="1" * 600, valid_from="2026-01-01")
    assert response.status_code == 400 and "montant" in response.text
    note = post(c, f"/partners/{pid}/fees", fee="1", valid_from="2026-01-01", note="n" * 5000)
    assert note.status_code == 400 and "500" in note.text
    phone = post(c, "/partners", name="Z", phone="1" * 600)
    assert phone.status_code == 400 and "téléphone" in phone.text


def test_a_thousands_separator_gets_a_helpful_message(env: Env) -> None:
    c = env.manager_a()
    pid = make_partner(env, c)
    response = post(c, f"/partners/{pid}/fees", fee="1,500", valid_from="2026-01-01")
    assert response.status_code == 400 and "sans séparateur de milliers" in response.text


def test_the_team_page_links_to_partners_in_a_card(env: Env) -> None:
    html = env.manager_a().get("/staff").text
    assert 'aria-labelledby="partners-link-title"' in html and "Partenaires et frais" in html
