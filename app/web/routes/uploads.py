"""Upload an Amadeus daily report: the form, and ``POST /uploads``.

Any signed-in user (staff or manager) may upload. ``POST /uploads`` does its work in a fixed order:

1. CSRF, a route dependency: the ``X-CSRF-Token`` header or the hidden ``csrf_token`` field, plus
   the Origin check. The request body limit (``app.web.bodylimit``) ran before anything, so the
   multipart form is never bigger than ``MAX_REQUEST_BYTES``. The form works in a plain browser
   without JavaScript; the file is spooled to a temporary file by the form parser.
2. The per-agency upload rate limit (30 an hour). The attempt is counted and committed first.
3. The file bytes are read, at most ``MAX_FILE_BYTES + 1`` of them (one more byte than allowed
   proves the file is too big without reading the rest).
4. ``ingest_report`` with the agency taken from the session ONLY (a form field named
   ``agency_id`` is not declared, so it is never read) and the current user as the uploader. The
   parse runs in the sandbox (``app.parsing.sandbox``).
5. A 303 to the result page.

Every failure is audited (``upload_failed``) with the sanitized filename and a short reason, never
file contents. The user sees a French message; a parse error shows the parser's own safe message
(escaped by the template).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from app import audit
from app.auth import ratelimit
from app.clock import Clock
from app.ingest import (
    EmptyFileError,
    FileTooLargeError,
    NotAnXlsxError,
    ingest_report,
    sanitize_filename,
)
from app.parsing.amadeus import MAX_FILE_BYTES, ParsedReport, ParseError
from app.parsing.sandbox import SandboxBusy, parse_in_sandbox
from app.repository import get_upload, recent_uploads, user_names
from app.settings import Settings
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, current_user, get_clock, get_session, get_settings
from app.web.pages import page
from app.web.params import UploadId

log = logging.getLogger("app.web.uploads")

router = APIRouter(prefix="/uploads", dependencies=[Depends(csrf_protect)])

Parser = Callable[[bytes], ParsedReport]
BUSY_RETRY_SECONDS = "5"
MAX_WARNINGS_SHOWN = 50
RECENT_UPLOADS = 50

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


def _form(auth: AuthContext, *, error: str | None = None, status_code: int = 200) -> Response:
    return page(
        "uploads/new.html", status_code=status_code, csrf_token=auth.csrf_token, error=error
    )


@router.get("/new")
def upload_form(auth: Annotated[AuthContext, Depends(current_user)]) -> Response:
    return _form(auth)


@router.get("")
def upload_list(
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    agency_id = auth.user.agency_id
    uploads = recent_uploads(db, agency_id, RECENT_UPLOADS)
    names = user_names(db, agency_id, [u.uploaded_by_user_id for u in uploads])
    return page("uploads/list.html", uploads=uploads, names=names, csrf_token=auth.csrf_token)


@router.get("/{upload_id}")
def upload_result(
    upload_id: UploadId,
    db: Annotated[Session, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(current_user)],
) -> Response:
    agency_id = auth.user.agency_id
    upload = get_upload(db, agency_id, upload_id)
    if upload is None:  # missing and "another agency's" look the same
        raise HTTPException(status_code=404)
    names = user_names(db, agency_id, [upload.uploaded_by_user_id])
    warnings = list(upload.warnings)
    return page(
        "uploads/show.html",
        headers={"Cache-Control": "no-store"},
        upload=upload,
        uploader=names.get(upload.uploaded_by_user_id or 0),
        warnings=warnings[:MAX_WARNINGS_SHOWN],
        more_warnings=max(0, len(warnings) - MAX_WARNINGS_SHOWN),
        csrf_token=auth.csrf_token,
    )


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


def _too_many(
    db: Session,
    request: Request,
    auth: AuthContext,
    now_decision: ratelimit.RateDecision,
    clock: Clock,
) -> HTTPException:
    """The 429. Only the first refusal of a window is a database row."""
    details = {"scope": "upload"}
    ip = client_ip(request)
    if now_decision.first_rejection:
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
    return HTTPException(status_code=429, headers={"Retry-After": str(now_decision.retry_after)})


@router.post("")
def upload_submit(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    clock: Annotated[Clock, Depends(get_clock)],
    auth: Annotated[AuthContext, Depends(current_user)],
    parse: Annotated[Parser, Depends(get_parser)],
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    agency_id = auth.user.agency_id  # from the session, never from the form
    now = clock()

    decision = ratelimit.hit(db, ratelimit.UPLOAD_AGENCY, str(agency_id), now)
    db.commit()  # the count must survive whatever happens next
    if not decision.allowed:
        raise _too_many(db, request, auth, decision, clock)

    name = sanitize_filename(file.filename if file is not None else None)
    data = file.file.read(MAX_FILE_BYTES + 1) if file is not None else b""

    def refuse(reason: str, message: str) -> Response:
        _failed(db, request, clock, auth, name, reason)
        return _form(auth, error=message, status_code=400)

    if file is None:
        return refuse("no_file", MSG_NO_FILE)
    if not data and not file.filename:
        return refuse("no_file", MSG_NO_FILE)
    try:
        result = ingest_report(
            db, agency_id, data, file.filename, parse=parse, uploaded_by=auth.user.id
        )
    except FileTooLargeError:
        _failed(db, request, clock, auth, name, "too_large")
        raise HTTPException(status_code=413) from None
    except EmptyFileError:
        return refuse("empty", MSG_EMPTY)
    except NotAnXlsxError:
        return refuse("not_xlsx", MSG_NOT_XLSX)
    except SandboxBusy:
        _failed(db, request, clock, auth, name, "busy")
        raise HTTPException(status_code=503, headers={"Retry-After": BUSY_RETRY_SECONDS}) from None
    except ParseError as exc:
        return refuse("parse_error", MSG_PARSE.format(reason=exc))

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
    log.info("upload stored", extra={"upload_id": result.upload_id, "agency_id": agency_id})
    return RedirectResponse(f"/uploads/{result.upload_id}", status_code=303)
