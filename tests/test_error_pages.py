from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.settings import Settings
from app.web.main import create_app

FORBIDDEN = ("traceback", "fastapi", "starlette", "uvicorn", "pydantic", "sqlalchemy")
INTERNAL_DETAIL = "internal-detail-xyzzy"


@pytest.fixture
def err_client(web_settings: Settings) -> Iterator[TestClient]:
    app = create_app(web_settings)

    def boom() -> None:
        raise RuntimeError(INTERNAL_DETAIL)

    def forbidden() -> None:
        raise HTTPException(status_code=403, detail=INTERNAL_DETAIL)

    def unauthorized() -> None:
        raise HTTPException(
            status_code=401, detail=INTERNAL_DETAIL, headers={"WWW-Authenticate": "Bearer"}
        )

    def too_many() -> None:
        raise HTTPException(status_code=429, headers={"Retry-After": "60"})

    def teapot() -> None:
        raise HTTPException(status_code=418)

    def unavailable() -> None:
        raise HTTPException(status_code=503, detail=INTERNAL_DETAIL, headers={"Retry-After": "60"})

    def bad_gateway() -> None:
        raise HTTPException(status_code=502, detail=INTERNAL_DETAIL)

    def too_large() -> None:
        raise HTTPException(status_code=413, detail=INTERNAL_DETAIL)

    def validated(n: int) -> dict[str, int]:
        return {"n": n}

    app.add_api_route("/t/boom", boom)
    app.add_api_route("/t/forbidden", forbidden)
    app.add_api_route("/t/unauthorized", unauthorized)
    app.add_api_route("/t/too-many", too_many)
    app.add_api_route("/t/teapot", teapot)
    app.add_api_route("/t/unavailable", unavailable)
    app.add_api_route("/t/bad-gateway", bad_gateway)
    app.add_api_route("/t/too-large", too_large)
    app.add_api_route("/t/validated", validated)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def assert_clean(resp_text: str, headers: dict[str, str]) -> None:
    blob = (resp_text + " " + " ".join(f"{k}: {v}" for k, v in headers.items())).lower()
    for word in FORBIDDEN:
        assert word not in blob, word
    assert INTERNAL_DETAIL not in blob
    assert "server" not in {k.lower() for k in headers}


@pytest.mark.parametrize(
    ("path", "status", "french"),
    [
        ("/nope", 404, "Page introuvable"),
        ("/t/forbidden", 403, "Accès refusé"),
        ("/t/unauthorized", 401, "Connexion requise"),
        ("/t/too-many", 429, "Trop de tentatives"),
        ("/t/validated?n=abc", 400, "Requête invalide"),
        ("/t/boom", 500, "Une erreur est survenue"),
        ("/t/too-large", 413, "Fichier trop volumineux"),
        ("/t/unavailable", 503, "Service occupé"),
    ],
)
def test_error_pages_are_custom_french_and_leak_nothing(
    err_client: TestClient, path: str, status: int, french: str
) -> None:
    resp = err_client.get(path)
    assert resp.status_code == status
    assert resp.headers["content-type"].startswith("text/html")
    assert french in resp.text
    assert '<html lang="fr"' in resp.text
    assert_clean(resp.text, dict(resp.headers))


def test_validation_error_hides_field_details(err_client: TestClient) -> None:
    resp = err_client.get("/t/validated?n=abc")
    for fragment in ("int_parsing", "Input should", "valid integer", "query"):
        assert fragment not in resp.text


def test_unknown_statuses_use_the_closest_page_and_keep_their_code(
    err_client: TestClient,
) -> None:
    assert err_client.get("/t/teapot").status_code == 418
    assert "Requête invalide" in err_client.get("/t/teapot").text
    resp = err_client.get("/t/bad-gateway")
    assert resp.status_code == 502
    assert "Une erreur est survenue" in resp.text
    assert INTERNAL_DETAIL not in resp.text


def test_method_not_allowed_is_a_custom_page(err_client: TestClient) -> None:
    resp = err_client.post("/healthz")
    assert resp.status_code == 405
    assert "Méthode non autorisée" in resp.text
    # Starlette builds the header from a set, so its order depends on the hash seed.
    assert {m.strip() for m in resp.headers["allow"].split(",")} == {"GET", "HEAD"}
    assert_clean(resp.text, {k: v for k, v in resp.headers.items() if k != "allow"})


def test_only_safe_exception_headers_are_forwarded(err_client: TestClient) -> None:
    assert "www-authenticate" not in err_client.get("/t/unauthorized").headers
    assert err_client.get("/t/too-many").headers["retry-after"] == "60"


def test_reference_code_is_random_per_response(err_client: TestClient) -> None:
    pattern = re.compile(r"Référence\s*:\s*<code>([0-9A-F]{8})</code>")
    codes = {m.group(1) for _ in range(5) if (m := pattern.search(err_client.get("/nope").text))}
    assert len(codes) == 5


def test_500_is_logged_with_the_reference_code_but_not_shown(
    err_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("ERROR"):
        resp = err_client.get("/t/boom")
    ref = re.search(r"<code>([0-9A-F]{8})</code>", resp.text)
    assert ref
    records = [r for r in caplog.records if ref.group(1) in r.getMessage()]
    assert records
    assert records[0].exc_info is not None
    assert INTERNAL_DETAIL not in resp.text


def test_error_pages_are_not_cached(err_client: TestClient) -> None:
    assert err_client.get("/nope").headers["cache-control"] == "no-store"


def test_offline_page_is_served(err_client: TestClient) -> None:
    resp = err_client.get("/offline")
    assert resp.status_code == 200
    assert "Vous êtes hors ligne" in resp.text


def test_html_escaping_is_on_by_default() -> None:
    from app.web.templating import render_string

    assert render_string("{{ x }}", x="<script>alert(1)</script>") == (
        "&lt;script&gt;alert(1)&lt;/script&gt;"
    )


def test_413_page_explains_the_file_is_too_large(err_client: TestClient) -> None:
    resp = err_client.get("/t/too-large")
    assert "trop volumineux" in resp.text
    assert "Référence" in resp.text


def test_503_page_says_to_retry_in_a_minute_and_keeps_retry_after(err_client: TestClient) -> None:
    resp = err_client.get("/t/unavailable")
    assert "dans une minute" in resp.text
    assert resp.headers["retry-after"] == "60"


def test_untrusted_host_gets_the_custom_400_page_with_no_store(err_client: TestClient) -> None:
    resp = err_client.get("/healthz", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "Requête invalide" in resp.text
    assert resp.headers["cache-control"] == "no-store"
    assert "Invalid host header" not in resp.text
    assert re.fullmatch(r"[0-9a-f]{32}", resp.headers["x-request-id"])
    assert re.search(r"<code>[0-9A-F]{8}</code>", resp.text)
    assert_clean(resp.text, dict(resp.headers))


@pytest.mark.parametrize(
    ("allowed", "host", "ok"),
    [
        (["a.example.com"], "a.example.com", True),
        (["a.example.com"], "A.Example.com:8443", True),
        (["a.example.com"], "b.example.com", False),
        (["a.example.com"], "xa.example.com", False),
        (["a.example.com"], "", False),
        (["*.example.com"], "x.example.com", True),
        (["*.example.com"], "example.com", False),
        (["*.example.com"], "evilexample.com", False),
        (["localhost"], "[::1]:8000", False),
        (["[::1]"], "[::1]:8000", True),
    ],
)
def test_host_matching(allowed: list[str], host: str, ok: bool) -> None:
    from app.web.security import host_allowed

    assert host_allowed(host, allowed) is ok
