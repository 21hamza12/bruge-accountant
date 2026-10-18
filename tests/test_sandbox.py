"""Step 25: the parse sandbox (child process, timeout, memory cap, crash handling, slots, env)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.parsing import sandbox, wire, worker
from app.parsing.amadeus import ParsedReport, ParseError, parse_report
from app.parsing.sandbox import SandboxBusy, parse_in_sandbox
from app.settings import Settings
from tests import sandbox_targets

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
TIMEOUT = 60


def _fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _parse(target: Any = parse_report, **kwargs: Any) -> ParsedReport:
    options: dict[str, Any] = {"timeout_s": TIMEOUT, "memory_mb": 1024, "queue_wait_s": 0}
    options.update(kwargs)
    return parse_in_sandbox(_fixture_bytes(), parse_target=target, **options)


def test_the_fixture_parses_identically_in_the_sandbox_and_in_process() -> None:
    assert _parse() == parse_report(_fixture_bytes())


def test_a_parse_error_comes_back_as_its_message() -> None:
    with pytest.raises(ParseError, match="not a valid .xlsx workbook"):
        parse_in_sandbox(b"PK\x03\x04 not really", timeout_s=TIMEOUT, memory_mb=1024)


def test_the_child_parse_error_message_is_kept() -> None:
    with pytest.raises(ParseError, match="the sheet has no header row"):
        _parse(sandbox_targets.raises_parse_error)


def test_stray_output_in_the_child_cannot_corrupt_the_answer() -> None:
    assert _parse(sandbox_targets.prints_noise_then_parses) == parse_report(_fixture_bytes())


def test_a_slow_child_is_killed_at_the_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(ParseError, match="took too long"):
        _parse(sandbox_targets.sleeps_forever, timeout_s=3)
    assert time.monotonic() - started < 45


def test_a_crashing_child_is_a_parse_error() -> None:
    with pytest.raises(ParseError, match="could not be read"):
        _parse(sandbox_targets.crashes)


def test_an_unexpected_child_exception_never_leaks_its_text() -> None:
    with pytest.raises(ParseError) as caught:
        _parse(sandbox_targets.raises_value_error)
    text = str(caught.value)
    assert "secret" not in text
    assert "Traceback" not in text
    assert "private.py" not in text


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS does not exist on Windows")
def test_the_memory_cap_turns_a_runaway_parse_into_a_parse_error() -> None:
    with pytest.raises(ParseError):
        _parse(sandbox_targets.hogs_memory, memory_mb=512)


def test_an_answer_bigger_than_the_cap_kills_the_child_and_is_a_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wire, "MAX_MESSAGE_BYTES", 2000)
    with pytest.raises(ParseError, match="could not be read"):
        _parse()


# --- the partial-message wedge ------------------------------------------------------------------


def test_a_partial_message_followed_by_silence_is_cut_at_the_deadline_and_frees_the_slot() -> None:
    started = time.monotonic()
    with pytest.raises(ParseError, match="took too long"):
        _parse(sandbox_targets.partial_message_then_sleeps, timeout_s=5, concurrency=7)
    elapsed = time.monotonic() - started
    assert 4.5 <= elapsed < 40  # near the deadline, not at the target's 120 s sleep
    assert sandbox.slots(7).acquire(blocking=False)  # the slot is free again
    sandbox.slots(7).release()


def test_the_slot_is_released_after_every_kind_of_failure() -> None:
    for target in (
        sandbox_targets.crashes,
        sandbox_targets.raises_parse_error,
        sandbox_targets.raises_value_error,
    ):
        with pytest.raises(ParseError):
            _parse(target, concurrency=6)
    assert _parse(concurrency=6) == parse_report(_fixture_bytes())


def test_a_child_that_cannot_start_leaves_the_original_error_and_frees_the_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        raise OSError("cannot fork: out of processes")

    monkeypatch.setattr("app.parsing.sandbox.subprocess.Popen", refuse)
    with pytest.raises(OSError, match="out of processes"):
        _parse(concurrency=5)
    monkeypatch.undo()
    assert sandbox.slots(5).acquire(blocking=False)
    sandbox.slots(5).release()


# --- the environment ----------------------------------------------------------------------------


def test_the_child_does_not_inherit_the_web_processs_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secrets = {
        "DATABASE_URL": f"postgresql://u:{sandbox_targets.MARKER}@db/x",
        "MIGRATION_DATABASE_URL": f"postgresql://owner:{sandbox_targets.MARKER}@db/x",
        "SENTRY_DSN": f"https://{sandbox_targets.MARKER}@sentry.example/1",
        "TEST_DATABASE_URL": f"postgresql://t:{sandbox_targets.MARKER}@db/test",
        "TEST_APP_DATABASE_URL": f"postgresql://t:{sandbox_targets.MARKER}@db/test",
        "SOME_API_KEY": sandbox_targets.MARKER,
        "PYTHONPATH": "/somewhere/else",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ParseError) as caught:
        _parse(sandbox_targets.reports_environment)
    text = str(caught.value)
    assert "leaked=False" in text
    names = text.split("names=", 1)[1].split(",")
    for key in secrets:
        assert key not in names
    assert not [n for n in names if n.startswith("TEST_")]


def test_the_child_environment_is_a_small_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("PYTHONPATH", "/x")
    env = sandbox.child_environment()
    assert "DATABASE_URL" not in env
    assert "PYTHONPATH" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    allowed = {"PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8"} | (
        {"SYSTEMROOT", "SYSTEMDRIVE", "PATH", "PATHEXT", "COMSPEC"}
        if sys.platform == "win32"
        else {"PATH", "LANG", "LC_ALL"}
    )
    assert set(env) <= allowed


def test_a_target_must_be_a_module_level_function() -> None:
    with pytest.raises(ValueError, match="module-level"):
        sandbox._target_spec(lambda data: parse_report(data))


# --- the slots ----------------------------------------------------------------------------------


def test_slots_with_no_free_slot_are_busy() -> None:
    semaphore = sandbox.slots(1)
    assert semaphore.acquire(blocking=False)
    try:
        with pytest.raises(SandboxBusy):
            _parse(concurrency=1)
    finally:
        semaphore.release()


def test_a_slot_count_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        sandbox.slots(0)


def _taken(size: int) -> int:
    value: int = sandbox.slots(size)._value
    return size - value


def _wait_until(condition: Any, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.05)


class _Run(threading.Thread):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(daemon=True)
        self.kwargs = kwargs
        self.result: ParsedReport | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            self.result = _parse(sandbox_targets.slow_then_parses, **self.kwargs)
        except BaseException as exc:
            self.error = exc


def test_two_real_overlapping_parses_with_one_slot_the_second_is_busy() -> None:
    first = _Run(concurrency=1)
    first.start()
    _wait_until(lambda: _taken(1) == 1)
    with pytest.raises(SandboxBusy):
        _parse(concurrency=1)
    first.join(TIMEOUT)
    assert first.error is None
    assert first.result == parse_report(_fixture_bytes())
    assert _taken(1) == 0
    assert _parse(concurrency=1) == parse_report(_fixture_bytes())


def test_with_two_slots_a_third_parse_waits_for_a_slot_and_then_succeeds() -> None:
    a, b = _Run(concurrency=2), _Run(concurrency=2)
    a.start()
    b.start()
    _wait_until(lambda: _taken(2) == 2)
    with pytest.raises(SandboxBusy):  # a fourth caller that will not wait is turned away
        _parse(concurrency=2)
    third = _Run(concurrency=2, queue_wait_s=TIMEOUT)
    third.start()
    for run in (a, b, third):
        run.join(TIMEOUT * 2)
        assert run.error is None
    assert third.result == parse_report(_fixture_bytes())
    assert _taken(2) == 0


def test_a_waiting_parse_gives_up_with_busy_after_the_queue_wait() -> None:
    first = _Run(concurrency=3)
    first.start()
    second = _Run(concurrency=3)
    second.start()
    third = _Run(concurrency=3)
    third.start()
    _wait_until(lambda: _taken(3) == 3)
    started = time.monotonic()
    with pytest.raises(SandboxBusy):
        _parse(concurrency=3, queue_wait_s=1)
    assert 0.9 <= time.monotonic() - started < 2.9
    for run in (first, second, third):
        run.join(TIMEOUT)
        assert run.error is None


def test_busy_is_not_a_parse_error() -> None:
    assert not issubclass(SandboxBusy, ParseError)


def test_windows_logs_that_the_memory_cap_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(sandbox, "_HAS_RLIMIT", False)
    with caplog.at_level(logging.WARNING, logger="app.parsing.sandbox"):
        _parse()
    assert any("memory cap" in r.getMessage() for r in caplog.records)


# --- the wire format round trip and the child's own logic ---------------------------------------


def test_the_wire_format_round_trips(parsed_fixture: ParsedReport) -> None:
    assert sandbox.decode_report(sandbox.encode_report(parsed_fixture)) == parsed_fixture


def test_run_target_in_process_covers_the_child_logic() -> None:
    ok = json.loads(worker.run_target(sandbox_targets.raises_parse_error, b"x"))
    assert ok == {"status": "error", "message": "the sheet has no header row"}
    generic = json.loads(worker.run_target(sandbox_targets.raises_value_error, b"x"))
    assert generic["status"] == "error"
    assert "secret" not in generic["message"]
    good = sandbox.decode_message(worker.run_target(parse_report, _fixture_bytes()))
    assert len(good.tickets) == 48


def test_a_bad_target_spec_is_refused_by_the_child() -> None:
    for spec in ("not a spec", "x:y;z", "", "a.b", ":f", "m:"):
        with pytest.raises(ValueError, match="bad target"):
            worker.load_target(spec)


# --- settings and limits ------------------------------------------------------------------------


def test_the_limits_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().parse_timeout_s == 30
    assert Settings().parse_memory_mb >= 256
    assert Settings().parse_concurrency == 2
    assert Settings().parse_queue_wait_s == 5
    monkeypatch.setenv("PARSE_TIMEOUT_S", "5")
    monkeypatch.setenv("PARSE_MEMORY_MB", "300")
    monkeypatch.setenv("PARSE_CONCURRENCY", "3")
    monkeypatch.setenv("PARSE_QUEUE_WAIT_S", "0")
    settings = Settings()
    assert (settings.parse_timeout_s, settings.parse_memory_mb) == (5, 300)
    assert (settings.parse_concurrency, settings.parse_queue_wait_s) == (3, 0)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PARSE_TIMEOUT_S", "4"),
        ("PARSE_TIMEOUT_S", "0"),
        ("PARSE_CONCURRENCY", "0"),
        ("PARSE_CONCURRENCY", "100"),
        ("PARSE_QUEUE_WAIT_S", "-1"),
        ("PARSE_QUEUE_WAIT_S", "61"),
    ],
)
def test_out_of_range_sandbox_settings_are_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name.lower()):
        Settings()


class FakeResource:
    RLIMIT_AS = 9
    RLIMIT_CPU = 0
    RLIMIT_NPROC = 6
    RLIMIT_FSIZE = 1
    RLIMIT_CORE = 4

    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[int, int]]] = []

    def setrlimit(self, which: int, limits: tuple[int, int]) -> None:
        self.calls.append((which, limits))


def test_linux_limits_cover_memory_cpu_processes_and_file_writes() -> None:
    fake = FakeResource()
    applied = worker.apply_limits(256, 30, fake)
    assert applied == [
        "RLIMIT_AS",
        "RLIMIT_CPU",
        "RLIMIT_NPROC",
        "RLIMIT_FSIZE",
        "RLIMIT_CORE",
    ]
    assert fake.calls == [
        (9, (256 * 1024 * 1024, 256 * 1024 * 1024)),
        (0, (30, 31)),
        (6, (0, 0)),
        (1, (0, 0)),
        (4, (0, 0)),
    ]


def test_no_limits_are_applied_where_they_do_not_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "HAS_RLIMIT", False)
    monkeypatch.setattr(worker, "_resource", None)
    assert worker.apply_limits(256, 30) == []  # must not raise
