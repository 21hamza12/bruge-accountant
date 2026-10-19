"""Parse targets for the sandbox tests.

They live in an importable module (not in a test file's closure) because the sandbox child imports
them by name (``tests.sandbox_targets:<function>``).
"""

from __future__ import annotations

import os
import sys
import time

from app.parsing.amadeus import ParsedReport, ParseError, parse_report

MARKER = "s3cr3t-marker"


def sleeps_forever(data: bytes) -> ParsedReport:
    time.sleep(120)
    raise AssertionError("not reached")


def crashes(data: bytes) -> ParsedReport:
    os._exit(3)


def raises_value_error(data: bytes) -> ParsedReport:
    raise ValueError("secret internal detail /srv/app/private.py")


def raises_parse_error(data: bytes) -> ParsedReport:
    raise ParseError("the sheet has no header row")


def hogs_memory(data: bytes) -> ParsedReport:
    block = bytearray(3 * 1024 * 1024 * 1024)
    raise AssertionError(f"allocated {len(block)} bytes")


def partial_message_then_sleeps(data: bytes) -> ParsedReport:
    """Send the start of a message on the real answer channel, then hang without closing it."""
    out = sys.__stdout__.buffer  # type: ignore[union-attr]
    out.write(b'{"status": "ok", "report": {"header": {"report_range_ra')
    out.flush()
    time.sleep(120)
    raise AssertionError("not reached")


def reports_environment(data: bytes) -> ParsedReport:
    """Tell the parent which environment variables the child has (names only) and whether any
    value carries the marker the test planted in the parent's environment."""
    leaked = any(MARKER in value for value in os.environ.values())
    raise ParseError(f"leaked={leaked} names={','.join(sorted(os.environ))}")


def slow_then_parses(data: bytes) -> ParsedReport:
    time.sleep(3)
    return parse_report(data)


def prints_noise_then_parses(data: bytes) -> ParsedReport:
    print("stray output from a library")
    return parse_report(data)
