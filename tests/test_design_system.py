"""The visual system as a contract: the CSP is untouched, nothing is styled inline, the stylesheet
is mirror-safe and responsive without JavaScript, and every page uses the shell or the bare card."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.web.security import CSP
from app.web.templating import TEMPLATES_DIR, render
from tests.page_support import Env
from tests.web_support import PASSWORD_S, WebDb, add_user, login

WEB_DIR = Path(__file__).resolve().parent.parent / "app" / "web"
STATIC = WEB_DIR / "static"
CSS = (STATIC / "app.css").read_text(encoding="utf-8")

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def no_inline_style(html: str) -> None:
    assert not re.search(r"\sstyle\s*=", html, re.I)
    assert not re.search(r"<style", html, re.I)
    assert not re.findall(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


# --- the CSP and the headers ---------------------------------------------------------------------


def test_the_csp_is_exactly_the_strict_one() -> None:
    assert CSP == EXPECTED_CSP


def test_pages_keep_the_security_headers(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        for path in ("/", "/questions", "/uploads", "/uploads/new", "/staff", "/password/change"):
            response = c.get(path)
            assert response.status_code == 200, path
            assert response.headers["content-security-policy"] == EXPECTED_CSP, path
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["cache-control"] == "no-store", path
            no_inline_style(response.text)


FONT = "/static/fonts/geist-latin-400-normal.woff2"
IMMUTABLE = "public, max-age=31536000, immutable"


def test_the_changeable_assets_are_cached_by_validation_only(client: TestClient) -> None:
    """A deploy that changes one of these must reach the user at once: ``no-cache`` plus an ETag."""
    for path in (
        "/static/app.css",
        "/static/htmx.min.js",
        "/static/register-sw.js",
        "/static/manifest.webmanifest",
        "/sw.js",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path
        assert response.headers["content-security-policy"] == EXPECTED_CSP, path


def test_a_font_is_cached_for_a_year_and_revalidates_with_its_etag(client: TestClient) -> None:
    """A font never changes in place (a new font gets a new file name, which the hash test in
    test_pwa_assets pins), so the browser may keep it for a year without asking."""
    first = client.get(FONT)
    assert first.status_code == 200
    assert first.headers["content-type"] == "font/woff2"
    assert first.headers["cache-control"] == IMMUTABLE
    assert first.headers["content-security-policy"] == EXPECTED_CSP
    assert first.headers["x-content-type-options"] == "nosniff"
    etag = first.headers["etag"]
    assert etag and first.content[:4] == b"wOF2"
    again = client.get(FONT, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == IMMUTABLE
    other = client.get(FONT, headers={"If-None-Match": '"something-else"'})
    assert other.status_code == 200


def test_every_shipped_woff2_is_immutable_and_only_fonts_are(client: TestClient) -> None:
    fonts = sorted((STATIC / "fonts").glob("*.woff2"))
    assert len(fonts) == 5
    for font in fonts:
        assert client.get(f"/static/fonts/{font.name}").headers["cache-control"] == IMMUTABLE
    licence = client.get("/static/fonts/OFL-geist.txt")  # text that may be edited in place
    assert licence.status_code == 200 and licence.headers["cache-control"] == "no-cache"
    assert client.get("/static/icon-192.png").headers["cache-control"] == "no-cache"


def test_vendored_md_says_fonts_are_immutable_and_how_to_change_one() -> None:
    text = (WEB_DIR / "VENDORED.md").read_text(encoding="utf-8")
    assert "immutable" in text and "new file name" in text


# --- templates and rendered pages ---------------------------------------------------------------


def test_no_template_styles_anything_inline() -> None:
    for path in TEMPLATES_DIR.rglob("*.html"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"\sstyle\s*=", src, re.I), path
        assert not re.search(r"<style", src, re.I), path
        assert "fonts.googleapis" not in src and "fonts.gstatic" not in src, path
        assert not re.search(r"https?://", src.replace("http://www.w3.org", "")), path


@pytest.mark.parametrize(
    "template",
    [
        "auth/login.html",
        "offline.html",
        "errors/400.html",
        "errors/401.html",
        "errors/403.html",
        "errors/404.html",
        "errors/405.html",
        "errors/413.html",
        "errors/429.html",
        "errors/500.html",
        "errors/503.html",
    ],
)
def test_signed_out_pages_are_one_centered_card_with_the_mark(template: str) -> None:
    html = render(template, ref="ABCD1234", status=500, csrf_token=None, form_token="t")  # noqa: S106
    no_inline_style(html)
    assert '<main class="bare" id="main">' in html
    assert 'class="brand-mark"' in html
    assert html.count("<h1") == 1
    assert "sidebar" not in html and 'class="tabbar"' not in html
    assert '<link rel="stylesheet" href="/static/app.css">' in html


def test_error_pages_still_show_the_reference_and_a_way_out() -> None:
    html = render("errors/404.html", ref="ABCD1234")
    assert "Référence : <code>ABCD1234</code>" in html
    assert 'href="/"' in html


def test_the_offline_page_is_restyled_and_french() -> None:
    html = render("offline.html")
    assert '<h1 class="bare-title">Vous êtes hors ligne</h1>' in html
    assert re.search(r"Cette page n(&#39;|')est pas disponible sans connexion", html)


def test_the_document_language_and_direction_follow_the_language() -> None:
    assert '<html lang="fr" dir="ltr">' in render("offline.html")
    assert '<html lang="ar" dir="rtl">' in render("offline.html", lang="ar")
    assert '<html lang="ar" dir="rtl">' in render("auth/login.html", lang="ar-MR", form_token="x")  # noqa: S106


def test_the_login_page_form_is_unchanged_in_behaviour() -> None:
    html = render(
        "auth/login.html",
        form_token="tok",  # noqa: S106
        error="Identifiants incorrects.",
    )
    assert 'action="/login"' in html and 'method="post"' in html
    assert 'name="csrf_token" value="tok"' in html
    assert 'name="phone"' in html and 'name="password"' in html
    assert 'role="alert"' in html and "Identifiants incorrects." in html


# --- signed-in pages ----------------------------------------------------------------------------


def test_upload_pages_use_the_shell_and_the_cards(env: Env) -> None:
    env.load()
    with env.staff_a() as c:
        form = c.get("/uploads/new").text
        listing = c.get("/uploads").text
    assert (
        '<div class="sidebar">' in form and '<h1 class="page-title">Envoyer un rapport</h1>' in form
    )
    assert 'enctype="multipart/form-data"' in form and 'type="file"' in form
    assert 'name="csrf_token"' in form
    assert '<h1 class="page-title">Rapports récents</h1>' in listing
    assert 'class="table-wrap"' in listing or "Aucun rapport envoyé" in listing
    for html in (form, listing):
        no_inline_style(html)


def test_staff_pages_use_the_shell(env: Env) -> None:
    with env.manager_a() as c:
        listing = c.get("/staff").text
        detail = c.get(f"/staff/{env.seeded.staff_a}").text
    assert '<h1 class="page-title">Équipe</h1>' in listing
    assert 'class="table-wrap"' in listing and 'class="pill pill-green"' in listing
    assert (
        'aria-current="page"'
        in listing.split('<div class="sidebar">')[1].split('<div class="content-col">')[0]
    )
    assert "Codes agent Amadeus" in detail and 'class="btn btn-danger"' in detail
    for html in (listing, detail):
        no_inline_style(html)
        assert html.count("<h1") == 1


def test_the_temporary_password_page_keeps_its_markup_and_is_not_cached(env: Env) -> None:
    from tests.web_support import csrf_for

    with env.manager_a() as c:
        token = csrf_for(c, "/staff")
        response = c.post(
            "/staff",
            data={
                "csrf_token": token,
                "display_name": "New Person",
                "phone": "+22212349999",
                "role": "staff",
            },
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert re.search(r'<p class="secret"><code>[A-Za-z0-9]+</code></p>', response.text)
    no_inline_style(response.text)


def test_the_profile_page_shows_the_account_and_the_password_form(env: Env) -> None:
    with env.staff_a() as c:
        response = c.get("/password/change")
    html = response.text
    assert response.status_code == 200
    assert '<div class="sidebar">' in html
    assert "Staff A" in html and "+22212345603" in html
    assert 'action="/password/change"' in html and 'action="/logout"' in html
    assert 'aria-current="page"' in html.split('<nav class="tabbar"')[1]


def test_a_forced_password_change_is_a_bare_card_without_navigation(env: Env) -> None:
    phone = "+22212345631"
    add_user(env.db.owner_url, env.seeded.agency_a, phone, must_change=True)
    c = env.factory()
    assert login(c, phone, PASSWORD_S).status_code == 303
    html = c.get("/password/change").text
    assert '<main class="bare" id="main">' in html
    assert "sidebar" not in html and 'class="tabbar"' not in html
    assert "Vous devez choisir un nouveau mot de passe" in html
    assert 'action="/logout"' in html
    no_inline_style(html)


def test_error_pages_are_bare_even_for_signed_in_users(env: Env) -> None:
    with env.staff_a() as c:
        missing = c.get("/uploads/999999")
        denied = c.get("/staff")
    assert missing.status_code == 404 and denied.status_code == 403
    for response in (missing, denied):
        assert '<main class="bare" id="main">' in response.text
        assert "Référence" in response.text


# --- the stylesheet -----------------------------------------------------------------------------


def test_the_stylesheet_defines_the_design_tokens() -> None:
    for token, value in {
        "--bg": "#f6f6f8",
        "--surface": "#ffffff",
        "--border": "#ececef",
        "--text": "#17171c",
        "--text-2": "#4a4a55",
        "--text-3": "#6b6b76",
        "--accent": "#5b4ce0",
        "--accent-soft": "#e4e0fb",
        "--amber": "#a15c07",
        "--amber-soft": "#fdf1de",
        "--green": "#1e7a4c",
        "--radius": "14px",
        "--tap": "2.75rem",
    }.items():
        assert re.search(rf"{re.escape(token)}:\s*{re.escape(value)}", CSS), token
    assert '"Geist"' in CSS and '"Geist Mono"' in CSS
    assert "font-variant-numeric: tabular-nums" in CSS


def test_the_stylesheet_uses_logical_properties_only() -> None:
    rules = re.sub(r"@media[^{]*", "", CSS)  # media conditions may say max-width
    physical = re.findall(
        r"(?:margin|padding|border)-(?:left|right|top|bottom)"
        r"|(?<![\w-])(?:left|right|top|bottom)\s*:"
        r"|text-align:\s*(?:left|right)"
        r"|float:\s*(?:left|right)"
        r"|(?<![\w-])(?:min-|max-)?(?:width|height)\s*:",
        rules,
    )
    assert physical == []


def test_the_layout_is_responsive_without_javascript() -> None:
    assert re.search(r"@media \(max-width: 767\.98px\)", CSS)
    mobile = CSS.split("@media (max-width: 767.98px)")[1]
    assert re.search(r"\.sidebar\s*\{\s*display:\s*none", mobile)
    assert re.search(r"\.tabbar\s*\{\s*display:\s*flex", mobile)
    assert re.search(r"\.tabbar\s*\{\s*display:\s*none", CSS.split("@media")[0])
    assert re.search(r"\.table-wrap\s*\{[^}]*overflow-x:\s*auto", CSS)
    assert re.search(r"\.kpis\s*\{[^}]*auto-fit", CSS)
    assert "<script" not in render("offline.html").replace("<script src=", "")


def test_touch_targets_are_at_least_44px() -> None:
    assert "--tap: 2.75rem" in CSS  # 44px
    for selector in (".btn", ".nav-link", ".agent-link", ".card-link", ".tab", ".search"):
        block = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS)
        assert block, selector
        assert "var(--tap)" in block.group(1) or "3rem" in block.group(1), selector


def test_the_stylesheet_has_no_remote_resources_and_no_important() -> None:
    assert not re.search(r"https?://|@import|!important", CSS)
    assert 'url("/static/fonts/' in CSS


def test_the_home_page_is_reachable_from_every_nav_link(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        side = (
            c.get("/").text.split('<div class="sidebar">')[1].split('<div class="content-col">')[0]
        )
        hrefs = set(re.findall(r'href="(/[^"#?]*)', side))
        for href in sorted(hrefs):
            assert c.get(href).status_code == 200, href
    assert {"/", "/questions", "/uploads", "/staff", "/password/change"} <= hrefs


def test_the_manifest_and_the_meta_tag_share_the_accent_colour(client: TestClient) -> None:
    manifest = client.get("/static/manifest.webmanifest").json()
    meta = re.search(r'<meta name="theme-color" content="([^"]+)">', render("offline.html"))
    assert meta
    assert manifest["theme_color"] == meta.group(1) == "#5b4ce0"
    assert manifest["background_color"] == "#f6f6f8"
