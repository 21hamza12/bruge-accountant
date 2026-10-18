"""Parse targets for the sandbox tests.

They live in an importable module (not in a test file's closure) because a spawned child must
be able to unpickle them by name.
"""

from __future__ import annotations

import os
import time

from app.parsing.amadeus import ParsedReport, ParseError


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
