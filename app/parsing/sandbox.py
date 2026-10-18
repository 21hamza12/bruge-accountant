"""Run the parser in a throwaway child process.

Why: a hostile or broken workbook must not be able to take the web process down (memory
exhaustion, an endless loop, a crash inside a C extension). The child is a fresh ``spawn``ed
interpreter that does one thing: ``parse_report``. The parent waits for the answer for at most
``timeout_s`` seconds, then kills the child whatever it is doing.

* Linux: before parsing, the child sets ``RLIMIT_AS`` (``memory_mb``) and ``RLIMIT_CPU``. Windows
  has neither, so the memory cap is skipped there and a warning says so (the timeout still holds).
* What comes back is a JSON message (``encode_report``), never a pickle: the child has just read
  untrusted input, and unpickling data from a compromised child would run code in the parent. The
  parent rebuilds the dataclasses from validated fields and refuses anything of the wrong shape.
* Only a ``ParsedReport`` or a short ``ParseError`` message crosses the boundary. A traceback, or
  the text of an unexpected exception, never does.
* One parse at a time per process (a ``BoundedSemaphore(1)``): a second concurrent call raises
  :class:`SandboxBusy` at once, which the web layer turns into a friendly 503. A queue can replace
  this later behind the same function.
* The parent reads the pipe BEFORE joining the child: a big result could otherwise fill the pipe
  and block the child forever while the parent waits for it to exit.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import sys
import threading
from collections.abc import Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from multiprocessing.connection import Connection
from typing import Any

from app.parsing.amadeus import (
    ParsedReport,
    ParsedTicket,
    ParseError,
    ReportHeader,
    parse_report,
)

_resource: Any = None  # the ``resource`` module on POSIX; typed Any so mypy is platform-neutral
try:
    import resource

    _resource = resource
except ImportError:  # pragma: no cover - Windows
    pass

log = logging.getLogger("app.parsing.sandbox")

_HAS_RLIMIT = sys.platform == "linux" and _resource is not None
_LOCK = threading.BoundedSemaphore(1)
MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # a 50 000-row report encodes to far less
MAX_ERROR_CHARS = 300
JOIN_GRACE_SECONDS = 2
KILL_WAIT_SECONDS = 5

MSG_TIMEOUT = "the file took too long to read"
MSG_CRASH = "the file could not be read"
MSG_MEMORY = "the file needs too much memory to read"
MSG_BAD_ANSWER = "the file could not be read"

_PAX_TYPES = frozenset({"ADT", "CHD", "INF"})


class SandboxBusy(Exception):  # noqa: N818 - a condition, not a failure of the input
    """Another parse is running in this process; try again shortly."""


# --- the wire format ----------------------------------------------------------------------------


def encode_report(report: ParsedReport) -> bytes:
    header = report.header
    message = {
        "status": "ok",
        "report": {
            "header": {
                "report_range_raw": header.report_range_raw,
                "report_date": header.report_date.isoformat(),
                "generated_on": header.generated_on.isoformat(),
            },
            "tickets": [
                {
                    "seq_no": t.seq_no,
                    "airline": t.airline,
                    "ticket_no": t.ticket_no,
                    "cost": None if t.cost is None else str(t.cost),
                    "passenger_name": t.passenger_name,
                    "pax_type": t.pax_type,
                    "staff_sign": t.staff_sign,
                    "transaction_type": t.transaction_type,
                    "booking_ref": t.booking_ref,
                    "flags": list(t.flags),
                    "source_row": t.source_row,
                }
                for t in report.tickets
            ],
            "warnings": list(report.warnings),
        },
    }
    return json.dumps(message, ensure_ascii=False).encode("utf-8", "surrogatepass")


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value


def _ticket(raw: object) -> ParsedTicket:
    if not isinstance(raw, dict):
        raise ValueError("ticket is not an object")
    cost_raw = raw["cost"]
    cost = None if cost_raw is None else Decimal(_str(cost_raw))
    if cost is not None and not cost.is_finite():
        raise ValueError("cost is not finite")
    pax_type = _str(raw["pax_type"])
    if pax_type not in _PAX_TYPES:
        raise ValueError("unknown passenger type")
    flags = raw["flags"]
    source_row = raw["source_row"]
    if (
        not isinstance(flags, list)
        or isinstance(source_row, bool)
        or not isinstance(source_row, int)
    ):
        raise ValueError("bad flags or row")
    return ParsedTicket(
        seq_no=_str(raw["seq_no"]),
        airline=_str(raw["airline"]),
        ticket_no=_str(raw["ticket_no"]),
        cost=cost,
        passenger_name=_str(raw["passenger_name"]),
        pax_type=pax_type,  # type: ignore[arg-type]  # checked against _PAX_TYPES above
        staff_sign=_str(raw["staff_sign"]),
        transaction_type=_str(raw["transaction_type"]),
        booking_ref=_str(raw["booking_ref"]),
        flags=tuple(_str(f) for f in flags),
        source_row=source_row,
    )


def _report(raw: object) -> ParsedReport:
    if not isinstance(raw, dict):
        raise ValueError("report is not an object")
    header = raw["header"]
    tickets, warnings = raw["tickets"], raw["warnings"]
    if not isinstance(header, dict) or not isinstance(tickets, list):
        raise ValueError("bad report")
    if not isinstance(warnings, list):
        raise ValueError("bad warnings")
    return ParsedReport(
        header=ReportHeader(
            report_range_raw=_str(header["report_range_raw"]),
            report_date=date.fromisoformat(_str(header["report_date"])),
            generated_on=date.fromisoformat(_str(header["generated_on"])),
        ),
        tickets=tuple(_ticket(t) for t in tickets),
        warnings=tuple(_str(w) for w in warnings),
    )


def decode_message(payload: bytes) -> ParsedReport:
    """The report in a child's message, or the ``ParseError`` it reported. Anything malformed is
    a ``ParseError`` too (never an unexpected exception type)."""
    try:
        message: Any = json.loads(payload.decode("utf-8", "surrogatepass"))
        if not isinstance(message, dict):
            raise ValueError("message is not an object")
        status = message["status"]
        if status == "ok":
            return _report(message["report"])
        if status != "error":
            raise ValueError("unknown status")
        text = _str(message["message"])[:MAX_ERROR_CHARS]
    except (ValueError, KeyError, TypeError, InvalidOperation, UnicodeError, RecursionError):
        raise ParseError(MSG_BAD_ANSWER) from None
    raise ParseError(text)


def decode_report(payload: bytes) -> ParsedReport:
    return decode_message(payload)


# --- the child ----------------------------------------------------------------------------------


def run_target(target: Callable[[bytes], ParsedReport], data: bytes) -> bytes:
    """Run the parser and encode its answer. Never raises: the child reports, it does not crash."""
    try:
        return encode_report(target(data))
    except ParseError as exc:
        text = str(exc)[:MAX_ERROR_CHARS]
    except MemoryError:
        text = MSG_MEMORY
    except Exception:
        text = MSG_CRASH  # the exception's own text could hold file contents or paths
    return json.dumps({"status": "error", "message": text}).encode("utf-8")


def _apply_limits(memory_mb: int, cpu_seconds: int) -> None:
    if not _HAS_RLIMIT:
        return
    try:
        cap = memory_mb * 1024 * 1024
        _resource.setrlimit(_resource.RLIMIT_AS, (cap, cap))
        _resource.setrlimit(_resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (ValueError, OSError):  # pragma: no cover - a platform without these limits
        pass


def _child_main(
    conn: Connection,
    target: Callable[[bytes], ParsedReport],
    data: bytes,
    memory_mb: int,
    cpu_seconds: int,
) -> None:
    _apply_limits(memory_mb, cpu_seconds)
    conn.send_bytes(run_target(target, data))
    conn.close()


# --- the parent ---------------------------------------------------------------------------------


def parse_in_sandbox(
    data: bytes,
    *,
    timeout_s: int,
    memory_mb: int,
    parse_target: Callable[[bytes], ParsedReport] = parse_report,
) -> ParsedReport:
    """Parse ``data`` in a child process; see the module docstring.

    ``parse_target`` exists for tests (a slow or crashing stand-in). It must be a module-level
    function so the spawned child can import it; the web app never passes it.
    """
    if not _LOCK.acquire(blocking=False):
        raise SandboxBusy
    try:
        if not _HAS_RLIMIT:
            log.warning("memory cap skipped: RLIMIT_AS is not available on this platform")
        return decode_message(_run_child(data, timeout_s, memory_mb, parse_target))
    finally:
        _LOCK.release()


def _run_child(
    data: bytes,
    timeout_s: int,
    memory_mb: int,
    target: Callable[[bytes], ParsedReport],
) -> bytes:
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child_main, args=(sender, target, data, memory_mb, timeout_s), daemon=True
    )
    try:
        proc.start()
        sender.close()  # the parent's copy: without this a dead child would never read as EOF
        if not receiver.poll(timeout_s):
            log.warning("parse killed: no answer within %s s", timeout_s)
            raise ParseError(MSG_TIMEOUT)
        try:
            return receiver.recv_bytes(MAX_MESSAGE_BYTES)
        except (EOFError, OSError):
            log.warning("parse child died without an answer (exit code %s)", proc.exitcode)
            raise ParseError(MSG_CRASH) from None
    finally:
        sender.close()
        receiver.close()
        proc.join(JOIN_GRACE_SECONDS)
        if proc.is_alive():
            proc.kill()
            proc.join(KILL_WAIT_SECONDS)
