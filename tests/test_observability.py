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


@pytest.mark.parametrize("name", ["PIL", "anthropic", "httpx", "httpx2", "httpcore", "httpcore2"])
def test_the_libraries_that_log_what_they_send_are_pinned_to_warning(name: str) -> None:
    from app.observability import quiet_libraries

    logger = logging.getLogger(name)
    before = logger.level
    try:
        logger.setLevel(logging.DEBUG)  # what ANTHROPIC_LOG=debug does when the SDK is imported
        quiet_libraries()
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(before)


def test_building_the_ai_clients_pins_the_libraries_again() -> None:
    """The SDK reads ANTHROPIC_LOG when it is first imported, which is after configure_logging."""
    from pydantic import SecretStr

    from app.settings import Settings
    from app.web.main import _real_ai_client, _real_chat_client

    names = ("anthropic", "httpx", "httpx2", "httpcore")
    key = SecretStr("sk-test-not-real")
    for build in (_real_chat_client, _real_ai_client):
        for name in names:
            logging.getLogger(name).setLevel(logging.DEBUG)
        assert build(Settings(anthropic_api_key=key)) is not None
        assert all(logging.getLogger(n).level == logging.WARNING for n in names), build


# --- redaction is the formatter's final step: no leak may depend on the filter being installed --


def fmt(
    msg: Any,
    *args: Any,
    extra: dict[str, Any] | None = None,
    exc: BaseException | None = None,
    stack: str | None = None,
) -> dict[str, Any]:
    """Format a record with the JsonFormatter alone (no RedactionFilter)."""
    logger = logging.getLogger("t.leak")
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    record = logger.makeRecord(
        "t.leak", logging.INFO, __file__, 1, msg, args, exc_info, extra=extra
    )
    record.stack_info = stack
    parsed: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return parsed


def assert_no_leak(out: dict[str, Any], *secrets: str) -> None:
    blob = json.dumps(out, ensure_ascii=False)
    for secret in secrets:
        assert secret not in blob, secret


def test_leak_message_args_dict() -> None:
    out = fmt("login %s", {"password": "P1"})
    assert_no_leak(out, "P1")
    assert out["message"].startswith("login")


def test_leak_message_args_tuple_of_values() -> None:
    out = fmt("cfg %s %s", {"token": "P1b"}, "password=P1c&x=1")
    assert_no_leak(out, "P1b", "P1c")
    assert "x=1" in out["message"]


def test_leak_message_that_is_not_a_string() -> None:
    assert_no_leak(fmt({"password": "P2"}), "P2")


def test_leak_secret_inside_an_already_formatted_message() -> None:
    out = fmt("login {'password': 'P2b', 'user': 'bob'} ok")
    assert_no_leak(out, "P2b")
    assert "bob" in out["message"]
    assert_no_leak(fmt('{"api_key": "P2c"}'), "P2c")


def test_leak_bearer_token_in_message() -> None:
    out = fmt("sent Authorization: Bearer abc.DEF-123 to upstream")
    assert_no_leak(out, "abc.DEF-123")
    assert "Bearer [REDACTED]" in out["message"]


def test_leak_url_userinfo_in_message() -> None:
    out = fmt("connecting to postgresql+psycopg://owner:P3@db.internal:5432/bruge")
    assert_no_leak(out, "P3", "owner:")
    assert "postgresql+psycopg://[REDACTED]@db.internal:5432/bruge" in out["message"]


def test_leak_url_userinfo_without_a_password_and_with_an_at_sign() -> None:
    assert_no_leak(fmt("dsn https://key123@o1.ingest.sentry.io/1"), "key123")
    assert_no_leak(fmt("postgres://u:p@ss@h/d"), "p@ss", "ss@h")


def test_leak_exception_text() -> None:
    try:
        raise ValueError("postgres://u:P6@h/d")
    except ValueError as exc:
        out = fmt("failed", exc=exc)
    assert_no_leak(out, "P6")
    assert "ValueError" in out["exception"]


def test_leak_exception_chain_text() -> None:
    try:
        try:
            raise OSError("password=P6b&host=h")
        except OSError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as exc:
        out = fmt("failed", exc=exc)
    assert_no_leak(out, "P6b")
    assert "wrapped" in out["exception"]


def test_leak_stack_info_text() -> None:
    out = fmt("m", stack="Stack (most recent call last):\n  token=P6c\n")
    assert_no_leak(out, "P6c")
    assert "Stack (most recent call last)" in out["stack"]


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "apikey",
        "API-KEY",
        "database_url",
        "dsn",
        "sentry_dsn",
        "session",
        "session_id",
        "passwd",
        "pwd",
        "creds",
        "credential",
        "credentials",
        "auth",
        "x-auth",
        "bearer",
        "private",
        "private_key",
    ],
)
def test_leak_widened_sensitive_keys(key: str) -> None:
    out = fmt("m", extra={key: "S3CRET", "nested": {key: "S3CRET2"}})
    assert out[key] == REDACTED
    assert out["nested"] == {key: REDACTED}
    assert_no_leak(out, "S3CRET")


def test_leak_plain_string_values_with_key_value_pairs() -> None:
    out = fmt("m", extra={"query": "password=P4&x=1", "note": "a token=T4 b"})
    assert out["query"] == "password=[REDACTED]&x=1"
    assert out["note"] == "a token=[REDACTED] b"
    assert_no_leak(out, "P4", "T4")


@pytest.mark.parametrize("name", ["pwd", "passwd", "secret", "api_key", "api-key", "apikey"])
def test_leak_each_key_value_pattern(name: str) -> None:
    out = fmt("m", extra={"q": f"a=1&{name}=ZZ9&b=2"})
    assert out["q"] == f"a=1&{name}=[REDACTED]&b=2"


def test_leak_header_pairs_as_tuples() -> None:
    out = fmt("m", extra={"headers": [("authorization", "Bearer B"), ("accept", "text/html")]})
    assert_no_leak(out, "Bearer B")
    assert out["headers"] == [["authorization", REDACTED], ["accept", "text/html"]]


def test_leak_pair_tuple_with_a_non_string_first_element_is_kept() -> None:
    assert fmt("m", extra={"p": (1, 2)})["p"] == [1, 2]


@pytest.mark.parametrize("make", [set, frozenset])
def test_leak_sets_are_walked(make: Any) -> None:
    out = fmt("m", extra={"s": make({"password=P5", "ok"})})
    assert_no_leak(out, "P5")
    assert sorted(out["s"]) == ["ok", "password=[REDACTED]"]


def test_leak_unknown_objects_log_their_type_not_their_repr() -> None:
    class Holder:
        def __repr__(self) -> str:
            return "Holder(password='P7')"

        def __str__(self) -> str:
            return "str-P7"

    out = fmt("m", extra={"obj": Holder(), "inner": [Holder()], "d": {"k": Holder()}})
    assert_no_leak(out, "P7")
    assert out["obj"] == "<Holder>"
    assert out["inner"] == ["<Holder>"]
    assert out["d"] == {"k": "<Holder>"}


def test_leak_unknown_objects_in_args_log_their_type() -> None:
    class Holder:
        def __str__(self) -> str:
            return "str-P8"

    out = fmt("got %s", Holder())
    assert_no_leak(out, "P8")
    assert out["message"] == "got <Holder>"


def test_leak_non_string_dict_keys_cannot_smuggle_a_repr() -> None:
    class Key:
        def __repr__(self) -> str:
            return "key-P9"

        def __hash__(self) -> int:
            return 1

    out = fmt("m", extra={"d": {Key(): "v", 5: "five"}})
    assert_no_leak(out, "P9")
    assert out["d"] == {"<Key>": "v", "5": "five"}


def test_cyclic_and_deep_structures_do_not_crash() -> None:
    loop: list[Any] = []
    loop.append(loop)
    deep: dict[str, Any] = {}
    cursor = deep
    for _ in range(200):
        cursor["k"] = {}
        cursor = cursor["k"]
    out = fmt("m", extra={"loop": loop, "deep": deep})
    assert "loop" in out
    assert "deep" in out


def test_harmless_scalars_survive() -> None:
    out = fmt("m", extra={"n": 3, "f": 1.5, "b": True, "none": None, "s": "plain"})
    assert (out["n"], out["f"], out["b"], out["none"], out["s"]) == (3, 1.5, True, None, "plain")


def test_reserved_fields_cannot_be_overwritten_by_extra() -> None:
    token = request_id_var.set("real-id")
    try:
        out = fmt(
            "real message",
            extra={
                "ts": "forged",
                "level": "FORGED",
                "logger": "forged",
                "request_id": "forged",
                "exception": "forged",
                "safe": "kept",
            },
        )
    finally:
        request_id_var.reset(token)
    assert out["message"] == "real message"
    assert out["level"] == "INFO"
    assert out["logger"] == "t.leak"
    assert out["request_id"] == "real-id"
    assert out["ts"] != "forged"
    assert "exception" not in out
    assert out["safe"] == "kept"
    assert "forged" not in json.dumps(out)


@pytest.mark.parametrize("path", ["/static/app.css", "/static/no-such-file-123.js"])
def test_static_requests_are_logged_with_the_static_route_label(
    obs_client: TestClient, caplog: pytest.LogCaptureFixture, path: str
) -> None:
    with caplog.at_level(logging.INFO, logger=REQUEST_LOGGER):
        obs_client.get(path)
    (rec,) = request_records(caplog)
    assert rec.route == "static"  # type: ignore[attr-defined]
    assert "no-such-file" not in json.dumps(render(rec))


# --- durable logs: every line is one JSON object (release item 8) ----------------------------

LINE_FIELDS = {"ts", "level", "logger", "message"}


def stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    """Every non-empty stdout line must be a whole JSON object (what a log service parses)."""
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    parsed = [json.loads(line) for line in lines]
    assert all(isinstance(doc, dict) for doc in parsed)
    return parsed


def test_every_line_of_a_request_is_one_json_object_with_the_request_id(
    web_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(web_settings)

    def boom() -> None:
        raise RuntimeError("kaboom\nsecond line password=hunter2")

    app.add_api_route("/t/boom", boom)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        ok = c.get("/healthz")
        missing = c.get("/nope?token=s3cr3t")
        broken = c.get("/t/boom")
    docs = stdout_lines(capsys)
    assert docs
    for doc in docs:
        assert doc.keys() >= LINE_FIELDS
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}\+00:00", doc["ts"])
        assert doc["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    ids = {r.headers["x-request-id"] for r in (ok, missing, broken)}
    lines_by_id = {rid: [d for d in docs if d.get("request_id") == rid] for rid in ids}
    for rid, request_docs in lines_by_id.items():
        assert any(d["logger"] == "app.request" for d in request_docs), rid
    # the failing request: the error line and the request line share the id, the traceback is one
    # line, and no secret survived
    broken_docs = lines_by_id[broken.headers["x-request-id"]]
    assert {d["logger"] for d in broken_docs} >= {"app.request", "app.web.errors"}
    assert "Traceback" in next(d for d in broken_docs if "exception" in d)["exception"]
    blob = json.dumps(docs)
    for leaked in ("hunter2", "s3cr3t", "token="):
        assert leaked not in blob


def test_a_multiline_message_stays_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging()
    logging.getLogger("t.multi").info("first\nsecond\r\nthird")
    (doc,) = [d for d in stdout_lines(capsys) if d["logger"] == "t.multi"]
    assert doc["message"] == "first\nsecond\r\nthird"


def test_uvicorn_own_messages_are_json_too(capsys: pytest.CaptureFixture[str]) -> None:
    """uvicorn sets up plain-text handlers before the factory runs; create_app() takes them over."""
    err = logging.getLogger("uvicorn.error")
    plain = logging.StreamHandler(sys.stdout)
    plain.setFormatter(logging.Formatter("INFO:     %(message)s"))
    err.addHandler(plain)
    err.propagate = False
    configure_logging()
    err.info(
        "Uvicorn running on %s", "http://0.0.0.0:10000", extra={"color_message": "\x1b[1mx\x1b[0m"}
    )
    (doc,) = stdout_lines(capsys)
    assert doc["logger"] == "uvicorn.error"
    assert doc["message"] == "Uvicorn running on http://0.0.0.0:10000"
    assert "color_message" not in doc


def test_the_uvicorn_access_log_prints_nothing_so_no_query_string_or_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    access = logging.getLogger("uvicorn.access")
    plain = logging.StreamHandler(sys.stdout)  # what uvicorn installs by default
    access.addHandler(plain)
    configure_logging()
    access.info(
        '%s - "%s %s HTTP/%s" %d',
        "203.0.113.9:5555",
        "POST",
        "/login?phone=22244556677&token=s3cr3t",
        "1.1",
        200,
    )
    assert capsys.readouterr().out == ""


def test_uvicorn_is_started_without_its_access_log() -> None:
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text("utf-8")
    assert "--no-access-log" in dockerfile


def test_healthz_body_matches_the_uptime_monitor_keyword(obs_client: TestClient) -> None:
    """runbook section 6: the monitor looks for the exact text "status":"ok" (GET, and HEAD for
    monitors that default to it)."""
    resp = obs_client.get("/healthz")
    assert '"status":"ok"' in resp.text
    assert obs_client.head("/healthz").status_code == 200
