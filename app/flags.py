"""Run the comparison of a day with its Amadeus report and keep the flags (B8 wiring).

``app/comparison.py`` is the pure engine; this module feeds it from the database and stores what
comes out: one append-only ``comparison_runs`` row per run and the ``comparison_flags`` of the day.
Merging flags and settling the day's status is in ``app/flag_merge.py`` (read its doc: decisions are
kept only when the values AND the ticket facts are unchanged).

**A run** (``run_comparison(db, actor, report_day, ...)``; the actor is always a real user):

1. **Locks, in this order**: every day that could lose a sale to this run (earlier open days that
   own a ticket of the report) TOGETHER WITH the report day, ``FOR UPDATE``, in ascending id order
   (one ``ORDER BY id`` query); only then the sales, through the service. The report day is part of
   that ordering. The set is worked out before locking and checked again after; if it grew
   meanwhile the run stops with a "try again" message rather than lock out of order. A closed
   report day is refused.
2. takes the line set of the latest upload whose report date is the day
   (``repository.lines_upload_for``) and the active sales of the day, and asks ``compare``.
3. **Moves** (manager and accountant only: ``permissions.can_run_comparison``; a run by anybody
   else, such as a staff upload, only flags): a ticket of an EARLIER OPEN day that the report lists
   moves its whole sale to this day (``sales.move_sale_day(system=True)``, attributed to the actor)
   only when EVERY ticket of the sale is in this report and none is in the earlier day's own
   latest report. Otherwise nothing moves and the reported tickets get an ELSEWHERE flag naming the
   day they are on (a ticket of an earlier CLOSED day gets the same). After a move the day is
   gathered and compared again once, and that result gives the counts, the flags and the status;
   each earlier day that lost a sale is compared again too (its own run), so no stale flag stays.
4. stores the run (counts only), merges the flags, settles the day: ``approved`` only for a
   manager or accountant and only with no blocking flag open or answered, else ``compared``.
   A change of status is a ``day_events`` row; the audit trail has ``comparison_run``,
   ``comparison_flags_superseded`` (blocking flags replaced) and ``day_approved``.

A partner ticket missing from the report is not a flag (docs/design/10-ticket-screens.md section
7); the lists of cancelled lines and of those partner tickets are not stored: they are worked out
again from the report and the sales when a page asks (``day_summary``).

Functions never commit; the caller owns the transaction and rolls back on any error.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app import audit, days, permissions, repository, sales
from app.comparison import (
    KIND_ELSEWHERE,
    AmadeusTicket,
    AppTicket,
    ComparisonResult,
    Flag,
    amadeus_ticket_from_line,
    app_ticket_from_row,
    compare,
    normalise_doc,
)
from app.errors import UserError
from app.flag_merge import fingerprint, merge_flags, norm_sign, settle_day
from app.formatting import date_fr
from app.models import (
    DAY_APPROVED,
    DAY_CLOSED,
    DAY_COMPARED,
    FLAG_ANSWERED,
    FLAG_LIVE_STATUSES,
    FLAG_MOVED,
    FLAG_NOT_IN_AMADEUS,
    FLAG_OPEN,
    FLAG_STATUSES,
    AmadeusLine,
    ComparisonFlag,
    ComparisonRun,
    ReportDay,
    Upload,
    User,
)
from app.tenancy import bind_tenant

log = logging.getLogger("app.flags")

__all__ = [
    "Counts",
    "DaySummary",
    "FlagError",
    "day_summary",
    "flag_for",
    "flags_for_day",
    "flags_for_me",
    "is_current",
    "latest_run",
    "latest_upload_for_day",
    "open_blocking_count",
    "run_comparison",
    "run_for_upload",
]

MSG_NO_REPORT = "Aucun rapport Amadeus n'a été envoyé pour cette journée."
MSG_NO_LINES = "Le rapport de cette journée n'a pas de lignes enregistrées : renvoyez-le."
MSG_CHANGED = "Les journées ont changé pendant la comparaison : relancez-la."
MOVE_REASON = "Trouvé dans le rapport Amadeus du {day}"


class FlagError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


# --- what goes into the comparison --------------------------------------------------------------


@dataclass(frozen=True)
class _Inputs:
    upload_id: int  # the upload that holds the line set
    amadeus: list[AmadeusTicket]
    app: list[AppTicket]
    other: dict[str, AppTicket]
    # Tickets passed to ``compare`` as "not an earlier day" so they are flagged, not moved:
    # ticket number to the day the sale really is on.
    pinned: dict[str, date]
    move_day_ids: frozenset[int] = field(default_factory=frozenset)  # days that may lose a sale


def latest_upload_for_day(db: Session, agency_id: int, day: date) -> Upload | None:
    """The newest upload of the agency whose report date is ``day``."""
    return db.scalars(
        select(Upload)
        .where(Upload.agency_id == agency_id, Upload.report_date == day)
        .order_by(Upload.id.desc())
        .limit(1)
    ).first()


def _lines_of(db: Session, agency_id: int, upload_id: int) -> list[AmadeusLine]:
    return list(
        db.scalars(
            select(AmadeusLine)
            .where(AmadeusLine.agency_id == agency_id, AmadeusLine.upload_id == upload_id)
            .order_by(AmadeusLine.seq_order)
        )
    )


def _own_report_docs(db: Session, agency_id: int, day: date) -> set[str]:
    """The ticket numbers of the latest report of ``day`` itself (empty when it has none)."""
    upload = latest_upload_for_day(db, agency_id, day)
    lines_id = repository.lines_upload_for(db, agency_id, upload.id) if upload else None
    if lines_id is None:
        return set()
    return {normalise_doc(x.doc_number, x.airline) for x in _lines_of(db, agency_id, lines_id)}


def _other_days(
    db: Session,
    agency_id: int,
    report_day: ReportDay,
    wanted: Collection[str],
    report_docs: Collection[str],
    *,
    can_move: bool,
) -> tuple[dict[str, AppTicket], dict[str, date], frozenset[int]]:
    """Tickets of the agency's other days among ``wanted``, as ``compare`` wants them, the days
    they are pinned to, and the ids of the days that may lose a sale.

    A sale of an earlier, not closed day stays as it is (``compare`` turns its tickets into moves)
    only when ``can_move``, every ticket of the sale is in ``report_docs`` and none is in that
    earlier day's own latest report. Any other earlier ticket is pinned to the reported day so that
    ``compare`` flags it ELSEWHERE; ``pinned`` remembers the day it is really on."""
    found = sales.active_ticket_lookup(db, agency_id, wanted)
    found = {doc: ids for doc, ids in found.items() if ids[1] != report_day.id}
    if not found:
        return {}, {}, frozenset()
    views = sales.agency_sales_by_id(db, agency_id, {sale_id for sale_id, _ in found.values()})
    status_of = {
        row.id: (row.day, row.status)
        for row in db.execute(
            select(ReportDay.id, ReportDay.day, ReportDay.status).where(
                ReportDay.agency_id == agency_id,
                ReportDay.id.in_({day_id for _, day_id in found.values()}),
            )
        )
    }
    own_docs: dict[int, set[str]] = {}
    movable: dict[int, bool] = {}
    for view in views:
        real_day, status = status_of[view.report_day_id]
        ok = can_move and real_day < report_day.day and status != DAY_CLOSED
        if ok:
            docs = {t.doc_number for t in view.tickets}
            if view.report_day_id not in own_docs:
                own_docs[view.report_day_id] = _own_report_docs(db, agency_id, real_day)
            ok = docs <= set(report_docs) and not docs & own_docs[view.report_day_id]
        movable[view.id] = ok
    kind_of = {view.id: view.ag_kind for view in views}
    other: dict[str, AppTicket] = {}
    pinned: dict[str, date] = {}
    move_days: set[int] = set()
    for row in sales.ticket_rows(views):
        if row.doc_number not in found or found[row.doc_number][0] != row.sale_id:
            continue
        real_day, _ = status_of[row.report_day_id]
        shown = real_day
        if real_day < report_day.day:
            if movable[row.sale_id]:
                move_days.add(row.report_day_id)
            else:
                shown = report_day.day
                pinned[row.doc_number] = real_day
        other[row.doc_number] = app_ticket_from_row(
            row, ag_kind=kind_of[row.sale_id], report_day=shown
        )
    return other, pinned, frozenset(move_days)


def _gather(db: Session, report_day: ReportDay, *, can_move: bool) -> _Inputs:
    agency_id = report_day.agency_id
    upload = latest_upload_for_day(db, agency_id, report_day.day)
    if upload is None:
        raise FlagError(MSG_NO_REPORT)
    lines_id = repository.lines_upload_for(db, agency_id, upload.id)
    if lines_id is None:
        raise FlagError(MSG_NO_LINES)
    lines = _lines_of(db, agency_id, lines_id)
    views = sales.agency_sales_for_day(db, agency_id, report_day.day)
    kind_of = {view.id: view.ag_kind for view in views}
    app = [
        app_ticket_from_row(row, ag_kind=kind_of[row.sale_id], report_day=report_day.day)
        for row in sales.ticket_rows(views)
    ]
    mine = {ticket.doc_number for ticket in app}
    report_docs = {normalise_doc(line.doc_number, line.airline) for line in lines} - {""}
    other, pinned, move_ids = _other_days(
        db, agency_id, report_day, report_docs - mine, report_docs, can_move=can_move
    )
    return _Inputs(
        upload_id=lines_id,
        amadeus=[amadeus_ticket_from_line(line) for line in lines],
        app=app,
        other=other,
        pinned=pinned,
        move_day_ids=move_ids,
    )


def _compare(inputs: _Inputs, day: date) -> ComparisonResult:
    result = compare(inputs.app, inputs.amadeus, day=day, other_days_app=inputs.other)
    if not inputs.pinned:
        return result
    fixed = tuple(
        replace(flag, app_value=inputs.pinned[flag.doc_number].isoformat())
        if flag.kind == KIND_ELSEWHERE and flag.doc_number in inputs.pinned
        else flag
        for flag in result.flags
    )
    return replace(result, flags=fixed)


def _with_prints(inputs: _Inputs, flags: tuple[Flag, ...]) -> list[tuple[Flag, str]]:
    """Each flag with the fingerprint of the ticket facts on both sides."""
    app = {t.doc_number: t for t in inputs.app} | inputs.other
    amd = {normalise_doc(t.doc_number, t.airline): t for t in inputs.amadeus}
    return [(f, fingerprint(app.get(f.doc_number), amd.get(f.doc_number))) for f in flags]


# --- the run ------------------------------------------------------------------------------------


def _lock_days(db: Session, agency_id: int, day_ids: Collection[int]) -> None:
    """Lock these days ``FOR UPDATE`` in ascending id order (one query), refreshing the objects
    already in the session. The one place days are locked together."""
    db.execute(
        select(ReportDay)
        .where(ReportDay.agency_id == agency_id, ReportDay.id.in_(set(day_ids)))
        .order_by(ReportDay.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


def _move_tickets(
    db: Session,
    actor: User,
    report_day: ReportDay,
    result: ComparisonResult,
    *,
    now: datetime,
    ip: str | None,
) -> tuple[int, list[ReportDay]]:
    """Move the sales of the earlier open days whose tickets the report lists (``_other_days``
    left only movable sales as moves); their NOT_IN_AMADEUS flags become ``moved``. Returns the
    number of sales moved and the earlier days that lost one (already locked by the caller)."""
    reason = MOVE_REASON.format(day=date_fr(report_day.day))
    moved_sales: set[int] = set()
    earlier_days: dict[int, ReportDay] = {}
    for move in result.moves:
        earlier = days.get_day(db, report_day.agency_id, move.from_day)
        if earlier is None:  # cannot happen: the sale was on that day
            continue
        if move.sale_id not in moved_sales:
            sales.move_sale_day(
                db,
                actor,
                move.sale_id,
                report_day.day,
                reason,
                system=True,
                source="comparison",
                now=now,
                ip=ip,
            )
            moved_sales.add(move.sale_id)
        earlier_days[earlier.id] = earlier
        for row in db.scalars(
            select(ComparisonFlag).where(
                ComparisonFlag.agency_id == report_day.agency_id,
                ComparisonFlag.report_day_id == earlier.id,
                ComparisonFlag.doc_number == move.doc_number,
                ComparisonFlag.kind == FLAG_NOT_IN_AMADEUS,
                ComparisonFlag.status.in_(FLAG_LIVE_STATUSES),
            )
        ):
            row.status, row.updated_at = FLAG_MOVED, now
    db.flush()
    return len(moved_sales), [earlier_days[i] for i in sorted(earlier_days)]


def _store_run(
    db: Session,
    actor: User,
    day: ReportDay,
    inputs: _Inputs,
    result: ComparisonResult,
    moved: int,
    *,
    now: datetime,
    ip: str | None,
) -> ComparisonRun:
    """The run row, its flags, the day's status and the audit event."""
    run = ComparisonRun(
        agency_id=day.agency_id,
        report_day_id=day.id,
        upload_id=inputs.upload_id,
        started_by_user_id=actor.id,
        matched_count=len(result.matched),
        flag_count=len(result.flags),
        blocking_count=len(result.blocking_flags),
        cancelled_count=len(result.cancelled),
        move_count=moved,
        partner_missing_count=len(result.not_in_report_partner),
        created_at=now,
    )
    db.add(run)
    db.flush()
    merge_flags(db, actor, day, run, _with_prints(inputs, result.flags), now, ip)
    settle_day(
        db,
        actor,
        day,
        now,
        upload_id=inputs.upload_id,
        may_approve=permissions.can_run_comparison(actor),
        ip=ip,
    )
    audit.record(
        db,
        audit.COMPARISON_RUN,
        agency_id=day.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "run_id": run.id,
            "report_day_id": day.id,
            "upload_id": run.upload_id,
            "matched": run.matched_count,
            "flags": run.flag_count,
            "blocking": run.blocking_count,
            "cancelled": run.cancelled_count,
            "moves": run.move_count,
            "partner_not_in_report": run.partner_missing_count,
        },
        now=now,
    )
    return run


def _recompare_earlier(
    db: Session, actor: User, earlier: ReportDay, *, now: datetime, ip: str | None
) -> None:
    """An earlier day lost a sale: compare it again so no stale flag stays (a day that was never
    compared, or has no report, has no flags to fix)."""
    if earlier.status not in (DAY_COMPARED, DAY_APPROVED):
        return
    try:
        inputs = _gather(db, earlier, can_move=False)
    except FlagError:
        return
    _store_run(db, actor, earlier, inputs, _compare(inputs, earlier.day), 0, now=now, ip=ip)


def run_comparison(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime,
    ip: str | None = None,
) -> ComparisonRun:
    """Compare the day's sales with the day's Amadeus report and store the outcome (see the module
    doc). The caller commits, and rolls back on any error. Raises ``FlagError`` (no report, or the
    days changed meanwhile) or ``days.DayError`` (not the actor's agency's day, or a closed day)."""
    agency_id = report_day.agency_id
    if actor.agency_id != agency_id:
        raise days.DayError(days.NOT_FOUND)
    bind_tenant(db, agency_id)
    can_move = permissions.can_run_comparison(actor)
    peek = _gather(db, report_day, can_move=can_move)  # which days could lose a sale
    locked = {report_day.id} | set(peek.move_day_ids)
    _lock_days(db, agency_id, locked)  # all together, ascending id; then the sales
    days.require_writable(report_day)
    inputs = _gather(db, report_day, can_move=can_move)
    if not set(inputs.move_day_ids) <= locked:
        raise FlagError(MSG_CHANGED)
    result = _compare(inputs, report_day.day)
    earlier_days: list[ReportDay] = []
    moved = 0
    if result.moves:
        moved, earlier_days = _move_tickets(db, actor, report_day, result, now=now, ip=ip)
        inputs = _gather(db, report_day, can_move=can_move)  # the moved sales are ours now
        result = _compare(inputs, report_day.day)
    run = _store_run(db, actor, report_day, inputs, result, moved, now=now, ip=ip)
    for earlier in earlier_days:
        _recompare_earlier(db, actor, earlier, now=now, ip=ip)
    return run


def is_current(db: Session, actor: User, report_day: ReportDay) -> bool:
    """True when running the comparison again would change nothing: a run exists on the latest
    line set, no sale would move, and the flags it would find are exactly the live flags (same
    ticket, kind, field, both values and ticket facts). Written for the close gate (B9), which
    must not trust flags made before a sale was edited, voided or moved, or a newer upload. Reads
    only. A day without a report is not current."""
    run = latest_run(db, actor, report_day)
    upload = latest_upload_for_day(db, report_day.agency_id, report_day.day)
    if run is None or upload is None:
        return False
    if run.upload_id != repository.lines_upload_for(db, report_day.agency_id, upload.id):
        return False
    try:
        inputs = _gather(db, report_day, can_move=permissions.can_run_comparison(actor))
    except FlagError:
        return False
    result = _compare(inputs, report_day.day)
    if result.moves:
        return False
    fresh = {
        (f.doc_number, f.kind.lower(), f.field or "", f.app_value or "", f.amadeus_value or "", p)
        for f, p in _with_prints(inputs, result.flags)
    }
    live = {
        (
            r.doc_number,
            r.kind,
            r.field or "",
            r.app_value or "",
            r.amadeus_value or "",
            r.fingerprint,
        )
        for r in db.scalars(
            select(ComparisonFlag)
            .where(
                ComparisonFlag.agency_id == report_day.agency_id,
                ComparisonFlag.report_day_id == report_day.id,
                ComparisonFlag.status.in_(FLAG_LIVE_STATUSES),
            )
            .execution_options(populate_existing=True)
        )
    }
    return fresh == live


# --- reading ------------------------------------------------------------------------------------


def _visible(db: Session, actor: User) -> list[str] | None:
    """None: every sign (manager, accountant); else the actor's own signs, normalised (staff: their
    links; any other role: none). A flag with a blank sign is therefore for managers only."""
    signs = sales.visible_signs(db, actor)
    return None if signs is None else [norm_sign(s) for s in signs]


def _scoped(query: Select[ComparisonFlag], signs: list[str] | None) -> Select[ComparisonFlag]:
    if signs is None:
        return query
    return query.where(ComparisonFlag.responsible_sign.in_(signs))


def flags_for_day(
    db: Session, actor: User, day: ReportDay, *, history: bool = False
) -> list[ComparisonFlag]:
    """The live flags of a day (open, answered, resolved) that the actor may see, oldest first:
    everything for manager and accountant, only the actor's own signs for staff. ``history`` adds
    the superseded and moved ones. A day of another agency has none."""
    if day.agency_id != actor.agency_id:
        return []
    signs = _visible(db, actor)
    if signs is not None and not signs:
        return []
    statuses = FLAG_STATUSES if history else FLAG_LIVE_STATUSES
    query = select(ComparisonFlag).where(
        ComparisonFlag.agency_id == actor.agency_id,
        ComparisonFlag.report_day_id == day.id,
        ComparisonFlag.status.in_(statuses),
    )
    return list(db.scalars(_scoped(query, signs).order_by(ComparisonFlag.id)))


def flag_for(db: Session, actor: User, flag_id: int) -> ComparisonFlag | None:
    """A flag as it stands now (re-read from the database), when it is in the actor's agency and
    one the actor may see (a staff member's own signs); else None. B9 answers go through this."""
    row = db.scalars(
        select(ComparisonFlag)
        .where(ComparisonFlag.agency_id == actor.agency_id, ComparisonFlag.id == flag_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        return None
    signs = _visible(db, actor)
    if signs is not None and row.responsible_sign not in signs:
        return None
    return row


def flags_for_me(db: Session, actor: User) -> list[ComparisonFlag]:
    """The flags still waiting for somebody (open or answered), on any day, that the actor may
    see: a staff member's own signs, everything for manager and accountant."""
    signs = _visible(db, actor)
    if signs is not None and not signs:
        return []
    query = select(ComparisonFlag).where(
        ComparisonFlag.agency_id == actor.agency_id,
        ComparisonFlag.status.in_((FLAG_OPEN, FLAG_ANSWERED)),
    )
    return list(
        db.scalars(_scoped(query, signs).order_by(ComparisonFlag.report_day_id, ComparisonFlag.id))
    )


def open_blocking_count(db: Session, actor: User) -> int:
    """How many open blocking flags the actor may see (the sidebar badge)."""
    signs = _visible(db, actor)
    if signs is not None and not signs:
        return 0
    query = (
        select(func.count())
        .select_from(ComparisonFlag)
        .where(
            ComparisonFlag.agency_id == actor.agency_id,
            ComparisonFlag.status == FLAG_OPEN,
            ComparisonFlag.blocking.is_(True),
        )
    )
    if signs is not None:
        query = query.where(ComparisonFlag.responsible_sign.in_(signs))
    return db.scalar(query) or 0


def latest_run(db: Session, actor: User, day: ReportDay) -> ComparisonRun | None:
    """The newest run of a day of the actor's agency."""
    if day.agency_id != actor.agency_id:
        return None
    return db.scalars(
        select(ComparisonRun)
        .where(ComparisonRun.agency_id == actor.agency_id, ComparisonRun.report_day_id == day.id)
        .order_by(ComparisonRun.id.desc())
        .limit(1)
    ).first()


def run_for_upload(db: Session, agency_id: int, upload: Upload) -> ComparisonRun | None:
    """The newest run made on the line set of ``upload`` (for the upload's own report date)."""
    day = days.get_day(db, agency_id, upload.report_date)
    lines_id = repository.lines_upload_for(db, agency_id, upload.id)
    if day is None or lines_id is None:
        return None
    return db.scalars(
        select(ComparisonRun)
        .where(
            and_(
                ComparisonRun.agency_id == agency_id,
                ComparisonRun.report_day_id == day.id,
                ComparisonRun.upload_id == lines_id,
            )
        )
        .order_by(ComparisonRun.id.desc())
        .limit(1)
    ).first()


@dataclass(frozen=True)
class Counts:
    """The numbers a viewer may see. Manager and accountant get the run's (``matched`` and
    ``moves`` included); staff get only the size of what they can see (those two are None)."""

    matched: int | None
    flags: int
    blocking: int
    cancelled: int
    moves: int | None
    partner_not_in_report: int


@dataclass(frozen=True)
class DaySummary:
    """What a day page shows about the comparison, for one viewer. ``cancelled`` and
    ``partner_not_in_report`` are information, not flags; both are empty until a run exists."""

    run: ComparisonRun | None
    flags: tuple[ComparisonFlag, ...]
    cancelled: tuple[AmadeusTicket, ...]
    partner_not_in_report: tuple[AppTicket, ...]
    counts: Counts | None  # None until a run exists

    @property
    def open_blocking(self) -> int:
        return sum(1 for f in self.flags if f.blocking and f.status == FLAG_OPEN)


def _counts(
    run: ComparisonRun | None,
    seen_all: bool,
    visible: list[ComparisonFlag],
    cancelled: tuple[AmadeusTicket, ...],
    partner: tuple[AppTicket, ...],
) -> Counts | None:
    if run is None:
        return None
    if seen_all:
        return Counts(
            run.matched_count,
            run.flag_count,
            run.blocking_count,
            run.cancelled_count,
            run.move_count,
            run.partner_missing_count,
        )
    blocking = sum(1 for f in visible if f.blocking and f.status in (FLAG_OPEN, FLAG_ANSWERED))
    return Counts(None, len(visible), blocking, len(cancelled), None, len(partner))


def day_summary(db: Session, actor: User, day: ReportDay) -> DaySummary:
    """The comparison of a day as the actor may see it: the run, the live flags (a staff member
    sees only their own signs), the cancelled lines and the partner tickets missing from the report
    (worked out from the report and the sales as they are now; staff see their own signs only) and
    the counts the actor may see (``Counts``)."""
    run = latest_run(db, actor, day)
    visible = flags_for_day(db, actor, day)
    signs = _visible(db, actor)
    cancelled: tuple[AmadeusTicket, ...] = ()
    partner: tuple[AppTicket, ...] = ()
    if run is not None:
        try:
            inputs = _gather(db, day, can_move=False)
        except FlagError:
            inputs = None
        if inputs is not None:
            result = _compare(inputs, day.day)
            allowed = None if signs is None else set(signs)
            cancelled = tuple(
                t for t in result.cancelled if allowed is None or norm_sign(t.staff_sign) in allowed
            )
            partner = tuple(
                t
                for t in result.not_in_report_partner
                if allowed is None or norm_sign(t.staff_sign) in allowed
            )
    counts = _counts(run, signs is None, visible, cancelled, partner)
    return DaySummary(run, tuple(visible), cancelled, partner, counts)
