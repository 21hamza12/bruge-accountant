"""Step 25: the parse sandbox (spawned child, timeout, memory cap, crash handling, busy)."""

from __future__ import annotations

import json
import logging
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

from app.parsing import sandbox
from app.parsing.amadeus import ParsedReport, ParseError, parse_report
from app.parsing.sandbox import SandboxBusy, parse_in_sandbox
from app.settings import Settings
from tests import sandbox_targets

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
TIMEOUT = 60


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_the_fixture_parses_identically_in_the_sandbox_and_in_process() -> None:
    data = _fixture_bytes()
    assert parse_in_sandbox(data, timeout_s=TIMEOUT, memory_mb=1024) == parse_report(data)


def test_a_parse_error_comes_back_as_its_message() -> None:
    with pytest.raises(ParseError, match="not a valid .xlsx workbook"):
        parse_in_sandbox(b"PK\x03\x04 not really", timeout_s=TIMEOUT, memory_mb=1024)


def test_the_child_parse_error_message_is_kept() -> None:
    with pytest.raises(ParseError, match="the sheet has no header row"):
        parse_in_sandbox(
            b"x",
            timeout_s=TIMEOUT,
            memory_mb=1024,
            parse_target=sandbox_targets.raises_parse_error,
        )


def test_a_slow_child_is_killed_at_the_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(ParseError, match="took too long"):
        parse_in_sandbox(
            b"x", timeout_s=2, memory_mb=1024, parse_target=sandbox_targets.sleeps_forever
        )
    assert time.monotonic() - started < 60
    assert multiprocessing.active_children() == []  # nothing left running


def test_a_crashing_child_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="could not be read"):
        parse_in_sandbox(
            b"x", timeout_s=TIMEOUT, memory_mb=1024, parse_target=sandbox_targets.crashes
        )
    assert multiprocessing.active_children() == []


def test_an_unexpected_child_exception_never_leaks_its_text() -> None:
    with pytest.raises(ParseError) as caught:
        parse_in_sandbox(
            b"x",
            timeout_s=TIMEOUT,
            memory_mb=1024,
            parse_target=sandbox_targets.raises_value_error,
        )
    text = str(caught.value)
    assert "secret" not in text
    assert "Traceback" not in text
    assert "private.py" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS does not exist on Windows")
def test_the_memory_cap_turns_a_runaway_parse_into_a_parse_error() -> None:
    with pytest.raises(ParseError):
        parse_in_sandbox(
            b"x", timeout_s=TIMEOUT, memory_mb=512, parse_target=sandbox_targets.hogs_memory
        )
    assert multiprocessing.active_children() == []


def test_a_second_concurrent_parse_is_busy() -> None:
    assert sandbox._LOCK.acquire(blocking=False)
    try:
        with pytest.raises(SandboxBusy):
            parse_in_sandbox(_fixture_bytes(), timeout_s=TIMEOUT, memory_mb=1024)
    finally:
        sandbox._LOCK.release()


def test_the_lock_is_released_after_a_failure() -> None:
    with pytest.raises(ParseError):
        parse_in_sandbox(
            b"x", timeout_s=TIMEOUT, memory_mb=1024, parse_target=sandbox_targets.crashes
        )
    assert parse_in_sandbox(_fixture_bytes(), timeout_s=TIMEOUT, memory_mb=1024)


def test_busy_is_not_a_parse_error() -> None:
    assert not issubclass(SandboxBusy, ParseError)


def test_windows_logs_that_the_memory_cap_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox, "_HAS_RLIMIT", False)
    with caplog.at_level(logging.WARNING, logger="app.parsing.sandbox"):
        parse_in_sandbox(_fixture_bytes(), timeout_s=TIMEOUT, memory_mb=1024)
    assert any("memory cap" in r.getMessage() for r in caplog.records)


# --- the wire format (JSON, never pickle) --------------------------------------------------------


def test_the_wire_format_round_trips(parsed_fixture: ParsedReport) -> None:
    assert sandbox.decode_report(sandbox.encode_report(parsed_fixture)) == parsed_fixture


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"[]",
        b'{"header": {}}',
        b'{"status": "ok"}',
        b'{"status": "ok", "report": {"header": {"report_range_raw": "01OCT", '
        b'"report_date": "nope", "generated_on": "2026-10-02"}, "tickets": [], "warnings": []}}',
        b'{"status": "weird"}',
        b"\xff\xfe",
    ],
)
def test_a_malformed_child_message_is_a_parse_error_not_a_crash(payload: bytes) -> None:
    with pytest.raises(ParseError):
        sandbox.decode_message(payload)


def test_a_ticket_with_the_wrong_shape_is_refused(parsed_fixture: ParsedReport) -> None:
    message = json.loads(sandbox.encode_report(parsed_fixture))
    message["report"]["tickets"][0]["pax_type"] = "VIP"
    with pytest.raises(ParseError):
        sandbox.decode_message(json.dumps(message).encode())
    message = json.loads(sandbox.encode_report(parsed_fixture))
    message["report"]["tickets"][0]["cost"] = "12abc"
    with pytest.raises(ParseError):
        sandbox.decode_message(json.dumps(message).encode())


def test_run_target_in_process_covers_the_child_logic() -> None:
    ok = json.loads(sandbox.run_target(sandbox_targets.raises_parse_error, b"x"))
    assert ok == {"status": "error", "message": "the sheet has no header row"}
    generic = json.loads(sandbox.run_target(sandbox_targets.raises_value_error, b"x"))
    assert generic["status"] == "error"
    assert "secret" not in generic["message"]
    good = sandbox.decode_message(sandbox.run_target(parse_report, _fixture_bytes()))
    assert len(good.tickets) == 48


# --- settings -----------------------------------------------------------------------------------


def test_the_limits_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().parse_timeout_s == 30
    assert Settings().parse_memory_mb >= 256
    monkeypatch.setenv("PARSE_TIMEOUT_S", "5")
    monkeypatch.setenv("PARSE_MEMORY_MB", "300")
    settings = Settings()
    assert (settings.parse_timeout_s, settings.parse_memory_mb) == (5, 300)


def test_linux_limits_are_applied_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []

    class FakeResource:
        RLIMIT_AS = 9
        RLIMIT_CPU = 0

        @staticmethod
        def setrlimit(which: int, limits: tuple[int, int]) -> None:
            calls.append((which, limits))

    monkeypatch.setattr(sandbox, "_HAS_RLIMIT", True)
    monkeypatch.setattr(sandbox, "_resource", FakeResource)
    sandbox._apply_limits(256, 30)
    assert calls == [(9, (256 * 1024 * 1024, 256 * 1024 * 1024)), (0, (30, 31))]


def test_no_limits_are_applied_where_they_do_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "_HAS_RLIMIT", False)
    monkeypatch.setattr(sandbox, "_resource", None)
    sandbox._apply_limits(256, 30)  # must not raise
