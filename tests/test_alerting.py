"""Error alerts (release item 8): Sentry behind SENTRY_DSN, and nothing personal leaves.

Every test uses a fake transport that keeps the envelopes in memory: nothing is ever sent.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sentry_sdk
from alembic import command
from alembic.util.exc import CommandError
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from app import alerting, cli
from app.observability import REDACTED
from app.settings import Settings
from app.web.main import create_app

DSN = "https://publickey123@o1.ingest.sentry.io/42"
PHONE = "22244556677"
API_KEY = "sk-ant-api03-abcdefghijklmnop"


class FakeTransport(Transport):
    """Collects what would have been sent."""

    def __init__(self) -> None:
        super().__init__({})
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    @property
    def events(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for envelope in self.envelopes:
            for item in envelope.items:
                if item.type == "event":
                    payload = item.payload.json
                    assert isinstance(payload, dict)
                    out.append(payload)
        return out

    def kinds(self) -> list[str]:
        return [str(item.type) for envelope in self.envelopes for item in envelope.items]


@pytest.fixture
def transport() -> Iterator[FakeTransport]:
    fake = FakeTransport()
    yield fake
    sentry_sdk.get_client().close(timeout=0)
    sentry_sdk.get_global_scope().set_client(None)


def settings_with(dsn: str | None, **extra: Any) -> Settings:
    return Settings(sentry_dsn=SecretStr(dsn) if dsn is not None else None, **extra)


def blob(event: Any) -> str:
    return json.dumps(event, default=str)


def assert_clean(event: dict[str, Any]) -> None:
    text = blob(event)
    for leaked in (PHONE, API_KEY, "hunter2", "csrf-abc", "session-cookie-value", "s3cr3t"):
        assert leaked not in text


# --- configuration -------------------------------------------------------------------------


def test_nothing_is_started_without_a_dsn(transport: FakeTransport) -> None:
    for dsn in (None, "", "   "):
        assert alerting.init_alerting(settings_with(dsn), transport=transport) is False
        assert not sentry_sdk.get_client().is_active()
    alerting.capture_exception(RuntimeError("x"))
    assert transport.envelopes == []


def test_options_are_private_and_free(transport: FakeTransport) -> None:
    settings = settings_with(DSN, render_git_commit="abc1234")
    assert alerting.init_alerting(settings, transport=transport) is True
    options = sentry_sdk.get_client().options
    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["include_source_context"] is False
    assert options["max_request_body_size"] == "never"
    assert options["max_breadcrumbs"] == 0
    assert not options["traces_sample_rate"]
    assert not options["profiles_sample_rate"]
    assert not options["enable_tracing"]
    assert options["release"] == "abc1234"
    assert options["environment"] == "development"
    assert options["auto_enabling_integrations"] is False


def test_release_and_environment_follow_the_settings(transport: FakeTransport) -> None:
    settings = settings_with(
        DSN,
        env="production",
        database_url=SecretStr("postgresql://u:p@db/x"),
        allowed_hosts=["example.org"],
        render_git_commit="deadbeef",
    )
    alerting.init_alerting(settings, transport=transport)
    options = sentry_sdk.get_client().options
    assert options["release"] == "deadbeef"
    assert options["environment"] == "production"


def test_release_matches_what_healthz_reports(
    transport: FakeTransport, web_settings: Settings
) -> None:
    settings = web_settings.model_copy(
        update={"sentry_dsn": SecretStr(DSN), "render_git_commit": "cafe123"}
    )
    with TestClient(create_app(settings, alerting_transport=transport)) as c:
        version = c.get("/healthz").json()["version"]
    assert sentry_sdk.get_client().options["release"] == version == "cafe123"


# --- the web app ---------------------------------------------------------------------------


@pytest.fixture
def alert_client(web_settings: Settings, transport: FakeTransport) -> Iterator[TestClient]:
    settings = web_settings.model_copy(update={"sentry_dsn": SecretStr(DSN)})
    app = create_app(settings, alerting_transport=transport)

    def boom() -> None:
        local_secret = "hunter2"  # noqa: F841, S105 - must never reach an event
        raise RuntimeError(f"kaboom for {PHONE} with {API_KEY}")

    async def aboom() -> None:
        raise RuntimeError("async kaboom")

    app.add_api_route("/t/boom", boom, methods=["GET", "POST"])
    app.add_api_route("/t/aboom", aboom)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def test_a_route_exception_makes_exactly_one_clean_event(
    alert_client: TestClient, transport: FakeTransport
) -> None:
    alert_client.cookies.set("bruge_session", "session-cookie-value")
    resp = alert_client.post(
        "/t/boom?token=s3cr3t&phone=" + PHONE,
        data={"password": "hunter2", "csrf_token": "csrf-abc", "phone": PHONE},
        headers={"Authorization": "Bearer " + API_KEY, "X-Api-Key": API_KEY},
    )
    assert resp.status_code == 500
    sentry_sdk.flush(timeout=1)
    (event,) = transport.events
    assert_clean(event)
    request = event.get("request", {})
    for dropped in ("data", "cookies", "headers", "query_string", "env"):
        assert dropped not in request
    assert "?" not in request.get("url", "")
    assert not event.get("user")
    assert not event.get("breadcrumbs", {}).get("values")
    frames = event["exception"]["values"][0]["stacktrace"]["frames"]
    assert all("vars" not in frame and "context_line" not in frame for frame in frames)
    assert event["exception"]["values"][0]["type"] == "RuntimeError"
    assert event["release"] == "dev"
    assert "server_name" not in event
    assert transport.kinds() == ["event"]  # no transaction, no profile, no session


def test_an_async_route_exception_makes_one_event(
    alert_client: TestClient, transport: FakeTransport
) -> None:
    assert alert_client.get("/t/aboom").status_code == 500
    sentry_sdk.flush(timeout=1)
    assert len(transport.events) == 1


def test_handled_responses_send_nothing(alert_client: TestClient, transport: FakeTransport) -> None:
    assert alert_client.get("/healthz").status_code == 200
    assert alert_client.get("/nope").status_code == 404
    assert alert_client.get("/t/boom", params={"x": "y"}).status_code == 500
    sentry_sdk.flush(timeout=1)
    assert len(transport.events) == 1  # only the 500


def test_logged_errors_do_not_become_events_or_breadcrumbs(
    alert_client: TestClient, transport: FakeTransport
) -> None:
    log = logging.getLogger("app.test")
    log.error("a plain error", extra={"phone": PHONE, "who": "Awa Diop"})
    log.warning("careful", extra={"note": PHONE})
    assert alert_client.get("/t/boom").status_code == 500
    sentry_sdk.flush(timeout=1)
    (event,) = transport.events
    assert event.get("logentry") is None
    assert "Awa Diop" not in blob(event)
    assert not event.get("breadcrumbs", {}).get("values")


def test_no_dsn_means_no_client_and_no_event(
    web_settings: Settings, transport: FakeTransport
) -> None:
    app = create_app(web_settings, alerting_transport=transport)

    def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_api_route("/t/boom", boom)
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        assert c.get("/t/boom").status_code == 500
    assert not sentry_sdk.get_client().is_active()
    assert transport.envelopes == []


def test_the_500_page_still_has_a_request_id_and_no_internals(alert_client: TestClient) -> None:
    resp = alert_client.get("/t/boom")
    assert resp.status_code == 500
    assert "x-request-id" in resp.headers
    assert "kaboom" not in resp.text


# --- the scrubber, directly ----------------------------------------------------------------


def test_scrubber_drops_request_details_and_user() -> None:
    event: Any = {
        "event_id": "0123456789abcdef0123456789abcdef",
        "release": "12345678ab",
        "request": {
            "method": "POST",
            "url": "https://x.test/sales/12?token=abc",
            "query_string": "token=abc",
            "data": {"password": "hunter2"},
            "cookies": {"s": "1"},
            "headers": {"Cookie": "s=1", "User-Agent": "UA", "X-Request-ID": "rid"},
            "env": {"REMOTE_ADDR": "1.2.3.4"},
        },
        "user": {"id": "7", "ip_address": "1.2.3.4", "username": "awa", "email": "a@b.c"},
        "extra": {"password": "hunter2", "note": f"call {PHONE}"},
        "contexts": {"trace": {"trace_id": "12345678123456781234567812345678"}},
        "breadcrumbs": {"values": [{"message": PHONE}]},
        "server_name": "host-1",
    }
    out = alerting.scrub_event(event, {})
    assert out is not None
    assert out["request"] == {"method": "POST", "url": "https://x.test/sales/12"}
    assert out["user"] == {}
    assert "breadcrumbs" not in out
    assert "server_name" not in out
    assert out["extra"]["password"] == REDACTED
    assert PHONE not in blob(out)
    # identifiers made of digits are not phone numbers
    assert out["event_id"] == event["event_id"]
    assert out["release"] == "12345678ab"
    assert out["contexts"]["trace"]["trace_id"] == "12345678123456781234567812345678"


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        (f"call +{PHONE}", PHONE),
        (f"call {PHONE}", PHONE),
        ("call +222 44 55 66 77", "44 55"),
        ("call 22 24 45 56 67", "45 56"),
        ("call (222) 4455 6677", "4455"),
        ("call 222-4455-6677", "4455"),
        (f"key {API_KEY}", "abcdefghijklmnop"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("postgresql://user:pw@db.internal/x", "user:pw"),
        ("password=hunter2", "hunter2"),
        ("api_key=zzz9", "zzz9"),
        ('repr {"token": "abc77"}', "abc77"),
        ("mail awa@example.org failed", "awa@example.org"),
    ],
)
def test_scrub_string_masks_personal_and_secret_text(text: str, forbidden: str) -> None:
    assert forbidden not in alerting.scrub_string(text)


def test_scrub_string_keeps_ordinary_numbers() -> None:
    plain = "line 12, 3 tickets, 2026-10-24, id 4521, ref 7F3A9C01, 0.0008 USD"
    assert alerting.scrub_string(plain) == plain


def test_sql_statements_and_parameters_are_cut_from_exception_text() -> None:
    event: Any = {
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": "(sqlite3.IntegrityError) UNIQUE constraint failed: users.phone\n"
                    "[SQL: INSERT INTO users (phone) VALUES (?)]\n[parameters: ('Awa Diop',)]",
                }
            ]
        }
    }
    out = alerting.scrub_event(event, {})
    assert out is not None
    value = out["exception"]["values"][0]["value"]
    assert "Awa Diop" not in value
    assert "INSERT" not in value
    assert "UNIQUE constraint failed" in value


def test_scrubber_never_raises_on_odd_events() -> None:
    event: Any = {"extra": {"o": object(), "n": None, "deep": [[[[[[[[[[1]]]]]]]]]]}}
    assert alerting.scrub_event(event, {}) is not None


# --- command line jobs ---------------------------------------------------------------------


@pytest.fixture
def job_env(transport: FakeTransport, monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    monkeypatch.setenv("SENTRY_DSN", DSN)
    monkeypatch.setattr(alerting, "default_transport", lambda: transport)
    return transport


def test_a_failing_purge_job_makes_one_event(job_env: FakeTransport, tmp_path: Path) -> None:
    empty_db = f"sqlite:///{tmp_path / 'empty.db'}"  # no tables: the purge fails
    assert cli.main(["purge-expired", "--db", empty_db]) == 1
    (event,) = job_env.events
    assert event["tags"]["job"] == "purge-expired"
    assert_clean(event)
    assert "request" not in event


def test_an_unexpected_job_exception_makes_one_event_and_is_raised(
    job_env: FakeTransport, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(url: str) -> int:
        raise ValueError(f"bad thing for {PHONE}")

    monkeypatch.setattr(cli, "_purge_expired", explode)
    with pytest.raises(ValueError, match="bad thing"):
        cli.main(["purge-expired", "--db", db_url])
    (event,) = job_env.events
    assert event["tags"]["job"] == "purge-expired"
    assert PHONE not in blob(event)


def test_a_failing_init_db_makes_one_event(
    job_env: FakeTransport, db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> None:
        raise CommandError("cannot upgrade")

    monkeypatch.setattr(command, "upgrade", broken)
    assert cli.main(["init-db", "--db", db_url]) == 1
    (event,) = job_env.events
    assert event["tags"]["job"] == "init-db"


def test_a_successful_job_sends_nothing(job_env: FakeTransport, db_url: str) -> None:
    assert cli.main(["init-db", "--db", db_url]) == 0
    assert cli.main(["purge-expired", "--db", db_url]) == 0
    assert job_env.envelopes == []


def test_jobs_without_a_dsn_send_nothing(
    transport: FakeTransport, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(alerting, "default_transport", lambda: transport)
    assert cli.main(["purge-expired", "--db", f"sqlite:///{tmp_path / 'e.db'}"]) == 1
    assert transport.envelopes == []
    assert not sentry_sdk.get_client().is_active()


def test_commands_other_than_jobs_do_not_start_alerting(
    job_env: FakeTransport, db_url: str
) -> None:
    assert cli.main(["add-agency", "bad slug!", "X", "--db", db_url]) == 2
    assert not sentry_sdk.get_client().is_active()
