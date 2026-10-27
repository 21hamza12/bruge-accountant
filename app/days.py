"""Report days: one per agency and date, with a status and an append-only list of its changes.

``open`` -> ``compared`` -> ``approved`` -> ``closed``. B4 only needs ``open`` and ``closed``
(and reopening); the comparison (B8) and the approvals (B9) move a day through the other two.
The CHECK on ``report_days.status`` already allows all four.

* A day is opened the first time somebody writes to it (``get_or_open_day``), by anyone of the
  agency, never for a date after today. The day boundary is the application clock's UTC date
  (Nouakchott is UTC+0).
* ``close_day``: manager or accountant (``permissions.can_close_day``). In B4 closing needs no
  other condition; B9 adds "no open flag" and the stored TJQ APRES.
* ``reopen_day``: manager only (``can_reopen_day``), with a reason of at least 5 characters. The
  day goes back to ``compared`` when an Amadeus upload had been compared (``compared_upload_id``
  is set), else to ``open``. Nothing sets ``compared_upload_id`` before B8, so in B4 a reopened
  day is always ``open``.
* Every change writes a ``day_events`` row (who, when, from, to, reason) and an audit event
  (``day_opened``, ``day_closed``, ``day_reopened``) holding ids and the date only.
* A closed day refuses every write on its sales (``require_writable``). Closing and reopening lock
  the day row (``FOR UPDATE``), sale writers lock it ``FOR SHARE`` (both no-ops on SQLite), so a
  sale cannot be saved while the day is being closed and writers do not wait for each other.

* Dates: ``check_day`` is the one place that says whether a date may be written: a real date (not
  a datetime), not after today, not more than 5 years back.
* **Lock order, everywhere: day first, then sale.** A sale writer locks its day ``FOR SHARE``,
  then the sale row ``FOR UPDATE``; a writer that touches two days locks them in id order. The
  closing/comparison/approval steps of later phases lock a day ``FOR UPDATE`` and only then touch
  its sales. Taking the locks in any other order could deadlock.
* A refused close or reopen is recorded as a ``permission_denied`` audit event (action only).

Every function takes the agency from the acting user. A day of another agency is "not found".
Mistakes the user can correct are :class:`DayError` (a ``UserError``) with a French message.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, permissions
from app.accounts import is_unique_violation
from app.clock import utc_now
from app.errors import UserError
from app.models import DAY_CLOSED, DAY_COMPARED, DAY_OPEN, DayEvent, ReportDay, User
from app.reasons import ReasonError, required_reason

MAX_PAST_DAYS = 5 * 366

NOT_FOUND = "Journée introuvable."
BAD_DAY = "Date invalide."
FUTURE_DAY = "Cette date est dans le futur."
TOO_OLD = "Cette date est trop ancienne."
CLOSED = "Cette journée est clôturée. Demandez au gérant de la rouvrir."
NOT_CLOSED = "Cette journée n'est pas clôturée."
CLOSE_FORBIDDEN = "Seul le gérant ou le comptable peut clôturer la journée."
REOPEN_FORBIDDEN = "Seul le gérant peut rouvrir une journée clôturée."


class DayError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


class DayClosed(DayError):
    """The day is closed: nothing may be written on it until the manager reopens it."""

    def __init__(self, message: str = CLOSED) -> None:
        super().__init__(message)


# --- reading ------------------------------------------------------------------------------------


def get_day(db: Session, agency_id: int, day: date) -> ReportDay | None:
    """The agency's report day for a date, or None when nobody has written to it yet."""
    return db.scalars(
        select(ReportDay).where(ReportDay.agency_id == agency_id, ReportDay.day == day)
    ).one_or_none()


def day_events(db: Session, agency_id: int, report_day_id: int) -> list[DayEvent]:
    """The changes of one day, oldest first."""
    return list(
        db.scalars(
            select(DayEvent)
            .where(DayEvent.agency_id == agency_id, DayEvent.report_day_id == report_day_id)
            .order_by(DayEvent.id)
        )
    )


def check_day(day: date, today: date) -> None:
    """Raise DayError unless ``day`` is a date no later than ``today`` and at most 5 years back."""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise DayError(BAD_DAY)
    if day > today:
        raise DayError(FUTURE_DAY)
    if day < today - timedelta(days=MAX_PAST_DAYS):
        raise DayError(TOO_OLD)


def require_writable(report_day: ReportDay) -> None:
    """Raise :class:`DayClosed` for a closed day."""
    if report_day.status == DAY_CLOSED:
        raise DayClosed


# --- writing ------------------------------------------------------------------------------------


def _record(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    kind: str,
    from_status: str | None,
    to_status: str,
    reason: str | None,
    audit_event: str,
    now: datetime,
    ip: str | None,
) -> None:
    event = DayEvent(
        agency_id=actor.agency_id,
        report_day_id=report_day.id,
        from_status=from_status,
        to_status=to_status,
        kind=kind,
        actor_user_id=actor.id,
        reason=reason,
        created_at=now,
    )
    db.add(event)
    db.flush()
    audit.record(
        db,
        audit_event,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"report_day_id": report_day.id, "day": report_day.day.isoformat()},
        now=now,
    )


def get_or_open_day(
    db: Session,
    actor: User,
    day: date,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> ReportDay:
    """The actor's agency's day for ``day``, created ``open`` (with its event) when it does not
    exist yet. Idempotent, and safe when two requests open the same day at once. Never for a date
    after today. The caller commits."""
    moment = now or utc_now()
    check_day(day, moment.date())
    found = get_day(db, actor.agency_id, day)
    if found is not None:
        return found
    created = ReportDay(
        agency_id=actor.agency_id, day=day, status=DAY_OPEN, created_at=moment, updated_at=moment
    )
    try:
        with db.begin_nested():
            db.add(created)
            db.flush()
    except IntegrityError as exc:
        if not is_unique_violation(exc):
            raise
        raced = get_day(db, actor.agency_id, day)  # the other request committed first
        if raced is None:  # pragma: no cover - a unique violation means the row exists
            raise
        return raced
    _record(
        db,
        actor,
        created,
        kind="open",
        from_status=None,
        to_status=DAY_OPEN,
        reason=None,
        audit_event=audit.DAY_OPENED,
        now=moment,
        ip=ip,
    )
    return created


def _own(actor: User, report_day: ReportDay) -> None:
    if report_day.agency_id != actor.agency_id:
        raise DayError(NOT_FOUND)


def lock_day(db: Session, actor: User, report_day: ReportDay, *, shared: bool = False) -> ReportDay:
    """Check the day is the actor's agency's, then lock its row and reload it (so a status
    committed by a concurrent request is seen). ``shared`` (``FOR SHARE``) is for the writers of
    a day's sales: they do not block each other, but a closing (exclusive) waits for them and
    they wait for it, so nothing is saved on a day while it is being closed."""
    _own(actor, report_day)
    db.refresh(report_day, with_for_update={"read": True} if shared else True)
    return report_day


def _refuse(
    db: Session, actor: User, action: str, message: str, now: datetime | None, ip: str | None
) -> DayError:
    audit.refused(db, action, agency_id=actor.agency_id, actor_user_id=actor.id, ip=ip, now=now)
    return DayError(message)


def close_day(
    db: Session,
    actor: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> ReportDay:
    """Close the day (manager or accountant). B9 will also require "no open flag"."""
    _own(actor, report_day)
    if not permissions.can_close_day(actor):
        raise _refuse(db, actor, "close_day", CLOSE_FORBIDDEN, now, ip)
    lock_day(db, actor, report_day)
    require_writable(report_day)
    moment = now or utc_now()
    previous = report_day.status
    report_day.status = DAY_CLOSED
    report_day.closed_at = moment
    report_day.closed_by_user_id = actor.id
    report_day.updated_at = moment
    db.flush()
    _record(
        db,
        actor,
        report_day,
        kind="close",
        from_status=previous,
        to_status=DAY_CLOSED,
        reason=None,
        audit_event=audit.DAY_CLOSED,
        now=moment,
        ip=ip,
    )
    return report_day


def reopen_day(
    db: Session,
    actor: User,
    report_day: ReportDay,
    reason: str | None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> ReportDay:
    """Reopen a closed day (manager only), with a reason. It goes back to ``compared`` when an
    upload had been compared, else to ``open``."""
    _own(actor, report_day)
    if not permissions.can_reopen_day(actor):
        raise _refuse(db, actor, "reopen_day", REOPEN_FORBIDDEN, now, ip)
    try:
        clean = required_reason(reason)
    except ReasonError as exc:
        raise DayError(str(exc)) from None
    lock_day(db, actor, report_day)
    if report_day.status != DAY_CLOSED:
        raise DayError(NOT_CLOSED)
    moment = now or utc_now()
    target = DAY_COMPARED if report_day.compared_upload_id is not None else DAY_OPEN
    report_day.status = target
    report_day.closed_at = None
    report_day.closed_by_user_id = None
    report_day.updated_at = moment
    db.flush()
    _record(
        db,
        actor,
        report_day,
        kind="reopen",
        from_status=DAY_CLOSED,
        to_status=target,
        reason=clean,
        audit_event=audit.DAY_REOPENED,
        now=moment,
        ip=ip,
    )
    return report_day
