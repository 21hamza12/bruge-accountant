from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from app.web.templating import TEMPLATES_DIR, render

WEB_DIR = Path(__file__).resolve().parent.parent / "app" / "web"
ROOT = WEB_DIR.parent.parent
STATIC = WEB_DIR / "static"
HTMX_SHA384 = "HGfztofotfshcF7+8n44JQL2oJmowVChPTg48S+jvZoztPfvwD79OC/LTtG6dMp+"


def test_vendored_htmx_matches_recorded_hash() -> None:
    digest = base64.b64encode(hashlib.sha384((STATIC / "htmx.min.js").read_bytes()).digest())
    assert digest.decode() == HTMX_SHA384


def test_vendored_md_is_not_served_from_the_static_directory() -> None:
    assert not (STATIC / "VENDORED.md").exists()
    assert (WEB_DIR / "VENDORED.md").is_file()


def test_vendored_md_is_not_reachable_over_http(client: TestClient) -> None:
    assert client.get("/static/VENDORED.md").status_code == 404


def test_vendored_md_records_version_source_and_hash() -> None:
    text = (WEB_DIR / "VENDORED.md").read_text(encoding="utf-8")
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
    # htmx would otherwise keep page snapshots (tenant data) in sessionStorage.
    assert config["historyCacheSize"] == 0


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


def require_node() -> str:
    """Path to node. Missing node skips locally but fails in CI, where a skip hides a gap."""
    node = shutil.which("node")
    if node is not None:
        return node
    if os.environ.get("CI"):
        pytest.fail("node is required in CI to run the service worker tests")
    pytest.skip("node is needed to run sw.js")


def test_missing_node_fails_in_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception, match="node is required in CI"):
        require_node()


def test_missing_node_skips_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception):
        require_node()


def test_ci_has_node_available_for_the_service_worker_test() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    uses = [str(step.get("uses", "")) for step in ci["jobs"]["checks"]["steps"]]
    assert any(u.startswith("actions/setup-node@") for u in uses)


def test_service_worker_behaviour() -> None:
    node = require_node()
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
    assert out["navOnline"] == {"intercepted": True, "body": page, "waited": 0}
    assert out["afterNavOnline"] == precached

    # Static files are stored when fetched (inside waitUntil, so the worker is not killed
    # mid-write) and served from cache when offline.
    assert out["staticOnline"]["waited"] == 1
    assert out["afterStaticOnline"] == sorted([*precached, "/static/new.js"])
    assert out["staticOffline"]["body"] == "body of https://app.test/static/new.js"

    # A non-OK static response (404, 500) is passed through and never cached.
    assert out["staticNotOk"] == {
        "intercepted": True,
        "body": "missing https://app.test/static/missing.js",
        "status": 404,
        "waited": 0,
    }
    assert out["afterStaticNotOk"] == out["afterStaticOnline"]
    assert out["staticServerError"]["status"] == 500
    assert out["afterStaticServerError"] == out["afterStaticOnline"]

    # A failed navigation shows the offline page.
    assert out["navOffline"] == {
        "intercepted": True,
        "body": "body of https://app.test/offline",
        "waited": 0,
    }

    # Everything else is left to the browser.
    for key in ("post", "crossOrigin", "xhr"):
        assert out[key] == {"intercepted": False}, key
    assert out["afterOffline"] == out["afterStaticOnline"]
