"""Upload an Amadeus daily report: the form, and ``POST /uploads``.

Any signed-in user (staff or manager) may upload. ``POST /uploads`` declares NO file or form
parameter: FastAPI parses a declared ``UploadFile`` before it runs the dependencies, which would
spool up to 10 MB to disk for an anonymous, CSRF-less or rate-limited request. The route reads the
multipart body itself, and only after the cheap gates, in this order:

1. Login (the session cookie): ``current_user``. An anonymous request is turned away (401 page,
   or the login redirect for a GET) before any body byte is read.
2. The Origin check (``verify_origin``), still without reading the body.
3. CSRF: the ``X-CSRF-Token`` header, when there is one, is checked now (``verify_token``). A plain
   HTML form cannot set a header, so without one the token is the hidden ``csrf_token`` field,
   checked as soon as the form is read and BEFORE the file is looked at (a wrong token gives the
   quota hit back).
4. The rate limits: 10 an hour per user, then 30 an hour per agency. The hits are counted and
   committed first. The body limit (``app.web.bodylimit``) ran before everything, so the form is
   never bigger than ``MAX_REQUEST_BYTES``.
5. ``await request.form(max_files=1, max_fields=5, max_part_size=...)``: one file, a few small
   fields. The page works in a plain browser without JavaScript.
6. The file bytes are read, at most ``MAX_FILE_BYTES + 1`` of them (one more byte than allowed
   proves the file is too big without reading the rest).
7. ``ingest_report`` with the agency taken from the session ONLY (a form field named
   ``agency_id`` is never read) and the current user as the uploader. The parse runs in the
   sandbox (``app.parsing.sandbox``).
8. A 303 to the result page.

The quota is given back when the failure is not the user's: the 503 (sandbox busy), a database
error, any unexpected error, and a wrong CSRF token. A bad file (not an xlsx, unreadable, too big)
still counts.

Every failure is audited (``upload_failed``) with the sanitized filename and a short reason, never
file contents. The user sees a French message; a parse error shows the parser's own safe message
(escaped by the template). A failure to write the success audit event is logged and does not turn
a committed upload into a 500.

Visibility (docs/phase2-plan.md, decision 5): managers see every upload of the agency with its
warnings (they quote cell values: ticket numbers, passenger name fragments). Staff see only the
uploads they made, with the counts and dates but never the warnings; a colleague's upload is a
404, like a missing one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.responses import RedirectResponse, Response

from app import audit
from app.auth import ratelimit
from app.clock import Clock
from app.ingest import (
    EmptyFileError,
    FileTooLargeError,
    IngestResult,
    NotAnXlsxError,
    ingest_report,
    sanitize_filename,
)
from app.models import ROLE_MANAGER
from app.parsing.amadeus import MAX_FILE_BYTES, ParsedReport, ParseError
from app.parsing.sandbox import SandboxBusy, parse_in_sandbox
from app.repository import get_upload, recent_uploads, upload_ticket_dates, user_names
from app.settings import Settings
from app.web.csrf import CSRF_FIELD, CSRF_HEADER, verify_origin, verify_token
from app.web.deps import AuthContext, client_ip, current_user, get_clock, get_session, get_settings
from app.web.pages import page
from app.web.params import UploadId
from app.web.shell import (
    ACTIVE_UPLOAD_NEW,
    ACTIVE_UPLOADS,
    shell_context,
)

log = logging.getLogger("app.web.uploads")

router = APIRouter(prefix="/uploads")

Parser = Callable[[bytes], ParsedReport]
BUSY_RETRY_SECONDS = "5"
MAX_WARNINGS_SHOWN = 50
RECENT_UPLOADS = 50
# The multipart form: the file and a handful of small fields (the token and whatever a browser or
# a script adds). A text field is cut off at 64 KB; the file itself is bounded by the body limit.
FORM_MAX_FILES = 1
FORM_MAX_FIELDS = 5
FORM_MAX_FIELD_BYTES = 64 * 1024

MSG_NO_FILE = "Choisissez un fichier .xlsx."
MSG_EMPTY = "Le fichier est vide."
MSG_NOT_XLSX = "Ce fichier n'est pas un classeur Excel (.xlsx)."
MSG_PARSE = "Le fichier n'a pas pu être lu : {reason}"


async def get_parser(settings: Annotated[Settings, Depends(get_settings)]) -> Parser:
    """The parser the upload uses: the sandbox. Tests replace it with a cheaper stand-in."""
    return partial(
        parse_in_sandbox,
        timeout_s=settings.parse_timeout_s,
        memory_mb=settings.parse_memory_mb,
        concurrency=settings.parse_concurrency,
        queue_wait_s=settings.parse_queue_wait_s,
    )


def _form(
    db: Session, auth: AuthContext, *, error: str | None = None, status_code: int = 200
) -> Response:
    return page(
        "uploads/new.html",
        status_code=status_code,
        error=error,
        **shell_context(db, auth, ACTIVE_UPLOAD_NEW),
    )


def _is_manager(auth: AuthContext) -> bool:
    return auth.user.role == ROLE_MANAGER


@router.get("/new")
def upload_form(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    return _form(db, auth)


@router.get("")
def upload_list(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    """Managers: every upload of the agency. Staff: only their own."""
    agency_id = auth.user.agency_id
    only_mine = None if _is_manager(auth) else auth.user.id
    uploads = recent_uploads(db, agency_id, RECENT_UPLOADS, uploaded_by=only_mine)
    names = user_names(db, agency_id, [u.uploaded_by_user_id for u in uploads])
    return page(
        "uploads/list.html",
        uploads=uploads,
        names=names,
        **shell_context(db, auth, ACTIVE_UPLOADS),
    )


@router.get("/{upload_id}")
def upload_result(
    upload_id: UploadId,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    agency_id = auth.user.agency_id
    manager = _is_manager(auth)
    upload = get_upload(db, agency_id, upload_id, uploaded_by=None if manager else auth.user.id)
    if upload is None:  # missing, another agency's and (for staff) a colleague's look the same
        raise HTTPException(status_code=404)
    names = user_names(db, agency_id, [upload.uploaded_by_user_id])
    # Warnings quote cell values (ticket numbers, passenger name fragments): managers only.
    warnings = list(upload.warnings) if manager else []
    ticket_dates = upload_ticket_dates(db, agency_id, upload.id)
    return page(
        "uploads/show.html",
        headers={"Cache-Control": "no-store"},
        upload=upload,
        uploader=names.get(upload.uploaded_by_user_id or 0),
        show_warnings=manager,
        warnings=warnings[:MAX_WARNINGS_SHOWN],
        more_warnings=max(0, len(warnings) - MAX_WARNINGS_SHOWN),
        ticket_dates=ticket_dates,
        dates_differ=any(day != upload.report_date for day, _ in ticket_dates),
        **shell_context(db, auth, ACTIVE_UPLOADS),
    )


# --- POST /uploads ------------------------------------------------------------------------------


@dataclass
class Quota:
    """The rate-limit hits this request has counted, so it can give them back."""

    hits: list[tuple[ratelimit.Limit, str]] = field(default_factory=list)


def _rollback_quietly(db: Session) -> None:
    try:
        db.rollback()
    except Exception:  # pragma: no cover - the connection is gone
        log.warning("rollback failed", exc_info=True)


def _refund(db: Session, clock: Clock, quota: Quota) -> None:
    """Give the counted hits back. Never raises: a failure here must not hide the real outcome."""
    try:
        now = clock()
        for limit, subject in quota.hits:
            ratelimit.refund(db, limit, subject, now)
        db.commit()
    except Exception:
        log.warning("could not refund the upload quota", exc_info=True)
        _rollback_quietly(db)
    quota.hits.clear()


def _failed(
    db: Session,
    request: Request,
    clock: Clock,
    auth: AuthContext,
    filename: str,
    reason: str,
) -> None:
    audit.record_and_commit(
        db,
        audit.UPLOAD_FAILED,
        agency_id=auth.user.agency_id,
        actor_user_id=auth.user.id,
        ip=client_ip(request),
        details={"filename": filename, "reason": reason},
        now=clock(),
    )


def _failed_quietly(
    db: Session, request: Request, clock: Clock, auth: AuthContext, name: str, reason: str
) -> None:
    """Audit a failure the user cannot fix; a failing audit must not replace the real answer."""
    try:
        _failed(db, request, clock, auth, name, reason)
    except Exception:
        log.error("upload_failed audit event could not be written", exc_info=True)
        _rollback_quietly(db)


def _too_many(
    db: Session,
    request: Request,
    auth: AuthContext,
    decision: ratelimit.RateDecision,
    clock: Clock,
    scope: str,
) -> HTTPException:
    """The 429. Only the first refusal of a window is a database row."""
    details = {"scope": scope}
    ip = client_ip(request)
    if decision.first_rejection:
        audit.record_and_commit(
            db,
            audit.RATE_LIMITED,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=ip,
            details=details,
            now=clock(),
        )
    else:
        audit.log_only(
            audit.RATE_LIMITED,
            agency_id=auth.user.agency_id,
            actor_user_id=auth.user.id,
            ip=ip,
            details=details,
        )
    return HTTPException(status_code=429, headers={"Retry-After": str(decision.retry_after)})


def _reserve(db: Session, request: Request, clock: Clock, auth: AuthContext) -> Quota:
    """Count this attempt against the user's budget, then the agency's; raise the 429 if either
    is spent. A user over their own budget never touches the agency's counter, and an agency
    refusal gives the user's hit back."""
    quota = Quota()
    now = clock()
    user_limit, agency_limit = ratelimit.UPLOAD_USER, ratelimit.UPLOAD_AGENCY
    user_subject, agency_subject = str(auth.user.id), str(auth.user.agency_id)

    decision = ratelimit.hit(db, user_limit, user_subject, now)
    db.commit()  # the count must survive whatever happens next
    quota.hits.append((user_limit, user_subject))
    if not decision.allowed:
        raise _too_many(db, request, auth, decision, clock, "upload_user")

    decision = ratelimit.hit(db, agency_limit, agency_subject, now)
    db.commit()
    quota.hits.append((agency_limit, agency_subject))
    if not decision.allowed:
        _refund(db, clock, Quota([(user_limit, user_subject)]))
        raise _too_many(db, request, auth, decision, clock, "upload_agency")
    return quota


def _store(
    db: Session,
    request: Request,
    clock: Clock,
    auth: AuthContext,
    parse: Parser,
    quota: Quota,
    upload: UploadFile | None,
) -> Response:
    """Everything after the body was read: validate, parse, store, audit. Runs in a thread."""
    agency_id = auth.user.agency_id  # from the session, never from the form
    name = sanitize_filename(upload.filename if upload is not None else None)
    data = upload.file.read(MAX_FILE_BYTES + 1) if upload is not None else b""

    def refuse(reason: str, message: str) -> Response:
        _failed(db, request, clock, auth, name, reason)
        return _form(db, auth, error=message, status_code=400)

    if upload is None or (not data and not upload.filename):
        return refuse("no_file", MSG_NO_FILE)
    result: IngestResult
    try:
        result = ingest_report(
            db, agency_id, data, upload.filename, parse=parse, uploaded_by=auth.user.id
        )
    except FileTooLargeError:
        _failed(db, request, clock, auth, name, "too_large")
        raise HTTPException(status_code=413) from None
    except EmptyFileError:
        return refuse("empty", MSG_EMPTY)
    except NotAnXlsxError:
        return refuse("not_xlsx", MSG_NOT_XLSX)
    except SandboxBusy:
        _refund(db, clock, quota)
        _failed_quietly(db, request, clock, auth, name, "busy")
        raise HTTPException(status_code=503, headers={"Retry-After": BUSY_RETRY_SECONDS}) from None
    except ParseError as exc:
        return refuse("parse_error", MSG_PARSE.format(reason=exc))
    except SQLAlchemyError:
        log.error("upload not stored: database error", exc_info=True)
        _rollback_quietly(db)
        _refund(db, clock, quota)
        _failed_quietly(db, request, clock, auth, name, "db_error")
        raise HTTPException(status_code=500) from None
    except Exception:
        _rollback_quietly(db)
        _refund(db, clock, quota)
        raise

    try:
        audit.record_and_commit(
            db,
            audit.UPLOAD_SUCCEEDED,
            agency_id=agency_id,
            actor_user_id=auth.user.id,
            ip=client_ip(request),
            details={
                "upload_id": result.upload_id,
                "filename": result.filename,
                "inserted": result.inserted,
                "updated": result.updated,
            },
            now=clock(),
        )
    except Exception:
        # The upload is committed: a lost audit row must not turn it into a 500.
        log.error(
            "upload audit event could not be written",
            exc_info=True,
            extra={"upload_id": result.upload_id, "agency_id": agency_id},
        )
        _rollback_quietly(db)
    log.info("upload stored", extra={"upload_id": result.upload_id, "agency_id": agency_id})
    return RedirectResponse(f"/uploads/{result.upload_id}", status_code=303)


@router.post("")
async def upload_submit(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    settings: Annotated[Settings, Depends(get_settings)],
    auth: Annotated[AuthContext, Depends(current_user)],
    parse: Annotated[Parser, Depends(get_parser)],
) -> Response:
    # 1. Login: ``current_user`` above. No body parameter is declared, so nothing has read the body.
    # 2. Origin.
    await verify_origin(request, db, settings, clock, auth)
    # 3. CSRF by header, when the client sent one (a script or htmx; a plain form cannot).
    header_token = request.headers.get(CSRF_HEADER)
    if header_token:
        await verify_token(request, db, clock, auth, header_token)
    # 4. Rate limits.
    quota = await run_in_threadpool(_reserve, db, request, clock, auth)
    # 5. Only now the body.
    async with request.form(
        max_files=FORM_MAX_FILES,
        max_fields=FORM_MAX_FIELDS,
        max_part_size=FORM_MAX_FIELD_BYTES,
    ) as form:
        if not header_token:
            token = form.get(CSRF_FIELD)
            try:
                await verify_token(
                    request, db, clock, auth, token if isinstance(token, str) else None
                )
            except HTTPException:
                await run_in_threadpool(_refund, db, clock, quota)
                raise
        file = form.get("file")
        upload = file if isinstance(file, UploadFile) else None
        return await run_in_threadpool(_store, db, request, clock, auth, parse, quota, upload)
