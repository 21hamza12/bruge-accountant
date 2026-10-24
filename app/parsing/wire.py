"""The JSON message a parse child sends back, and the parent's strict reader of it.

The child has just read an untrusted workbook, so everything it says is untrusted too. The parent
rebuilds the dataclasses from validated fields and answers anything else with a ``ParseError``:

* types are exact (a ``bool`` is not a row number, a cost is a decimal STRING, not a float);
* values are bounded: string lengths follow the column sizes (``FIELD_LIMITS``), a cost is
  below 10^12 with at most two decimals, a source row is 1..10^7, at most 50 000 tickets and 1 000
  warnings, flags come from a fixed list;
* no string holds a control, format, surrogate, private-use or unassigned character;
* the message is at most ``MAX_MESSAGE_BYTES`` and is never unpickled (the format is JSON).

Only the parent imports :func:`decode_message`; the child imports :func:`encode_report`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from decimal import Decimal
from typing import Any, NoReturn

from app.parsing.amadeus import (
    FIELD_LIMITS,
    KNOWN_FLAGS,
    MANUAL_LIMITS,
    MAX_NAME_CHARS,
    ParsedReport,
    ParsedTicket,
    ParseError,
    ReportHeader,
    clean_text,
)

MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # a 50 000-row report encodes to far less
MAX_ERROR_CHARS = 300
MAX_TICKETS = 50_000
MAX_WARNING_COUNT = 1_000
MAX_WARNING_CHARS = 500
MAX_TICKET_NO_CHARS = 32
MAX_RANGE_CHARS = 32
MAX_SOURCE_ROW = 10_000_000
MAX_FLAGS = 16

MSG_BAD_ANSWER = "the file could not be read"

_PAX_TYPES = frozenset({"ADT", "CHD", "INF"})
_COST_RE = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,2})?", re.ASCII)
_TICKET_KEYS = frozenset(
    {
        "seq_no",
        "airline",
        "ticket_no",
        "cost",
        "passenger_name",
        "pax_type",
        "staff_sign",
        "transaction_type",
        "booking_ref",
        "flags",
        "source_row",
        "tax",
        "comm",
        "form_of_payment",
        "confirmed",
        "pb",
        "ag",
        "clt",
        "ap",
    }
)


class _Invalid(ValueError):
    """A message field that is not acceptable (internal; becomes a ``ParseError``)."""


# No version field: the parent and the sandboxed child always ship in the same release, so the two
# sides of this message never differ.
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
                    "tax": None if t.tax is None else str(t.tax),
                    "comm": None if t.comm is None else str(t.comm),
                    "form_of_payment": t.form_of_payment,
                    "confirmed": t.confirmed,
                    "pb": None if t.pb is None else str(t.pb),
                    "ag": t.ag,
                    "clt": t.clt,
                    "ap": t.ap,
                }
                for t in report.tickets
            ],
            "warnings": list(report.warnings),
        },
    }
    return json.dumps(message, ensure_ascii=False).encode("utf-8", "surrogatepass")


def encode_error(text: str) -> bytes:
    return json.dumps({"status": "error", "message": text[:MAX_ERROR_CHARS]}).encode("utf-8")


# --- field validators ---------------------------------------------------------------------------


def _has_control(text: str) -> bool:
    if text.isprintable():
        return False
    return any(unicodedata.category(ch)[0] == "C" or ch in "  " for ch in text)


def _str(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        raise _Invalid("expected a string")
    if len(value) > max_chars:
        raise _Invalid("string too long")
    if _has_control(value):
        raise _Invalid("control character")
    return value


def _int_in(value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid("expected an integer")
    if not low <= value <= high:
        raise _Invalid("integer out of range")
    return value


def _cost(value: object) -> Decimal | None:
    if value is None:
        return None
    text = _str(value, 32)
    if not _COST_RE.fullmatch(text):
        raise _Invalid("cost is not a plain decimal below 10^12")
    return Decimal(text)


def _opt_str(value: object, max_chars: int) -> str | None:
    return None if value is None else _str(value, max_chars)


def _list(value: object, max_items: int) -> list[Any]:
    if not isinstance(value, list):
        raise _Invalid("expected a list")
    if len(value) > max_items:
        raise _Invalid("list too long")
    return value


def _ticket(raw: object) -> ParsedTicket:
    if not isinstance(raw, dict) or set(raw) != _TICKET_KEYS:
        raise _Invalid("ticket has the wrong shape")
    pax_type = _str(raw["pax_type"], 3)
    if pax_type not in _PAX_TYPES:
        raise _Invalid("unknown passenger type")
    flags = tuple(_str(f, 32) for f in _list(raw["flags"], MAX_FLAGS))
    if not set(flags) <= KNOWN_FLAGS:
        raise _Invalid("unknown flag")
    ticket_no = _str(raw["ticket_no"], MAX_TICKET_NO_CHARS)
    if not ticket_no:
        raise _Invalid("empty ticket number")
    return ParsedTicket(
        seq_no=_str(raw["seq_no"], FIELD_LIMITS["seq_no"]),
        airline=_str(raw["airline"], FIELD_LIMITS["airline"]),
        ticket_no=ticket_no,
        cost=_cost(raw["cost"]),
        passenger_name=_str(raw["passenger_name"], MAX_NAME_CHARS),
        pax_type=pax_type,  # type: ignore[arg-type]  # checked against _PAX_TYPES above
        staff_sign=_str(raw["staff_sign"], FIELD_LIMITS["staff_sign"]),
        # Upper case, as the parser writes it: the cancelled-line filters compare exactly.
        transaction_type=_str(raw["transaction_type"], FIELD_LIMITS["transaction_type"]).upper(),
        booking_ref=_str(raw["booking_ref"], FIELD_LIMITS["booking_ref"]),
        flags=flags,
        source_row=_int_in(raw["source_row"], 1, MAX_SOURCE_ROW),
        tax=_cost(raw["tax"]),
        comm=_cost(raw["comm"]),
        form_of_payment=_opt_str(raw["form_of_payment"], FIELD_LIMITS["form_of_payment"]),
        confirmed=_opt_str(raw["confirmed"], FIELD_LIMITS["confirmed"]),
        pb=_cost(raw["pb"]),
        ag=_opt_str(raw["ag"], MANUAL_LIMITS["ag"]),
        clt=_opt_str(raw["clt"], MANUAL_LIMITS["clt"]),
        ap=_opt_str(raw["ap"], MANUAL_LIMITS["ap"]),
    )


def _report(raw: object) -> ParsedReport:
    if not isinstance(raw, dict):
        raise _Invalid("report is not an object")
    header = raw["header"]
    if not isinstance(header, dict):
        raise _Invalid("bad header")
    tickets = _list(raw["tickets"], MAX_TICKETS)
    warnings = _list(raw["warnings"], MAX_WARNING_COUNT)
    parsed_tickets = tuple(_ticket(t) for t in tickets)
    # The parser keeps the first line of a repeated DOC NUMBER; the stored snapshot relies on it.
    if len({t.ticket_no for t in parsed_tickets}) != len(parsed_tickets):
        raise _Invalid("duplicate ticket number")
    return ParsedReport(
        header=ReportHeader(
            report_range_raw=_str(header["report_range_raw"], MAX_RANGE_CHARS),
            report_date=date.fromisoformat(_str(header["report_date"], 10)),
            generated_on=date.fromisoformat(_str(header["generated_on"], 10)),
        ),
        tickets=parsed_tickets,
        warnings=tuple(_str(w, MAX_WARNING_CHARS) for w in warnings),
    )


def _reject_constant(name: str) -> NoReturn:
    raise _Invalid(f"{name} is not allowed")


def decode_message(payload: bytes) -> ParsedReport:
    """The report in a child's message, or the ``ParseError`` it reported. Anything malformed,
    out of range or oversized is a ``ParseError`` too (never an unexpected exception type)."""
    try:
        if len(payload) > MAX_MESSAGE_BYTES:
            raise _Invalid("message too large")
        message: Any = json.loads(
            payload.decode("utf-8", "surrogatepass"), parse_constant=_reject_constant
        )
        if not isinstance(message, dict):
            raise _Invalid("message is not an object")
        status = message["status"]
        if status == "ok":
            return _report(message["report"])
        if status != "error":
            raise _Invalid("unknown status")
        text = message["message"]
        if not isinstance(text, str):
            raise _Invalid("expected a string")
        text = clean_text(text[:MAX_ERROR_CHARS])  # display text: sanitized, never rejected
    except (ValueError, KeyError, TypeError, ArithmeticError, UnicodeError, RecursionError):
        raise ParseError(MSG_BAD_ANSWER) from None
    raise ParseError(text)


def decode_report(payload: bytes) -> ParsedReport:
    return decode_message(payload)
