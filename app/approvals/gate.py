"""The partner list and the gate in front of closing a day.

**The partner list**: partner tickets that are not in the Amadeus report are issued in another
office, so they do not block; but somebody who may validate money must have seen them. The
acknowledgement carries a digest of the list as the user saw it (ticket, sale, partner, fee and
cost of each), so a changed list, or a changed price, must be seen again. Whoever wrote one of the
listed sales cannot acknowledge them.

**The gate** (``can_close``): a report was received and compared with the latest upload; no
blocking flag is open or answered; every fee override of the day is approved; the partner list was
seen. ``can_close`` runs the comparison again first when it may be stale (a write, in a savepoint);
``gate_status`` is the read-only preview for the day page and never runs it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, days, flags, permissions, sales
from app.approvals.common import (
    LIST_CHANGED,
    NO_LIST,
    NO_RUN,
    NO_VALIDATE_RIGHT,
    OWN_LIST,
    ApprovalError,
    ApprovalNotFound,
    CloseBlocked,
    refuse,
    rerun,
)
from app.approvals.overrides import count_unapproved
from app.clock import utc_now
from app.comparison import AppTicket
from app.errors import UserError
from app.models import (
    ACK_PARTNER_LIST,
    DAY_CLOSED,
    FLAG_ANSWERED,
    FLAG_OPEN,
    ComparisonFlag,
    DayAcknowledgement,
    ReportDay,
    SaleVersion,
    User,
)
from app.tenancy import bind_tenant

GATE_NO_REPORT = "Aucun rapport Amadeus n'a été reçu pour cette journée."
GATE_LIST = (
    "La liste des billets partenaires absents du rapport n'a pas été vue : "
    "cliquez sur « J'ai vu la liste »."
)


# --- the partner list ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PartnerList:
    """The partner tickets missing from the report, and the digest of what they show."""

    tickets: tuple[AppTicket, ...]
    digest: str


def _digest(rows: Sequence[tuple[str, ...]]) -> str:
    text = "\x1e".join("\x1f".join(row) for row in sorted(rows))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def partner_list(
    db: Session,
    actor: User,
    report_day: ReportDay,
    tickets: tuple[AppTicket, ...] | None = None,
) -> PartnerList:
    """The partner list of a day. ``tickets`` are the ones ``flags.day_summary`` already found
    (pass them to avoid comparing again). The digest covers, per ticket, its number, sale, partner,
    the fee applied and the cost."""
    if tickets is None:
        tickets = flags.day_summary(db, actor, report_day).partner_not_in_report
    views = {
        v.id: v for v in sales.agency_sales_by_id(db, actor.agency_id, {t.sale_id for t in tickets})
    }
    rows = []
    for t in tickets:
        view = views.get(t.sale_id)
        fee = "" if view is None or view.fee_applied is None else str(view.fee_applied.normalize())
        partner = "" if view is None else str(view.partner_id)
        rows.append((t.doc_number, str(t.sale_id), partner, fee, str(t.total_doc.normalize())))
    return PartnerList(tickets, _digest(rows))


def partner_digest(db: Session, actor: User, report_day: ReportDay) -> str:
    """The digest of the partner list as it is now (what a form carries to be compared later)."""
    return partner_list(db, actor, report_day).digest


def _seen(db: Session, report_day: ReportDay, digest: str) -> bool:
    return (
        db.scalars(
            select(DayAcknowledgement.id)
            .where(
                DayAcknowledgement.agency_id == report_day.agency_id,
                DayAcknowledgement.report_day_id == report_day.id,
                DayAcknowledgement.kind == ACK_PARTNER_LIST,
                DayAcknowledgement.digest == digest,
            )
            .limit(1)
        ).first()
        is not None
    )


def partner_list_seen(
    db: Session,
    actor: User,
    report_day: ReportDay,
    tickets: tuple[AppTicket, ...] | None = None,
) -> bool:
    """The partner tickets missing from the report were acknowledged (an empty list needs none).
    A different list, or a different price in it, must be seen again."""
    current = partner_list(db, actor, report_day, tickets)
    return not current.tickets or _seen(db, report_day, current.digest)


def acknowledge_partner_list(
    db: Session,
    validator: User,
    report_day: ReportDay,
    *,
    seen_digest: str,
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    """Record that the partner tickets not in the report were seen: the digest the user saw must
    still be the list's digest. Manager and accountant, never the author of a listed sale."""
    moment = now or utc_now()
    if not permissions.can_validate_money(validator):
        refuse(db, validator, "acknowledge_partner_list", NO_VALIDATE_RIGHT, now=moment)
    if report_day.agency_id != validator.agency_id:
        raise ApprovalNotFound
    bind_tenant(db, validator.agency_id)
    days.lock_day(db, validator, report_day)
    days.require_writable(report_day)
    run = flags.latest_run(db, validator, report_day)
    if run is None:
        raise ApprovalError(NO_RUN)
    current = partner_list(db, validator, report_day)
    if not current.tickets:
        raise ApprovalError(NO_LIST)
    if seen_digest != current.digest:
        raise ApprovalError(LIST_CHANGED)
    wrote = db.scalars(
        select(SaleVersion.id)
        .where(
            SaleVersion.agency_id == validator.agency_id,
            SaleVersion.sale_id.in_({t.sale_id for t in current.tickets}),
            SaleVersion.actor_user_id == validator.id,
        )
        .limit(1)
    ).first()
    if wrote is not None:
        refuse(db, validator, "acknowledge_partner_list_own", OWN_LIST, now=moment)
    db.add(
        DayAcknowledgement(
            agency_id=validator.agency_id,
            report_day_id=report_day.id,
            kind=ACK_PARTNER_LIST,
            run_id=run.id,
            digest=current.digest,
            actor_user_id=validator.id,
            created_at=moment,
        )
    )
    db.flush()
    audit.record(
        db,
        audit.PARTNER_LIST_ACKNOWLEDGED,
        agency_id=validator.agency_id,
        actor_user_id=validator.id,
        ip=ip,
        details={
            "report_day_id": report_day.id,
            "run_id": run.id,
            "tickets": len(current.tickets),
        },
        now=moment,
    )


# --- the gate -----------------------------------------------------------------------------------


def _count_flags(db: Session, report_day: ReportDay, status: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(ComparisonFlag)
            .where(
                ComparisonFlag.agency_id == report_day.agency_id,
                ComparisonFlag.report_day_id == report_day.id,
                ComparisonFlag.blocking.is_(True),
                ComparisonFlag.status == status,
            )
        )
        or 0
    )


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many.format(n=n)


def _gate_reasons(db: Session, report_day: ReportDay, listed: PartnerList) -> list[str]:
    """The reasons from the flags and sales as they are stored (no comparison is run here)."""
    reasons: list[str] = []
    open_n = _count_flags(db, report_day, FLAG_OPEN)
    if open_n:
        reasons.append(
            _plural(
                open_n,
                "1 écart bloquant reste à traiter.",
                "{n} écarts bloquants restent à traiter.",
            )
        )
    answered_n = _count_flags(db, report_day, FLAG_ANSWERED)
    if answered_n:
        reasons.append(
            _plural(
                answered_n,
                "1 réponse attend la validation du gérant ou du comptable.",
                "{n} réponses attendent la validation du gérant ou du comptable.",
            )
        )
    waiting = count_unapproved(db, report_day.agency_id, day_id=report_day.id)
    if waiting:
        reasons.append(
            _plural(
                waiting,
                "1 frais exceptionnel de partenaire attend la validation.",
                "{n} frais exceptionnels de partenaires attendent la validation.",
            )
        )
    if listed.tickets and not _seen(db, report_day, listed.digest):
        reasons.append(GATE_LIST)
    return reasons


@dataclass(frozen=True)
class GateStatus:
    """What the day page shows before anybody tries to close: the reasons as the stored flags and
    sales give them, whether the comparison would be run again at the closing (``stale``; only
    known when asked for), and the partner list the user is asked to have seen."""

    reasons: tuple[str, ...]
    stale: bool
    partner_tickets: tuple[AppTicket, ...] = ()
    partner_digest: str = ""


def gate_status(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    partner_tickets: tuple[AppTicket, ...] | None = None,
    check_stale: bool = False,
) -> GateStatus:
    """Read-only preview of the close gate (nothing is run or written). ``partner_tickets`` are
    those the caller's ``flags.day_summary`` found; ``check_stale`` also asks whether the flags
    still match the sales and the report, which compares everything once more: only for the
    confirmation page, not for every view of the day."""
    if report_day.agency_id != actor.agency_id:
        raise days.DayError(days.NOT_FOUND)
    if flags.latest_upload_for_day(db, actor.agency_id, report_day.day) is None:
        return GateStatus((GATE_NO_REPORT,), False)
    stale = check_stale and not flags.is_current(db, actor, report_day)
    listed = partner_list(db, actor, report_day, partner_tickets)
    return GateStatus(
        tuple(_gate_reasons(db, report_day, listed)), stale, listed.tickets, listed.digest
    )


def can_close(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> list[str]:
    """What stops the day from being closed, as French sentences (empty: it can be closed).

    Locks the day, and runs the comparison again first when the flags may be stale (a sale
    changed, a newer upload): that is a write, so the caller commits whatever the answer. If that
    run cannot be made (no lines, the days changed meanwhile) only its savepoint is rolled back
    and the error is the reason."""
    moment = now or utc_now()
    if report_day.agency_id != actor.agency_id:
        raise days.DayError(days.NOT_FOUND)
    bind_tenant(db, actor.agency_id)
    days.lock_day(db, actor, report_day)
    if report_day.status == DAY_CLOSED:
        return [days.CLOSED]
    if flags.latest_upload_for_day(db, actor.agency_id, report_day.day) is None:
        return [GATE_NO_REPORT]
    if not flags.is_current(db, actor, report_day):
        try:
            with db.begin_nested():
                rerun(db, actor, report_day, moment, ip)
        except UserError as exc:
            return [str(exc)]
    return _gate_reasons(db, report_day, partner_list(db, actor, report_day))


def require_can_close(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    """``can_close`` that raises :class:`CloseBlocked` (with the reasons) instead of returning
    them. The comparison may have been run again: commit before showing the error."""
    reasons = can_close(db, actor, report_day, now=now, ip=ip)
    if reasons:
        raise CloseBlocked(reasons)


def close_day(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> ReportDay:
    """Close the day when the gate allows it. A user who may not close gets the usual refusal from
    ``days.close_day``. The TJQ APRES (B10) is stored after this succeeds."""
    if permissions.can_close_day(actor):
        require_can_close(db, actor, report_day, now=now, ip=ip)
    return days.close_day(db, actor, report_day, now=now, ip=ip)


def to_answer_count(db: Session, actor: User) -> int:
    """Open blocking flags the actor may see (the sidebar badge)."""
    return flags.open_blocking_count(db, actor)
