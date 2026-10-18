from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.observability import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    request_id_var,
)
from app.settings import Settings
from app.web.main import create_app

REQUEST_LOGGER = "app.request"


@pytest.fixture
def obs_client(web_settings: Settings) -> Iterator[TestClient]:
    app = create_app(web_settings)

    def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_api_route("/t/items/{item_id}", item)
    app.add_api_route("/t/boom", boom)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def request_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == REQUEST_LOGGER]


def render(record: logging.LogRecord) -> dict[str, Any]:
    f = RedactionFilter()
    f.filter(record)
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def test_request_is_logged_with_id_method_route_status_and_duration(
    obs_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=REQUEST_LOGGER):
        resp = obs_client.get("/healthz")
    (rec,) = request_records(caplog)
    request_id = resp.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert rec.request_id == request_id  # type: ignore[attr-defined]
    assert rec.method == "GET"  # type: ignore[attr-defined]
    assert rec.route == "/healthz"  # type: ignore[attr-defined]
    assert rec.status == 200  # type: ignore[attr-defined]
    assert rec.duration_ms >= 0  # type: ignore[attr-defined]


def test_route_template_is_logged_not_the_raw_path_or_query(
    obs_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=REQUEST_LOGGER):
        obs_client.get("/t/items/424242?token=s3cr3t&phone=22244556677")
    (rec,) = request_records(caplog)
    assert rec.route == "/t/items/{item_id}"  # type: ignore[attr-defined]
    blob = json.dumps(render(rec))
    for leaked in ("424242", "s3cr3t", "22244556677", "token="):
        assert leaked not in blob


def test_unmatched_paths_are_not_logged_verbatim(
    obs_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=REQUEST_LOGGER):
        obs_client.get("/wp-admin/secret-path-123")
    (rec,) = request_records(caplog)
    assert rec.status == 404  # type: ignore[attr-defined]
    assert "secret-path-123" not in json.dumps(render(rec))


def test_each_request_gets_its_own_id_and_client_ids_are_ignored(obs_client: TestClient) -> None:
    a = obs_client.get("/healthz", headers={"X-Request-ID": "attacker\nforged"})
    b = obs_client.get("/healthz")
    assert a.headers["x-request-id"] != b.headers["x-request-id"]
    assert "forged" not in a.headers["x-request-id"]


def test_error_responses_have_a_request_id_and_the_500_log_carries_it(
    obs_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        resp = obs_client.get("/t/boom")
    request_id = resp.headers["x-request-id"]
    (rec,) = request_records(caplog)
    assert rec.status == 500  # type: ignore[attr-defined]
    errors = [r for r in caplog.records if r.name == "app.web.errors"]
    assert errors
    assert errors[0].request_id == request_id  # type: ignore[attr-defined]


def test_formatter_adds_the_current_request_id() -> None:
    logger = logging.getLogger("t.fmt")
    token = request_id_var.set("abc123")
    try:
        rec = logger.makeRecord("t.fmt", logging.INFO, __file__, 1, "m", (), None)
        assert json.loads(JsonFormatter().format(rec))["request_id"] == "abc123"
    finally:
        request_id_var.reset(token)


def test_request_id_context_is_cleared_after_the_request(obs_client: TestClient) -> None:
    obs_client.get("/healthz")
    assert request_id_var.get() is None


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "new_password",
        "token",
        "access_token",
        "csrf_token",
        "X-CSRF-Token",
        "cookie",
        "Cookie",
        "Authorization",
        "client_secret",
        "Set-Cookie",
    ],
)
def test_sensitive_keys_are_redacted(key: str) -> None:
    logger = logging.getLogger("t.redact")
    rec = logger.makeRecord(
        "t.redact", logging.INFO, __file__, 1, "msg", (), None, extra={key: "hunter2"}
    )
    out = render(rec)
    assert out[key] == REDACTED
    assert "hunter2" not in json.dumps(out)


def test_nested_values_are_redacted_and_safe_keys_kept() -> None:
    logger = logging.getLogger("t.redact")
    payload: dict[str, Any] = {
        "headers": {"Authorization": "Bearer abc", "accept": "text/html"},
        "items": [{"password": "p1"}, {"name": "ok"}],
    }
    rec = logger.makeRecord(
        "t.redact", logging.INFO, __file__, 1, "msg", (), None, extra={"ctx": payload}
    )
    out = render(rec)
    assert out["ctx"]["headers"] == {"Authorization": REDACTED, "accept": "text/html"}
    assert out["ctx"]["items"] == [{"password": REDACTED}, {"name": "ok"}]
    assert payload["headers"]["Authorization"] == "Bearer abc"  # caller's data untouched


def test_json_formatter_output_shape_and_exceptions() -> None:
    logger = logging.getLogger("t.fmt")
    try:
        raise ValueError("bad")
    except ValueError:
        rec = logger.makeRecord(
            "t.fmt", logging.ERROR, __file__, 1, "oops %s", ("x",), sys.exc_info()
        )
    out = json.loads(JsonFormatter().format(rec))
    assert out["level"] == "ERROR"
    assert "request_id" not in out
    assert out["logger"] == "t.fmt"
    assert out["message"] == "oops x"
    assert "ts" in out
    assert "ValueError" in out["exception"]


def test_json_formatter_never_fails_on_odd_values() -> None:
    logger = logging.getLogger("t.fmt")
    rec = logger.makeRecord(
        "t.fmt", logging.INFO, __file__, 1, "m", (), None, extra={"obj": object(), "b": b"x"}
    )
    assert json.loads(JsonFormatter().format(rec))["message"] == "m"


def test_configure_logging_is_idempotent_and_silences_uvicorn_access() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        configure_logging()
        configure_logging()
        added = [h for h in root.handlers if h not in before]
        assert len(added) == 1
        assert isinstance(added[0].formatter, JsonFormatter)
        assert any(isinstance(f, RedactionFilter) for f in added[0].filters)
        assert logging.getLogger("uvicorn.access").disabled is True
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        logging.getLogger("uvicorn.access").disabled = False
