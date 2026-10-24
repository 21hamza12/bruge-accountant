"""The parallel-run comparison: ``GET|POST /days/{yyyy-mm-dd}/compare`` (phase C, doc 15).

For a CLOSED day with a stored TJQ APRES, a manager or accountant (``can_compare_apres``) uploads
the accountant's own APRES workbook; the page shows the rows only in hers, the rows only in the
app and every differing cell (``app.apres_compare``). The upload is read, compared and dropped:
**nothing is written but one audit event** (``apres_compared``: counts, never a name, a file name
or a cell value). The file is not kept in the database, and it is not written to disk either (the
multipart parser keeps it in memory, like the screenshot route).

Like ``POST /uploads`` the POST declares no body parameter and sits OUTSIDE the days router's
``csrf_protect`` (which would parse the body first). The body is read only after, in this order:

1. login and permission (the dependency): staff get a 403 and an audit event, nothing is read;
2. the Origin check;
3. the CSRF token when it came as the ``X-CSRF-Token`` header (a plain form cannot set one);
4. the day: a day that is not closed, has no stored export or belongs to another agency is a 404;
5. one hit on the hourly limit (``APRES_COMPARE_USER``), counted and committed.

Then the form is read (one file, a few small fields), the CSRF form field is checked when there was
no header (a wrong token keeps its hit: the body is already read), and the bytes go through the
same checks as an
Amadeus upload (``check_bytes``: empty, not a zip, over 10 MB) and the same sandbox
(``parse_in_sandbox`` with ``parse_apres``: size, zip-bomb and time limits, a child process with no
secrets). Cells of formulas are read as their stored value (never evaluated). The app's side is the
latest stored export of the day, parsed in-process (it is the app's own file). A sandbox that is
busy is a 503, and a server fault gives the hit back. Everything shown is escaped by the templates.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import aclosing
from datetime import date, datetime
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import Response

from app import apres_compare, audit, days, export_apres, permissions
from app.auth import ratelimit
from app.clock import Clock
from app.ingest import EmptyFileError, FileTooLargeError, NotAnXlsxError, check_bytes
from app.models import DAY_CLOSED
from app.parsing.amadeus import MAX_FILE_BYTES, ParsedReport, ParseError, parse_apres
from app.parsing.sandbox import SandboxBusy, parse_in_sandbox
from app.settings import Settings
from app.web.csrf import CSRF_FIELD, CSRF_HEADER, verify_origin, verify_token
from app.web.deps import (
    AuthContext,
    client_ip,
    get_clock,
    get_session,
    get_settings,
    require_permission,
)
from app.web.pages import page
from app.web.routes.days import (
    NO_STORE,
    NO_STORED_EXPORT,
    NOT_CLOSED_YET,
    _day_or_error,
    _error_page,
)
from app.web.sale_pages import throttle
from app.web.shell import ACTIVE_DAYS, shell_context

log = logging.getLogger("app.web.apres_compare")

router = APIRouter(prefix="/days")

Parser = Callable[[bytes], ParsedReport]
Db = Annotated[Session, Depends(get_session)]
Now = Annotated[Clock, Depends(get_clock)]
Comparer = Annotated[AuthContext, Depends(require_permission(permissions.can_compare_apres))]

FORM_MAX_FILES = 1
FORM_MAX_FIELDS = 5
FORM_MAX_FIELD_BYTES = 64 * 1024
BUSY_RETRY_SECONDS = "5"
MAX_WARNINGS_SHOWN = 10

MSG_NO_FILE = "Choisissez un fichier .xlsx."
MSG_EMPTY = "Le fichier est vide."
MSG_NOT_XLSX = "Ce fichier n'est pas un classeur Excel (.xlsx)."
MSG_PARSE = "Le fichier n'a pas pu être lu : {reason}"
MSG_STORED_UNREADABLE = (
    "Le TJQ APRES enregistré n'a pas pu être relu. Réessayez plus tard ou prévenez l'assistance."
)
MSG_UNREADABLE = "Le formulaire n'a pas pu être lu."


class _InMemoryParser(MultiPartParser):
    """The multipart parser with the file kept in memory, never in a temporary file. The body is
    capped by the request body limit (10 MB plus form overhead) before it gets here."""

    spool_max_size = MAX_FILE_BYTES + FORM_MAX_FIELD_BYTES


async def get_apres_parser(settings: Annotated[Settings, Depends(get_settings)]) -> Parser:
    """The parser the comparison uses: ``parse_apres`` in the sandbox. Tests replace it."""
    return partial(
        parse_in_sandbox,
        timeout_s=settings.parse_timeout_s,
        memory_mb=settings.parse_memory_mb,
        concurrency=settings.parse_concurrency,
        queue_wait_s=settings.parse_queue_wait_s,
        parse_target=parse_apres,
    )


def _locate(
    db: Session, auth: AuthContext, clock: Clock, raw_day: str
) -> tuple[date, int, int, datetime] | Response:
    """``(day, report day id, latest stored version, its time)`` of a closed day with a stored
    export, else the error page: 400 (not a usable date) or 404 (not closed, no export)."""
    chosen, problem = _day_or_error(raw_day, clock)
    if chosen is None:
        return _error_page(db, auth, problem)
    report_day = days.get_day(db, auth.user.agency_id, chosen)
    if report_day is None or report_day.status != DAY_CLOSED:
        return _error_page(db, auth, NOT_CLOSED_YET, 404)
    history = export_apres.export_history(db, auth.user.agency_id, report_day.id)
    if not history:
        return _error_page(db, auth, NO_STORED_EXPORT, 404)
    version, created_at = history[-1]
    return chosen, report_day.id, version, created_at


def _view(
    db: Session,
    auth: AuthContext,
    day: date,
    version: int,
    created_at: datetime,
    *,
    result: apres_compare.Comparison | None = None,
    error: str = "",
    sheet_day: date | None = None,
    warnings: tuple[str, ...] = (),
    status_code: int = 200,
) -> Response:
    return page(
        "days/compare.html",
        status_code=status_code,
        headers=NO_STORE,
        **shell_context(db, auth, ACTIVE_DAYS),
        day=day,
        version=version,
        created_at=created_at,
        result=result,
        error=error,
        sheet_day=sheet_day,
        warnings=warnings[:MAX_WARNINGS_SHOWN],
        more_warnings=max(0, len(warnings) - MAX_WARNINGS_SHOWN),
    )


@router.get("/{day}/compare")
def compare_form(day: str, db: Db, clock: Now, auth: Comparer) -> Response:
    found = _locate(db, auth, clock, day)
    if isinstance(found, Response):
        return found
    chosen, _, version, created_at = found
    return _view(db, auth, chosen, version, created_at)


async def _read_form(request: Request) -> FormData:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        # no file can come this way: Starlette's urlencoded parser, fields in memory and bounded
        return await request.form(max_fields=FORM_MAX_FIELDS, max_part_size=FORM_MAX_FIELD_BYTES)
    async with aclosing(request.stream()) as stream:
        parser = _InMemoryParser(
            request.headers,
            stream,
            max_files=FORM_MAX_FILES,
            max_fields=FORM_MAX_FIELDS,
            max_part_size=FORM_MAX_FIELD_BYTES,
        )
        return await parser.parse()


def _hit(db: Session, request: Request, auth: AuthContext, clock: Clock) -> None:
    throttle(db, request, auth, clock, write=False, limit=ratelimit.APRES_COMPARE_USER)


def _unhit(db: Session, auth: AuthContext, clock: Clock) -> None:
    """Give the hit back (a busy sandbox, a server fault). Never raises:
    it must not hide the real outcome."""
    try:
        ratelimit.refund(db, ratelimit.APRES_COMPARE_USER, str(auth.user.id), clock())
        db.commit()
    except Exception:
        log.warning("could not refund the comparison quota", exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - the connection is gone
            log.warning("rollback failed", exc_info=True)


def _compare(
    db: Session,
    request: Request,
    clock: Clock,
    auth: AuthContext,
    parse: Parser,
    located: tuple[date, int, int, datetime],
    upload: UploadFile | None,
) -> Response:
    """Everything after the body was read: validate, parse, compare, audit. Runs in a thread."""
    day, report_day_id, version, created_at = located

    def refuse(message: str, status_code: int = 400) -> Response:
        return _view(db, auth, day, version, created_at, error=message, status_code=status_code)

    data = upload.file.read(MAX_FILE_BYTES + 1) if upload is not None else b""
    if upload is None or (not data and not upload.filename):
        return refuse(MSG_NO_FILE)
    try:
        check_bytes(data)
        hers = parse(data)
    except FileTooLargeError:
        raise HTTPException(status_code=413) from None
    except EmptyFileError:
        return refuse(MSG_EMPTY)
    except NotAnXlsxError:
        return refuse(MSG_NOT_XLSX)
    except SandboxBusy:
        _unhit(db, auth, clock)
        raise HTTPException(status_code=503, headers={"Retry-After": BUSY_RETRY_SECONDS}) from None
    except ParseError as exc:
        return refuse(MSG_PARSE.format(reason=exc))
    except Exception:
        _unhit(db, auth, clock)
        raise
    stored = export_apres.get_export(db, auth.user.agency_id, report_day_id, version)
    if stored is None:  # pragma: no cover - the stored exports are never deleted
        raise HTTPException(status_code=404)
    try:
        ours = parse_apres(stored.content)
    except ParseError:  # the app's own file: a server fault, not the user's
        log.error("stored export could not be read back", exc_info=True)
        _unhit(db, auth, clock)
        return refuse(MSG_STORED_UNREADABLE, 503)
    result = apres_compare.compare(ours, hers)
    audit.record_and_commit(
        db,
        audit.APRES_COMPARED,
        agency_id=auth.user.agency_id,
        actor_user_id=auth.user.id,
        ip=client_ip(request),
        details={
            "report_day_id": report_day_id,
            "version": version,
            "matched": result.matched,
            "identical": result.identical,
            "differing_cells": result.differing_cells,
            "only_hers": result.only_hers_count,
            "only_app": result.only_app_count,
        },
        now=clock(),
    )
    sheet_day = hers.header.report_date if hers.header.report_date != day else None
    return _view(
        db, auth, day, version, created_at, result=result, sheet_day=sheet_day,
        warnings=hers.warnings,
    )  # fmt: skip


@router.post("/{day}/compare")
async def compare_submit(
    day: str,
    request: Request,
    db: Db,
    clock: Now,
    settings: Annotated[Settings, Depends(get_settings)],
    auth: Comparer,
    parse: Annotated[Parser, Depends(get_apres_parser)],
) -> Response:
    # 1. Login and permission: ``Comparer`` above. No body parameter is declared.
    # 2. Origin.
    await verify_origin(request, db, settings, clock, auth)
    # 3. CSRF by header, when the client sent one.
    header_token = request.headers.get(CSRF_HEADER)
    if header_token:
        await verify_token(request, db, clock, auth, header_token)
    # 4. The day.
    located = await run_in_threadpool(_locate, db, auth, clock, day)
    if isinstance(located, Response):
        return located
    # 5. The rate limit.
    await run_in_threadpool(_hit, db, request, auth, clock)
    # 6. Only now the body.
    form: FormData | None = None
    try:
        try:
            form = await _read_form(request)
        except MultiPartException:  # too many files, a field too big, a broken body
            return await run_in_threadpool(_refused_form, db, auth, located)
        if not header_token:
            token = form.get(CSRF_FIELD)
            # A wrong token is NOT refunded: the body is already read, and a refund would let one
            # user send unmetered 10 MB bodies.
            await verify_token(request, db, clock, auth, token if isinstance(token, str) else None)
        file = form.get("file")
        upload = file if isinstance(file, UploadFile) else None
        return await run_in_threadpool(_compare, db, request, clock, auth, parse, located, upload)
    finally:
        if form is not None:
            await form.close()


def _refused_form(
    db: Session, auth: AuthContext, located: tuple[date, int, int, datetime]
) -> Response:
    day, _, version, created_at = located
    return _view(db, auth, day, version, created_at, error=MSG_UNREADABLE, status_code=400)


__all__ = ["get_apres_parser", "router"]
