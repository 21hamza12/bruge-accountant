"""Typed path parameters shared by routes.

A database id is a BIGINT: 1 to 2**63 - 1. A bigger number would not be a "not found" for the
database, it would be an overflow error (a 500 on Postgres), so the range is checked before any
query and an out-of-range id gets the same 404 page as an id that does not exist
(``app.web.errors.validation_exception_handler``). A non-numeric id stays a 400.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated

from fastapi import HTTPException, Path, Query

MAX_DB_ID = 2**63 - 1

_DB_ID = re.compile(r"[0-9]{1,19}", re.ASCII)


def parse_db_id(raw: object) -> int | None:
    """A database id typed or posted as text: ASCII digits only (no ``²`` or full-width digits,
    which ``str.isdigit`` accepts and ``int`` refuses), 1 to ``MAX_DB_ID``. None otherwise."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if _DB_ID.fullmatch(text) is None:
        return None
    value = int(text)
    return value if 1 <= value <= MAX_DB_ID else None


DbId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]  # any BIGINT id in a path
UserId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]
SignId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]
UploadId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]
PartnerId = Annotated[int, Path(ge=1, le=MAX_DB_ID)]


DayParam = Annotated[str | None, Query(alias="date", max_length=32)]
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_day(raw: str | None) -> date | None:
    """``YYYY-MM-DD`` only (``date.fromisoformat`` also takes ``20261001`` and week dates).
    None for a missing or empty value; 400 for anything else."""
    if raw is None or raw == "":
        return None
    if not _ISO_DATE.fullmatch(raw):
        raise HTTPException(status_code=400)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=400) from None
