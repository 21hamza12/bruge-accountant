"""The two home pages (manager dashboard, staff home) and the application shell around them.

Numbers come from the synthetic fixture: 48 tickets, 1 118 857 in total; two staff signs,
0004SV (34 tickets, 746 423) and 0009ER (14 tickets, 372 434).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from tests.builders import build_workbook, ticket_row
from tests.page_support import DAY, Env, fr
from tests.web_support import WebDb


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def squash(html: str) -> str:
    """Tags removed and white space collapsed, to assert on what a reader sees."""
    return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def kpi(html: str, label: str) -> str:
    """The text of the KPI tile with this label."""
    for block in re.findall(r'<div class="card kpi">(.*?)</div>\s*</div>', html, re.S):
        text = squash(block)
        if text.startswith(label):
            return text
    raise AssertionError(f"no KPI tile {label!r}")


# --- the shell ----------------------------------------------------------------------------------


def test_the_shell_has_a_sidebar_a_top_bar_and_a_bottom_bar(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get("/")
    html = response.text
    assert response.status_code == 200
    assert '<html lang="fr" dir="ltr">' in html
    assert '<div class="sidebar">' in html and "<aside" not in html
    assert '<header class="topbar">' in html
    assert '<nav class="tabbar"' in html
    assert 'href="#main"' in html  # skip link
    assert '<h1 class="page-title">Tableau de bord</h1>' in html
    assert "Agence A" in html


def test_the_manager_navigation(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    side = html.split('<div class="sidebar">')[1].split('<div class="content-col">')[0]
    for href in ("/", "/sales/new", "/sales", "/uploads", "/staff", "/password/change"):
        assert f'href="{href}"' in side, href
    assert 'href="/questions"' not in side
    assert 'aria-current="page"' in side
    tabbar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    for href in ("/", "/sales/new", "/uploads/new", "/staff", "/password/change"):
        assert f'href="{href}"' in tabbar, href
    assert 'href="/questions"' not in tabbar
    assert "Équipe" in tabbar and "Compte" in tabbar and "Profil" not in tabbar
    assert tabbar.count('class="tab"') == 5  # five fit a 320px screen (see the stylesheet test)


def test_the_staff_navigation_has_no_team_pages(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        html = c.get("/").text
    assert 'href="/staff"' not in html
    tabbar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    # staff get the two sales tabs
    for href in ("/", "/sales/new", "/sales", "/uploads/new", "/password/change"):
        assert f'href="{href}"' in tabbar, href
    assert 'href="/questions"' not in tabbar
    assert "Compte" in tabbar and "Profil" not in tabbar
    assert tabbar.count('class="tab"') == 5  # five fit a 320px screen


def test_the_sidebar_logout_is_a_csrf_protected_post(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    form = re.search(r'<form[^>]*action="/logout"[^>]*>(.*?)</form>', html, re.S)
    assert form and 'method="post"' in form.group(0)
    assert 'name="csrf_token"' in form.group(1)


def test_the_primary_action_links_to_the_upload_form(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    assert re.search(r'<a class="btn btn-primary upload-cta" href="/uploads/new"><svg', html, re.S)
    assert "Envoyer un rapport" in html


def test_there_is_no_dead_notification_button_or_period_toggle(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    assert "Notifications" not in html
    assert "Semaine" not in html and "Mois" not in html


# --- the manager dashboard ----------------------------------------------------------------------


def test_the_dashboard_status_line_and_kpis(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get("/")
    html = response.text
    assert "Rapport du 1 octobre 2026 importé · 48 billets" in squash(html)
    assert response.headers["cache-control"] == "no-store"
    assert kpi(html, "Billets émis") == "Billets émis 48"
    assert kpi(html, "Coût total") == f"Coût total {fr(1118857)} MRU"
    assert "Prix confirmés" not in html and "Bénéfice" not in html


def test_the_cost_chart_is_svg_with_attribute_sized_bars(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    chart = html.split('<section class="card" aria-labelledby="chart-title">')[1].split(
        "</section>"
    )[0]
    assert squash(chart).startswith("Coût par compagnie")
    assert f"{fr(1118857)} MRU" in squash(chart) and "5 compagnies" in squash(chart)
    bars = re.findall(r"<rect [^>]*>", chart)
    assert len(bars) == 5
    for bar in bars:
        assert re.search(r'\sheight="[\d.]+%"', bar) and re.search(r'\sy="[\d.]+%"', bar)
        assert re.search(r'\swidth="[\d.]+%"', bar)
        assert "style=" not in bar
    heights = [float(re.search(r'height="([\d.]+)%"', b).group(1)) for b in bars]  # type: ignore[union-attr]
    assert heights == sorted(heights, reverse=True)
    assert heights[0] > heights[-1] > 0
    assert bars[0].count("bar-top") == 1 and all("bar-top" not in b for b in bars[1:])
    text = squash(chart)
    for name, count, cost in [
        ("Mauritania Airlines", 19, 336190),
        ("Royal Air Maroc", 7, 266361),
        ("Turkish Airlines", 6, 238252),
        ("Tunisair", 14, 222125),
        ("Qatar Airways", 2, 55929),
    ]:
        assert name in text, name
        assert fr(cost) in text, cost
        assert f"{count} billets" in text, count
    assert 'role="img"' in chart and "aria-label" in chart


def test_totals_per_agent_with_the_unassigned_flag_and_owner_names(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.manager_a() as c:
        html = c.get("/").text
    card = html.split('aria-labelledby="agents-title">')[1].split("</section>")[0]
    rows = card.split('<div class="agent-row">')[1:]
    assert len(rows) == 2
    sv, er = squash(rows[0]), squash(rows[1])
    assert "Non attribué" in sv and "0004SV" in sv
    assert f"{fr(746423)} MRU · 34 billets" in sv
    assert "Staff A" in er and "0009ER" in er
    assert f"{fr(372434)} MRU · 14 billets" in er
    assert "<progress" not in card


def test_the_passenger_types(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    block = html.split("Type de passagers")[1].split("</section>")[0]
    assert re.search(r"Adultes\s*</span>\s*<span[^>]*>45<", block)
    assert re.search(r"Enfants\s*</span>\s*<span[^>]*>1<", block)
    assert re.search(r"Bébés\s*</span>\s*<span[^>]*>2<", block)


def test_the_dashboard_follows_the_date_parameter(env: Env) -> None:
    env.load()
    env.load(data=build_workbook([ticket_row(doc="3940000999", rloc="LATE00")], range_raw="05OCT"))
    with env.manager_a() as c:
        latest = c.get("/").text
        older = c.get(f"/?date={DAY}").text
    assert "Rapport du 5 octobre 2026" in squash(latest)
    assert kpi(latest, "Billets émis").startswith("Billets émis 1")
    assert "Rapport du 1 octobre 2026" in squash(older)
    assert kpi(older, "Billets émis").startswith("Billets émis 48")


def test_a_date_without_tickets_says_so_and_shows_no_figures(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get("/?date=2026-09-01")
    assert response.status_code == 200
    assert "Aucun billet pour cette date" in response.text
    assert 'class="card kpi"' not in response.text


@pytest.mark.parametrize("value", ["nope", "2026-13-01", "20261001", "x" * 300, "'"])
def test_an_invalid_date_is_the_400_page(env: Env, value: str) -> None:
    with env.manager_a() as c:
        response = c.get("/", params={"date": value})
    assert response.status_code == 400
    assert "Référence" in response.text


def test_a_manager_without_any_report_gets_a_call_to_action(env: Env) -> None:
    with env.manager_a() as c:
        response = c.get("/")
    assert response.status_code == 200
    assert "Aucun rapport" in response.text
    assert re.search(r'<a class="btn btn-primary[^"]*" href="/uploads/new"', response.text)
    assert 'class="card kpi"' not in response.text


def test_the_dashboard_never_shows_another_agency(env: Env) -> None:
    env.load()
    with env.manager_b() as c:
        html = c.get("/").text
        forged = c.get(f"/?date={DAY}&agency_id={env.seeded.agency_a}").text
    for text in (html, forged):
        assert "0004SV" not in text and "QA00" not in text
        assert "Agence A" not in text


def test_hostile_passenger_names_are_escaped_on_the_dashboard(env: Env) -> None:
    script = "<script>alert(1)</script>"
    env.load(data=build_workbook([ticket_row(doc="3940000001", name=f"{script}/X", rloc="AAAAAA")]))
    with env.manager_a() as c:
        text = c.get("/").text
    assert script not in text
    assert not re.findall(r"<script(?![^>]*src=)", text)


# --- the staff home -----------------------------------------------------------------------------


def test_the_staff_home_shows_only_their_own_day(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        response = c.get("/")
    html = response.text
    text = squash(html)
    assert '<h1 class="page-title">Bonjour, Staff A</h1>' in html
    assert "Ma journée" in text and "14 à saisir" in text
    assert "0 ventes saisies · 14 billets dans le rapport" in text
    assert re.search(r'<a class="btn btn-primary btn-lg btn-block" href="/sales/new">', html)
    assert 'href="/sales?date=2026-10-01"' in html and 'href="/days/2026-10-01"' in html
    assert re.search(r"Coût de mes billets\s*</span>\s*<span[^>]*>" + fr(372434), html)
    assert len(re.findall(r'<li class="my-row">', html)) == 14
    assert "0004SV" not in html and fr(746423) not in html
    assert 'class="card kpi"' not in html and 'class="chart-plot"' not in html
    for gone in ("À tarifer", "Commencer", "Prix confirmés", "<progress", "/questions"):
        assert gone not in html, gone


def test_the_staff_home_lists_the_report_tickets_without_a_sale(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        html = c.get("/").text
    first = html.split('<li class="my-row">')[1][:600]
    text = squash(first)
    assert re.search(r"\d{10}", text)
    assert re.search(r"(Royal Air Maroc|Turkish Airlines|Tunisair|Qatar Airways|Cie \d+)", text)
    assert re.search(r"\d", text)


def test_a_staff_member_with_two_signs_sees_both(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    env.link(env.seeded.staff_a, "0004SV")
    with env.staff_a() as c:
        text = squash(c.get("/").text)
    assert "48 billets dans le rapport" in text


def test_staff_without_a_linked_sign_get_a_friendly_empty_state(env: Env) -> None:
    env.load()
    with env.staff_a() as c:
        response = c.get("/")
    assert response.status_code == 200
    assert "Aucun code agent ne vous est attribué" in response.text
    assert "0004SV" not in response.text and "0009ER" not in response.text
    assert fr(1118857) not in response.text


def test_staff_with_signs_but_no_report_get_the_upload_call_to_action(env: Env) -> None:
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        response = c.get("/")
    assert "Aucun rapport" in response.text
    assert 'href="/uploads/new"' in response.text


def test_the_staff_home_ignores_a_sign_linked_to_someone_else(env: Env) -> None:
    from tests.web_support import add_user

    env.load()
    other = add_user(env.db.owner_url, env.seeded.agency_a, "+22212345620", display_name="Other")
    env.link(other, "0004SV")
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        text = c.get("/").text
    assert "0004SV" not in text and "Other" not in text


def test_the_staff_home_follows_the_date_parameter_within_their_signs(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        assert "Aucun billet pour cette date" in c.get("/?date=2026-09-01").text
        assert c.get("/?date=garbage").status_code == 400


def test_the_staff_home_escapes_names(env: Env) -> None:
    img = '"><img src=x onerror=alert(1)>'
    env.load(
        data=build_workbook([ticket_row(doc="3940000002", name=f"{img}/Y", rloc="BBBBBB")]),
    )
    env.link(env.seeded.staff_a, "0004SV")
    with env.staff_a() as c:
        text = c.get("/").text
    assert "<img" not in text


# --- access gates stay as they were -------------------------------------------------------------


def test_anonymous_visitors_are_still_sent_to_the_login_page(env: Env) -> None:
    with env.factory() as c:
        response = c.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_the_forced_password_change_gate_still_applies_to_the_home_page(env: Env) -> None:
    from tests.web_support import PASSWORD_S, add_user, login

    phone = "+22212345630"
    add_user(env.db.owner_url, env.seeded.agency_a, phone, must_change=True)
    c: TestClient = env.factory()
    assert login(c, phone, PASSWORD_S).status_code == 303
    response = c.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/password/change"


def test_no_inline_styles_or_style_blocks_in_the_rendered_homes(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.manager_a() as m, env.staff_a() as s:
        pages = [m.get("/").text, s.get("/").text]
    for html in pages:
        assert not re.search(r"\sstyle=", html, re.I)
        assert not re.search(r"<style", html, re.I)
