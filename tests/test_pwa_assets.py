from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.web.templating import TEMPLATES_DIR, render

WEB_DIR = Path(__file__).resolve().parent.parent / "app" / "web"
STATIC = WEB_DIR / "static"
HTMX_SHA384 = "HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"


def test_vendored_htmx_matches_recorded_hash() -> None:
    digest = base64.b64encode(hashlib.sha384((STATIC / "htmx.min.js").read_bytes()).digest())
    assert digest.decode() == HTMX_SHA384


def test_vendored_md_records_version_source_and_hash() -> None:
    text = (STATIC / "VENDORED.md").read_text(encoding="utf-8")
    assert HTMX_SHA384 in text
    assert "2.0.4" in text
    assert "https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js" in text
    assert b'version:"2.0.4"' in (STATIC / "htmx.min.js").read_bytes()


def test_htmx_config_meta_disables_eval_and_injected_styles() -> None:
    html = render("base.html")
    match = re.search(r"<meta name=\"htmx-config\" content='([^']+)'>", html)
    assert match
    config = json.loads(match.group(1))
    assert config["allowEval"] is False
    assert config["includeIndicatorStyles"] is False


def test_templates_are_csp_safe() -> None:
    for path in TEMPLATES_DIR.rglob("*.html"):
        src = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", src), path
        assert not re.search(r"<style", src, re.I), path
        assert not re.search(r"\sstyle=", src, re.I), path
        assert not re.search(r"\son[a-z]+=", src, re.I), path
        assert "hx-on" not in src, path
        assert "javascript:" not in src, path


def test_base_template_links_assets_and_manifest() -> None:
    html = render("base.html")
    assert '<script src="/static/htmx.min.js" defer></script>' in html
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in html
    assert '<script src="/static/register-sw.js" defer></script>' in html


def test_manifest_is_valid(client: TestClient) -> None:
    resp = client.get("/static/manifest.webmanifest")
    assert resp.status_code == 200
    manifest = resp.json()
    assert manifest["name"]
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["lang"] == "fr"
    assert manifest["dir"] == "auto"
    sizes = {icon["sizes"] for icon in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in manifest["icons"]:
        assert (STATIC / Path(icon["src"]).name).is_file()


def test_service_worker_route_headers(client: TestClient) -> None:
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-cache"
    assert "javascript" in resp.headers["content-type"]
    assert resp.headers["service-worker-allowed"] == "/"
    assert "caches" in resp.text


def test_static_assets_are_served(client: TestClient) -> None:
    for name in ("htmx.min.js", "app.css", "register-sw.js"):
        assert client.get(f"/static/{name}").status_code == 200


def test_css_uses_logical_properties_only() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    physical = re.compile(
        r"(margin|padding|border)-(left|right|top|bottom)\b"
        r"|(?<![\w-])(left|right)\s*:"
        r"|text-align\s*:\s*(left|right)"
        r"|float\s*:|(?<![\w-])(width|height)\s*:"
    )
    assert not physical.findall(css)
    assert "margin-inline" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run sw.js")
def test_service_worker_behaviour() -> None:
    node = shutil.which("node") or "node"
    harness = str(Path(__file__).with_name("sw_harness.js"))
    proc = subprocess.run(  # noqa: S603
        [node, harness, str(WEB_DIR / "sw.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    out: dict[str, Any] = json.loads(proc.stdout)
    precached = sorted(["/offline", "/static/app.css", "/static/htmx.min.js"])
    assert out["afterInstall"] == precached
    assert "old-cache-v0" not in out["cachesAfterActivate"]

    # HTML from other routes is passed through and never stored.
    page = "body of https://app.test/questions?date=2026-10-01"
    assert out["navOnline"] == {"intercepted": True, "body": page}
    assert out["afterNavOnline"] == precached

    # Static files are stored when fetched and served from cache when offline.
    assert out["afterStaticOnline"] == sorted([*precached, "/static/new.js"])
    assert out["staticOffline"]["body"] == "body of https://app.test/static/new.js"

    # A failed navigation shows the offline page.
    assert out["navOffline"] == {"intercepted": True, "body": "body of https://app.test/offline"}

    # Everything else is left to the browser.
    for key in ("post", "crossOrigin", "xhr"):
        assert out[key] == {"intercepted": False}, key
    assert out["afterOffline"] == out["afterStaticOnline"]
