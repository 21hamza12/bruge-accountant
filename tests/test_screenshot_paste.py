"""Pasting or dropping a screenshot into the photo box of the sale form, and the way to that form
for the manager and the accountant. The browser behaviour of app/web/static/screenshot-paste.js is
driven under node with a fake page (tests/paste_harness.js); the rest is the rendered HTML."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.page_support import Env
from tests.test_pwa_assets import require_node
from tests.test_sale_pages import SIGN, accountant, text_of
from tests.test_sale_screenshot import with_ai
from tests.web_support import WebDb

SCRIPT = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "screenshot-paste.js"
TAG = '<script src="/static/screenshot-paste.js" defer></script>'


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    return e


# --- the page ------------------------------------------------------------------------------------


def test_the_photo_box_takes_a_paste_or_a_drop(env: Env) -> None:
    with_ai(env)
    html = env.staff_a().get("/sales/new").text
    assert TAG in html
    assert 'id="shot-zone"' in html and 'id="shot_files"' in html
    status = re.search(r'<p[^>]*id="shot-pasted"[^>]*>', html)
    assert status is not None
    assert 'role="status"' in status.group(0) and 'aria-live="polite"' in status.group(0)
    for key in ("data-one", "data-many", "data-full"):
        assert key in status.group(0)
    assert "Ctrl+V" in text_of(html)
    assert not re.search(r"<style|\sstyle=|<script(?![^>]*\ssrc=)|\son[a-z]+=", html)


def test_no_photo_box_no_script(env: Env) -> None:
    html = env.staff_a().get("/sales/new").text  # no AI client: no photo box
    assert TAG not in html and 'id="shot-zone"' not in html


def test_the_script_is_served_as_javascript(env: Env) -> None:
    r = env.staff_a().get("/static/screenshot-paste.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


def test_the_manager_finds_the_sale_form_in_the_menu_and_on_the_dashboard(env: Env) -> None:
    html = env.manager_a().get("/").text
    assert re.search(r'<a class="nav-link" href="/sales/new"[^>]*>', html)
    assert "Saisir une vente" in text_of(html)
    assert html.count('href="/sales/new"') >= 2  # the menu and the dashboard button


def test_the_accountant_finds_it_too(env: Env) -> None:
    html = accountant(env).get("/").text
    assert re.search(r'<a class="nav-link" href="/sales/new"[^>]*>', html)


def test_the_menu_marks_the_sale_form_as_the_current_page_for_the_manager(env: Env) -> None:
    html = env.manager_a().get("/sales/new").text
    assert re.search(r'<a class="nav-link" href="/sales/new" aria-current="page">', html)
    assert not re.search(r'<a class="nav-link" href="/sales" aria-current="page">', html)


# --- the browser behaviour -----------------------------------------------------------------------


def test_paste_and_drop_behaviour() -> None:
    node = require_node()
    harness = str(Path(__file__).with_name("paste_harness.js"))
    proc = subprocess.run(  # noqa: S603
        [node, harness, str(SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    assert out["pasteOne"] == {
        "prevented": True,
        "files": ["image.png"],
        "status": "1 image prête.",
    }
    assert out["pasteSecond"] == {"files": 2, "status": "2 images prêtes."}
    # Text goes to the Amadeus box as usual; a PDF is not a photo.
    assert out["pasteText"] == {"prevented": False, "files": 0, "status": ""}
    assert out["pastePdf"] == {"prevented": False, "files": 0}
    assert out["pasteFive"]["files"] == ["p0.png", "p1.png", "p2.png", "p3.png"]
    assert out["pasteFive"]["status"].startswith("4 images au plus")
    assert out["dragoverPrevented"] is True and out["dragoverClass"] is True
    assert out["drop"] == {"prevented": True, "files": ["d1.png"], "cls": False}
    assert out["afterSwap"] == 1
    assert out["noBox"] == {"prevented": False, "dropPrevented": False}


# --- the chat ------------------------------------------------------------------------------------


def test_the_chat_has_the_photo_box_and_the_script_once(env: Env) -> None:
    with_ai(env)
    html = env.staff_a().get("/chat").text
    assert 'action="/sales/screenshot"' in html and 'enctype="multipart/form-data"' in html
    assert 'id="shot-zone"' in html and 'id="shot_files"' in html and 'id="shot-pasted"' in html
    assert html.count(TAG) == 1
    assert "Envoyer une capture de l'écran Amadeus" in text_of(html)


def test_the_script_is_outside_the_swapped_chat_panel(env: Env) -> None:
    """htmx replaces the panel after each message: the script must not come back with it."""
    with_ai(env)
    c = env.staff_a()
    page = c.get("/chat").text
    panel_end = page.index("</section>", page.index('id="chat"'))
    assert page.index(TAG) > panel_end


def test_no_ai_no_photo_box_in_the_chat(env: Env) -> None:
    html = env.staff_a().get("/chat").text
    assert "/sales/screenshot" not in html and TAG not in html
