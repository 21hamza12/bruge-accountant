from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.settings import Settings
from app.web.main import create_app
from app.web.security import CSP, PERMISSIONS_POLICY

EXPECTED_STATIC = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "permissions-policy": PERMISSIONS_POLICY,
}


def test_csp_is_strict() -> None:
    directives = {d.split()[0]: d.split()[1:] for d in CSP.split("; ")}
    assert directives["default-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert directives["form-action"] == ["'self'"]
    assert "unsafe-inline" not in CSP
    assert "unsafe-eval" not in CSP
    assert "*" not in CSP


def build(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)

    def boom() -> None:
        raise RuntimeError("x")

    def validated(n: int) -> dict[str, int]:
        return {"n": n}

    app.add_api_route("/t/boom", boom)
    app.add_api_route("/t/validated", validated)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def dev_client(web_settings: Settings) -> Iterator[TestClient]:
    yield from build(web_settings)


@pytest.fixture
def prod_client(web_settings: Settings) -> Iterator[TestClient]:
    validated = Settings.model_validate(
        {
            "env": "production",
            "allowed_hosts": "testserver",
            "database_url": "postgresql://app:pw@db.invalid/bruge",
        }
    )
    # Production rules reject SQLite, so swap the test database in after validation.
    yield from build(validated.model_copy(update={"database_url": web_settings.database_url}))


@pytest.mark.parametrize("path", ["/healthz", "/nope", "/t/boom", "/t/validated?n=x", "/offline"])
def test_security_headers_on_every_response_including_errors(
    dev_client: TestClient, path: str
) -> None:
    resp = dev_client.get(path)
    for name, value in EXPECTED_STATIC.items():
        assert resp.headers[name] == value, (path, name)


def test_security_headers_on_405(dev_client: TestClient) -> None:
    resp = dev_client.post("/healthz")
    assert resp.status_code == 405
    assert resp.headers["content-security-policy"] == CSP


def test_no_hsts_outside_production(dev_client: TestClient) -> None:
    assert "strict-transport-security" not in dev_client.get("/healthz").headers


@pytest.mark.parametrize("path", ["/healthz", "/nope", "/t/boom"])
def test_hsts_in_production(prod_client: TestClient, path: str) -> None:
    value = prod_client.get(path).headers["strict-transport-security"]
    assert "max-age=31536000" in value
    assert "includeSubDomains" in value


def test_untrusted_host_is_rejected(dev_client: TestClient) -> None:
    resp = dev_client.get("/healthz", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
    assert resp.headers["content-security-policy"] == CSP


def test_no_cors_headers_without_allowlist(dev_client: TestClient) -> None:
    resp = dev_client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in resp.headers
    pre = dev_client.options(
        "/healthz",
        headers={"Origin": "https://evil.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in pre.headers


def test_cors_allowlist_only_allows_listed_origins(web_settings: Settings) -> None:
    settings = web_settings.model_copy(update={"cors_allowed_origins": ["https://app.example.com"]})
    for c in build(settings):
        ok = c.get("/healthz", headers={"Origin": "https://app.example.com"})
        assert ok.headers["access-control-allow-origin"] == "https://app.example.com"
        bad = c.get("/healthz", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in bad.headers
        assert "*" not in ok.headers.get("access-control-allow-origin", "")
