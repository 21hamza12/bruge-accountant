"""Run the parser in a throwaway child process.

Why: a hostile or broken workbook must not be able to take the web process down (memory
exhaustion, an endless loop, a crash inside a C extension), and must not be able to read the web
process's secrets if the parser were ever exploited.

How it works
------------
* The child is ``python -I -c <bootstrap>`` started with :class:`subprocess.Popen` and a MINIMAL
  environment (``child_environment``): the interpreter's own needs (``PATH``, ``SYSTEMROOT`` on
  Windows, ``LANG`` on Linux) and nothing else. ``DATABASE_URL``, the owner database URL,
  ``SENTRY_DSN`` and every other variable of the web process are simply not there. ``-I`` makes
  the interpreter ignore ``PYTHON*`` variables, the user site directory and the current directory.
* The bytes go to the child's stdin, its answer comes back on stdout (``app.parsing.worker``).
  No pickling and no ``multiprocessing``: the answer is JSON (``app.parsing.wire``) that the parent
  validates field by field, with a size cap. A traceback or the text of an unexpected exception
  never crosses the boundary.
* ONE monotonic deadline (``timeout_s``) covers the whole child: a reader thread collects stdout
  and the parent waits for it with the time that is left. When the deadline passes, or the answer
  grows past ``MAX_MESSAGE_BYTES``, the child (and on Windows its whole process tree: the venv
  launcher spawns the real interpreter) is killed, which also ends a half-sent message. Nothing
  blocks forever on a partial answer.
* Linux: the child sets ``RLIMIT_AS`` (``memory_mb``), ``RLIMIT_CPU``, ``RLIMIT_NPROC`` (no new
  process) and ``RLIMIT_FSIZE`` (no file writes) before parsing. Windows has none of them, so the
  memory cap is skipped there and a warning says so (the timeout still holds).
* ``concurrency`` parses may run at once per process (``PARSE_CONCURRENCY``, default 2). A parse
  that finds every slot taken waits up to ``queue_wait_s`` seconds (``PARSE_QUEUE_WAIT_S``,
  default 5) and then raises :class:`SandboxBusy`, which the web layer turns into a friendly 503.
  The slot is always released. A queue can replace this later behind the same function.

Memory sizing: a child needs about 31 MB resident for the 48-ticket fixture and about 49 MB for a
20 000-row report (docs/phase2-plan.md), so ``PARSE_CONCURRENCY`` children take roughly
``PARSE_CONCURRENCY x 50 MB`` on top of the web process; the default 2 fits a 512 MB instance.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.parsing import wire
from app.parsing.amadeus import ParsedReport, ParseError, parse_report
from app.parsing.wire import (
    MSG_BAD_ANSWER,
    decode_message,
    decode_report,
    encode_report,
)
from app.settings import child_environment

__all__ = [
    "SandboxBusy",
    "child_environment",
    "decode_message",
    "decode_report",
    "encode_report",
    "parse_in_sandbox",
    "slots",
]

log = logging.getLogger("app.parsing.sandbox")

DEFAULT_CONCURRENCY = 2
DEFAULT_QUEUE_WAIT_S = 5.0
JOIN_GRACE_SECONDS = 2.0
KILL_WAIT_SECONDS = 5.0
READ_CHUNK = 1024 * 1024

MSG_TIMEOUT = "the file took too long to read"
MSG_CRASH = "the file could not be read"

_ROOT = str(Path(__file__).resolve().parents[2])  # the directory that holds the ``app`` package
_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from app.parsing.worker import main; sys.exit(main(sys.argv[1:]))"
)
_HAS_RLIMIT = sys.platform == "linux"
_DEFAULT_TARGET = parse_report

_SLOTS: dict[int, threading.BoundedSemaphore] = {}
_SLOTS_GUARD = threading.Lock()


class SandboxBusy(Exception):  # noqa: N818 - a condition, not a failure of the input
    """Every parse slot of this process stays taken for the whole queue wait; try again shortly."""


def slots(size: int) -> threading.BoundedSemaphore:
    """The semaphore that holds ``size`` parse slots (one per size, created on first use)."""
    if size < 1:
        raise ValueError("at least one parse slot is needed")
    with _SLOTS_GUARD:
        semaphore = _SLOTS.get(size)
        if semaphore is None:
            semaphore = _SLOTS[size] = threading.BoundedSemaphore(size)
        return semaphore


def _target_spec(target: Callable[[bytes], ParsedReport]) -> str:
    module = getattr(target, "__module__", None)
    name = getattr(target, "__qualname__", None)
    if not module or not name or "<" in name:
        raise ValueError("a parse target must be a module-level function")
    return f"{module}:{name}"


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the child. On Windows also its descendants: the venv launcher ``python.exe`` starts
    the real interpreter as a child, and killing only the launcher would leave that one running
    with the pipe open."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):  # best effort
            subprocess.run(  # noqa: S603 - fixed arguments, a pid we started
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],  # noqa: S607
                capture_output=True,
                timeout=10,
                check=False,
            )
    with contextlib.suppress(OSError):  # already gone
        proc.kill()


class _Reader(threading.Thread):
    """Collects the child's stdout, at most ``limit`` bytes (one more byte proves it is too big)."""

    def __init__(self, stream: object, limit: int) -> None:
        super().__init__(daemon=True, name="parse-reader")
        self._stream = stream
        self._limit = limit
        self.payload = bytearray()
        self.overflow = False

    def run(self) -> None:
        read = self._stream.read  # type: ignore[attr-defined]
        try:
            while True:
                chunk = read(READ_CHUNK)
                if not chunk:
                    return
                self.payload += chunk
                if len(self.payload) > self._limit:
                    self.overflow = True
                    return
        except (OSError, ValueError):  # the pipe broke or was closed under us
            return


def _feed(stream: object, data: bytes) -> None:
    """Send the workbook to the child's stdin (in a thread: a full pipe must not block the
    parent), then close it so the child sees the end of its input."""
    try:
        stream.write(data)  # type: ignore[attr-defined]
        stream.close()  # type: ignore[attr-defined]
    except (OSError, ValueError):  # the child died or was killed first
        pass


def parse_in_sandbox(
    data: bytes,
    *,
    timeout_s: int,
    memory_mb: int,
    parse_target: Callable[[bytes], ParsedReport] = _DEFAULT_TARGET,
    concurrency: int = DEFAULT_CONCURRENCY,
    queue_wait_s: float = DEFAULT_QUEUE_WAIT_S,
) -> ParsedReport:
    """Parse ``data`` in a child process; see the module docstring.

    ``parse_target`` exists for tests (a slow or crashing stand-in). It must be a module-level
    function so the child can import it by name; the web app never passes it.
    """
    semaphore = slots(concurrency)
    if not semaphore.acquire(timeout=max(0.0, queue_wait_s)):
        raise SandboxBusy
    try:
        if not _HAS_RLIMIT:
            log.warning("memory cap skipped: RLIMIT_AS is not available on this platform")
        return decode_message(_run_child(data, timeout_s, memory_mb, parse_target))
    finally:
        semaphore.release()


def _run_child(
    data: bytes,
    timeout_s: int,
    memory_mb: int,
    target: Callable[[bytes], ParsedReport],
) -> bytes:
    deadline = time.monotonic() + timeout_s
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        _BOOTSTRAP.format(root=_ROOT),
        str(memory_mb),
        str(timeout_s),
        _target_spec(target),
    ]
    # If this raises there is no child to join or kill, and the original error is what the
    # caller sees (the semaphore is released by the caller's ``finally``).
    proc = subprocess.Popen(  # noqa: S603 - our own interpreter and a fixed bootstrap
        command,
        env=child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    assert proc.stdin is not None and proc.stdout is not None  # noqa: S101 - PIPE was requested
    reader = _Reader(proc.stdout, wire.MAX_MESSAGE_BYTES)
    writer = threading.Thread(
        target=_feed, args=(proc.stdin, data), daemon=True, name="parse-writer"
    )
    try:
        reader.start()
        writer.start()
        reader.join(max(0.0, deadline - time.monotonic()))
        if reader.is_alive():
            log.warning("parse killed: no complete answer within %s s", timeout_s)
            raise ParseError(MSG_TIMEOUT)
        if reader.overflow:
            log.warning("parse killed: the answer is bigger than %s bytes", wire.MAX_MESSAGE_BYTES)
            raise ParseError(MSG_BAD_ANSWER)
        try:
            code = proc.wait(timeout=max(JOIN_GRACE_SECONDS, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log.warning("parse killed: the child did not exit after answering")
            raise ParseError(MSG_TIMEOUT) from None
        if code != 0:
            log.warning("parse child died without a clean exit (exit code %s)", code)
            raise ParseError(MSG_CRASH)
        return bytes(reader.payload)
    finally:
        if proc.poll() is None:
            _kill(proc)
        try:
            proc.wait(KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - the kernel did not reap it
            log.error("parse child %s did not die after a kill", proc.pid)
        for thread in (reader, writer):
            if thread.ident is not None:  # never started if the first start() failed
                thread.join(KILL_WAIT_SECONDS)
        if not reader.is_alive():
            proc.stdout.close()
