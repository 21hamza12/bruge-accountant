"""Accessibility and right-to-left readiness of the pages, checked on the rendered HTML and on the
stylesheet (WCAG 2.2 AA audit of the design pass).

What a browser would compute cannot be asserted here, so each check pins the markup or the rule
that produces it: names and roles, text for screen readers, contrast ratios of the colour tokens,
focus rings that the card clipping would hide, mirrored chevrons.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.web.templating import TEMPLATES_DIR, render
from tests.page_support import DAY, Env
from tests.web_support import (
    PHONE_A,
    WebDb,
    csrf_for,
    login,
)

CSS = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "app.css").read_text(
    encoding="utf-8"
)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def squash(html: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def sidebar(html: str) -> str:
    return html.split('<div class="sidebar">')[1].split('<div class="content-col">')[0]


def tabbar(html: str) -> str:
    return html.split('<nav class="tabbar"')[1].split("</nav>")[0]


# --- contrast, computed from the stylesheet -----------------------------------------------------


def token(name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert match, name
    return match.group(1)


def luminance(colour: str) -> float:
    channels = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_the_contrast_helper_knows_the_extremes() -> None:
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#ffffff", "#ffffff") == pytest.approx(1.0)


@pytest.mark.parametrize("surface", ["surface", "bg", "subtle", "hover", "border-soft"])
@pytest.mark.parametrize("name", ["text-3", "text-4"])
def test_the_grey_text_tokens_reach_4_5_to_1_on_every_surface_they_sit_on(
    name: str, surface: str
) -> None:
    assert contrast(token(name), token(surface)) >= 4.5, (name, surface)


def test_the_darker_text_colour_is_used_on_the_accent_tint_of_a_targeted_booking() -> None:
    assert contrast(token("text-3"), token("accent-soft")) < 4.5  # why the override exists
    assert contrast(token("text-2"), token("accent-soft")) >= 4.5
    assert re.search(
        r"\.booking:target summary :is\(\.b-more, \.b-tickets, \.b-airlines\)"
        r"\s*\{\s*color: var\(--text-2\)",
        CSS,
    )


@pytest.mark.parametrize("background", ["surface", "subtle"])
def test_form_control_borders_reach_3_to_1(background: str) -> None:
    assert contrast(token("border-input"), token(background)) >= 3.0
    controls = re.search(r'input\[type="text"\],.*?select\s*\{([^}]*)\}', CSS, re.S)
    assert controls and "border: 1px solid var(--border-input)" in controls.group(1)
    search = re.search(r"\.search\s*\{([^}]*)\}", CSS)
    assert search and "border: 1px solid var(--border-input)" in search.group(1)


def test_the_placeholder_is_the_readable_grey_at_full_opacity() -> None:
    rule = re.search(r"::placeholder\s*\{([^}]*)\}", CSS)
    assert rule
    assert "color: var(--text-3)" in rule.group(1) and "opacity: 1" in rule.group(1)
    assert contrast(token("text-3"), token("surface")) >= 4.5
    assert contrast(token("text-3"), token("subtle")) >= 4.5  # the search box


def test_the_chart_bars_are_distinguishable_from_the_page() -> None:
    bar = re.search(r"\.bar\s*\{\s*fill:\s*(#[0-9a-fA-F]{6})", CSS)
    assert bar
    assert contrast(bar.group(1), token("surface")) >= 3.0
    assert contrast(token("accent"), token("surface")) >= 3.0  # the top bar


def test_the_empty_profit_figure_is_not_the_faint_decoration_grey() -> None:
    rule = re.search(r"\.kpi-value-empty\s*\{([^}]*)\}", CSS)
    assert rule and "color: var(--text-4)" in rule.group(1)


# --- stylesheet rules ---------------------------------------------------------------------------


def test_the_bottom_bar_never_hides_the_focused_element() -> None:
    assert re.search(r"html\s*\{[^}]*scroll-padding-block-end:\s*5rem", CSS)


def test_card_rows_draw_their_focus_ring_inside_the_clipping_card() -> None:
    assert re.search(r"\.card-flush\s*\{[^}]*overflow:\s*hidden", CSS)
    assert re.search(
        r"\.card-flush :is\(summary, \.more-link, \.my-row\):focus-visible"
        r"\s*\{\s*outline-offset:\s*-3px",
        CSS,
    )


def test_a_booking_row_has_a_chevron_that_turns_when_open_and_mirrors_in_rtl() -> None:
    assert re.search(r"\.booking summary::after\s*\{[^}]*content:\s*\"\"", CSS)
    assert re.search(r"\.booking summary::after\s*\{[^}]*rotate\(var\(--chevron-forward\)\)", CSS)
    assert re.search(
        r"\.booking\[open\] summary::after\s*\{[^}]*rotate\(var\(--chevron-down\)\)", CSS
    )
    # the three turns exist for both directions, and the right-to-left ones are not the same
    ltr = re.search(r":root\s*\{[^}]*\}", CSS, re.S)
    rtl = re.search(r'\[dir="rtl"\]\s*\{([^}]*)\}', CSS)
    assert ltr and rtl
    for name in ("forward", "back", "down"):
        left = re.search(rf"--chevron-{name}:\s*(-?\d+)deg", ltr.group(0))
        right = re.search(rf"--chevron-{name}:\s*(-?\d+)deg", rtl.group(1))
        assert left and right and left.group(1) != right.group(1), name
    # a column is reserved for it, on wide and narrow screens
    wide = re.search(r"\.booking summary\s*\{[^}]*grid-template-columns:([^;]*);", CSS, re.S)
    assert wide and wide.group(1).split()[-1] == "1rem"
    narrow = CSS.split("@media (max-width: 47.99rem)")[1]
    assert '"ref status chev"' in narrow and "grid-area: chev" in narrow


def test_the_chevrons_of_the_links_mirror_too() -> None:
    assert re.search(r"\.card-link-next::after\s*\{[^}]*rotate\(var\(--chevron-forward\)\)", CSS)
    assert re.search(r"\.card-link-back::before\s*\{[^}]*rotate\(var\(--chevron-back\)\)", CSS)


def test_five_bottom_tabs_have_room_on_a_320px_screen() -> None:
    """320px minus the bar's padding, shared by five tabs, is about 62px each: the labels wrap."""
    bar = re.search(r"\.tabbar\s*\{([^}]*)\}", CSS)
    assert bar and "padding-inline: 0.25rem" in bar.group(1)
    tab = re.search(r"\.tab\s*\{([^}]*)\}", CSS)
    assert tab and "min-inline-size: 0" in tab.group(1) and "text-align: center" in tab.group(1)
    assert re.search(r"\.tab-label\s*\{[^}]*overflow-wrap:\s*anywhere", CSS)


def test_no_template_has_a_literal_arrow_or_a_concatenated_phrase() -> None:
    for path in TEMPLATES_DIR.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        assert "→" not in source and "←" not in source, path
        for fragment in (
            '_("Afficher les")',
            '_("Voir les")',
            '_("autres réservations")',
            '_("Rapport du")',
            '_("importé")',
            '_("prix manquant(s)")',
        ):
            assert fragment not in source, (path, fragment)


# --- landmarks, names and one name per destination ----------------------------------------------


def test_the_sidebar_is_not_a_complementary_landmark(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    assert "<aside" not in html
    assert '<div class="sidebar">' in html


def test_the_home_is_called_tableau_de_bord_everywhere(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.manager_a() as m, env.staff_a() as s:
        pages = {"manager": m.get("/").text, "staff": s.get("/").text}
    for who, html in pages.items():
        assert "Tableau de bord" in squash(sidebar(html)), who
        assert "Tableau de bord" in squash(tabbar(html)), who
        assert "Accueil" not in html, who
        assert "<title>Tableau de bord</title>" in html, who


def test_uploads_are_called_envoyer_un_rapport_and_the_list_rapports(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    assert "Envoyer un rapport" in squash(tabbar(html))
    assert "Envoyer un rapport" in html.split('class="topbar-tools"')[1]
    side = squash(sidebar(html))
    assert "Rapports" in side and "Rapports Amadeus" not in html
    assert re.search(r">\s*Envoyer\s*<", tabbar(html)) is None  # not the old short name


def test_both_roles_reach_the_account_and_the_logout_from_the_bottom_bar(env: Env) -> None:
    env.load()
    with env.manager_a() as m, env.staff_a() as s:
        for client in (m, s):
            html = client.get("/").text
            assert 'href="/password/change"' in tabbar(html)
            assert "Compte" in squash(tabbar(html))
            account = client.get("/password/change").text
            assert 'action="/logout"' in account  # the page the tab leads to has the sign-out
            assert 'aria-current="page"' in tabbar(account)


def test_the_bottom_bar_has_five_tabs_for_a_manager(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        bar = tabbar(c.get("/").text)
    assert bar.count('class="tab"') == 5
    assert [
        squash(label) for label in re.findall(r'<span class="tab-label">(.*?)</span>', bar)
    ] == [
        "Tableau de bord",
        "Saisir une vente",
        "Envoyer un rapport",
        "Équipe",
        "Compte",
    ]


# --- numbers a screen reader can understand -----------------------------------------------------


def test_the_staff_home_pill_says_what_is_left_to_enter_not_a_bare_number(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        html = c.get("/").text
    card = html.split('aria-labelledby="todo-title"')[1].split("</section>")[0]
    pill = re.search(r'<span class="pill pill-amber">(.*?)</span>', card)
    assert pill and pill.group(1) == "14 à saisir"


def test_the_chart_is_a_named_group_and_each_bar_says_its_ticket_count(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get("/").text
    chart = html.split('<div class="chart"')[1].split("</section>")[0]
    assert 'role="region"' in chart.split(">")[0] and 'tabindex="0"' in chart.split(">")[0]
    assert 'role="group" aria-labelledby="chart-title"' in chart
    assert 'id="chart-title"' in html
    labels = re.findall(r'<svg class="chart-plot" role="img" aria-label="([^"]+)"', chart)
    assert len(labels) == 5
    assert any("Royal Air Maroc" in x and "7 billets" in x for x in labels)
    assert any("Qatar Airways" in x and "2 billets" in x for x in labels)
    assert all(re.search(r"\d+ billets?$", x) for x in labels)
    names = chart.split('<div class="chart-names"')[1].split(">")[0]
    assert 'aria-hidden="true"' in names  # the captions are not read a second time


def test_scrollable_wrappers_are_focusable_named_regions_and_tables_are_named(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        pages = {"uploads": c.get("/uploads").text}
        staff = c.get("/staff").text
    pages["staff"] = staff
    for name, html in pages.items():
        wraps = re.findall(r'<div class="table-wrap"([^>]*)>', html)
        if name == "uploads" and not wraps:
            continue  # no upload yet: nothing to scroll
        assert wraps, name
        for attrs in wraps:
            assert 'tabindex="0"' in attrs and 'role="region"' in attrs and "aria-label=" in attrs
        for table in re.findall(r'<table class="table"([^>]*)>', html):
            label = re.search(r'aria-labelledby="([^"]+)"', table)
            assert label, name
            assert f'id="{label.group(1)}"' in html, (name, label.group(1))


# --- form errors --------------------------------------------------------------------------------


def assert_error_is_linked(html: str, field_ids: list[str]) -> None:
    error = re.search(r'<p class="error-box" id="form-error"([^>]*)>', html)
    assert error, "the error message has no id"
    assert 'role="alert"' in error.group(1)
    assert "autofocus" in error.group(1) and 'tabindex="-1"' in error.group(1)
    assert html.count("autofocus") == 1  # one element takes the focus
    for field_id in field_ids:
        field = re.search(rf'<(?:input|select)[^>]*\bid="{field_id}"[^>]*>', html)
        assert field, field_id
        assert 'aria-describedby="form-error"' in field.group(0), field_id


def test_no_error_means_no_describedby_and_no_autofocus() -> None:
    html = render("auth/login.html", form_token="t")  # noqa: S106
    assert "form-error" not in html and "autofocus" not in html and "aria-describedby" not in html


def test_the_login_error_is_focused_and_linked_to_both_fields(env: Env) -> None:
    with env.factory() as c:
        response = login(c, PHONE_A, "wrong-password-1")
    assert response.status_code == 401
    assert_error_is_linked(response.text, ["phone", "password"])


def test_the_password_change_error_is_focused_and_linked_to_the_fields(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/password/change")
        response = c.post(
            "/password/change",
            data={
                "csrf_token": token,
                "current_password": "not-my-password-1",
                "new_password": "A-brand-new-pass-2026",
                "confirm_password": "A-brand-new-pass-2026",
            },
        )
    assert response.status_code in (400, 401, 403)
    assert_error_is_linked(response.text, ["current_password", "new_password", "confirm_password"])


def test_the_add_user_error_is_focused_and_linked_to_the_fields(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/staff")
        response = c.post(
            "/staff",
            data={"csrf_token": token, "display_name": "X", "phone": "123", "role": "staff"},
        )
    assert response.status_code == 400
    assert_error_is_linked(response.text, ["display_name", "phone", "role"])


def test_the_sign_error_on_a_staff_page_is_focused_and_linked(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, f"/staff/{env.seeded.staff_a}")
        response = c.post(
            f"/staff/{env.seeded.staff_a}/signs", data={"csrf_token": token, "sign": "x"}
        )
    assert response.status_code == 400
    assert_error_is_linked(response.text, ["sign", "role"])


def test_the_upload_error_is_focused_and_linked_to_the_file_field(env: Env) -> None:
    with env.manager_a() as c:
        token = csrf_for(c, "/uploads/new")
        response = c.post(
            "/uploads",
            data={"csrf_token": token},
            files={"file": ("notes.xlsx", b"this is not a workbook", "application/octet-stream")},
        )
    assert response.status_code == 400
    assert_error_is_linked(response.text, ["file"])


# --- dates and wording --------------------------------------------------------------------------


def test_visible_dates_are_french_and_iso_only_in_attributes(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        listing = c.get("/uploads").text
        match = re.search(r'href="/uploads/(\d+)"', listing)
        assert match
        upload_id = match.group(1)
        detail = c.get(f"/uploads/{upload_id}").text
    for html in (listing, detail):
        text = squash(re.sub(r"<(?:input|time|a)\b[^>]*>", "<x>", html))
        assert not re.search(r"\b20\d\d-\d\d-\d\d\b", text), text[:200]
    assert "1 octobre 2026" in squash(listing) and "1 octobre 2026" in squash(detail)
    assert re.search(r'<time datetime="2026-10-01">1 octobre 2026</time>', listing)
    assert re.search(
        r'<time datetime="20\d\d-\d\d-\d\dT[\d:.+-]+">\d+ \w+ 20\d\d, \d\d:\d\d</time>', listing
    )


def test_the_date_field_has_a_visible_french_reading_next_to_it(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        home = c.get(f"/?date={DAY}").text
    field = re.search(r'<input id="date"[^>]*>', home)
    assert field and 'aria-describedby="date-readout"' in field.group(0)
    assert '<span class="date-readout" id="date-readout">1 octobre 2026</span>' in home


# --- single translatable strings ----------------------------------------------------------------


def test_phrases_with_numbers_are_whole_strings_with_placeholders(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.manager_a() as m, env.staff_a() as s:
        home = squash(m.get("/").text)
        staff = squash(s.get("/").text)
    assert "Rapport du 1 octobre 2026 importé · 48 billets" in home
    assert "Bonjour, Staff A" in staff
