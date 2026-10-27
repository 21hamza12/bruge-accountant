"""The partner statement download (phase F, slice F3): « Télécharger le relevé (Excel) ».

``GET /partner-accounts/{partner_id}/periods/{start}/statement.xlsx``: manager and accountant
only (``can_export_partner_statement``; staff get the 403 page and a ``permission_denied``
audit event). The agency is the session's: a partner of another agency, or a period that does
not exist, is a 404. Counted against ``PARTNER_STATEMENT_USER``; every download is a
``partner_statement_downloaded`` audit event (ids and dates only). The file name holds the
partner's id and the period's dates, never a name; ``Content-Disposition: attachment`` and
``Cache-Control: no-store``. The workbook is ``app/partner_statement.py`` (a closed period from
its frozen snapshot only, an open one marked « PROVISOIRE »).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from app import audit, partner_statement, permissions
from app.auth import ratelimit
from app.partner_book import AccountNotFound
from app.partner_statement import StatementError, StatementForbidden, StatementNotFound
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, require_permission
from app.web.pages import page
from app.web.params import PartnerId, parse_day
from app.web.sale_pages import Db, Now, throttle
from app.web.shell import ACTIVE_PARTNER_ACCOUNTS, shell_context

log = logging.getLogger(__name__)

router = APIRouter(prefix="/partner-accounts", dependencies=[Depends(csrf_protect)])

require_statement_reader = require_permission(permissions.can_export_partner_statement)
Reader = Annotated[AuthContext, Depends(require_statement_reader)]


@router.get("/{partner_id}/periods/{start}/statement.xlsx")
def statement_download(
    partner_id: PartnerId,
    start: str,
    request: Request,
    db: Db,
    clock: Now,
    auth: Reader,
) -> Response:
    throttle(db, request, auth, clock, write=False, limit=ratelimit.PARTNER_STATEMENT_USER)
    day = parse_day(start)
    if day is None:  # pragma: no cover - a path segment is never empty
        raise HTTPException(status_code=404)
    now = clock()
    try:
        statement = partner_statement.statement_for(
            db, auth.user, partner_id, day, today=now.date()
        )
        content = partner_statement.build_statement_xlsx(statement)
    except (AccountNotFound, StatementNotFound):
        raise HTTPException(status_code=404) from None
    except StatementForbidden:  # pragma: no cover - the dependency refused it already
        raise HTTPException(status_code=403) from None
    except StatementError as exc:
        # A frozen period that does not add up: a deliberate page, never a 500 and never a
        # wrong statement. The log names the ids only (no name, no amount).
        db.rollback()
        log.error(
            "partner statement refused: the frozen period does not add up",
            extra={"partner_id": partner_id, "period_id": exc.period_id},
        )
        return page(
            "partner_accounts/statement_error.html",
            status_code=409,
            headers={"Cache-Control": "no-store"},
            **shell_context(db, auth, ACTIVE_PARTNER_ACCOUNTS),
            partner_id=partner_id,
            error=str(exc),
        )
    audit.record(
        db,
        audit.PARTNER_STATEMENT_DOWNLOADED,
        agency_id=auth.user.agency_id,
        actor_user_id=auth.user.id,
        ip=client_ip(request),
        details={
            "partner_id": statement.partner_id,
            "period_start": statement.start.isoformat(),
            "period_id": statement.period_id,
            "provisional": statement.provisional,
        },
        now=now,
    )
    db.commit()
    name = partner_statement.statement_filename(statement)
    return Response(
        content,
        media_type=partner_statement.XLSX_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'attachment; filename="{name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["router"]
