"""Keeping the flags of a day between runs, and the day's status (the storage half of B8).

Used by ``app/flags.py``; the callers hold the day lock (``FOR UPDATE``) and own the transaction.

* A flag is identified by its **key** ``(ticket number, kind, field)``. The live flag of a key
  (open,
  answered, resolved) is KEPT, whatever its status, only when the app value, the Amadeus value AND
  the **fingerprint** are the same as before. The fingerprint is a short hash of every fact of the
  ticket that exists on either side (amount, name, airline, type, pax type, booking, sign), so a
  decision made for one state of the ticket is never carried over to another. Otherwise the old flag
  becomes ``superseded`` and a new ``open`` one replaces it. A key that no longer appears is
  ``superseded``. Nothing is deleted.
* Superseding a BLOCKING flag is audited (ids only): a decision never disappears silently.
* ``settle_day``: ``compared`` while a blocking flag is open or answered; ``approved`` only when the
  actor may approve (manager, accountant: ``can_run_comparison``). A run by anybody else never
  approves: it keeps the day ``compared`` (an already approved day with nothing blocking stays so).
  Becoming approved is audited.
* Signs are stored stripped and upper-cased (``norm_sign``); a blank sign is stored as ``""`` and is
  visible to manager and accountant only, since no staff member's sign is blank.
* TODO B9: a sale edited, voided or moved after a comparison makes the flags stale. B9 must re-run
  the comparison (or make the close gate run it first) before it trusts the flags.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.comparison import AmadeusTicket, AppTicket, Flag
from app.models import (
    DAY_APPROVED,
    DAY_COMPARED,
    FLAG_ANSWERED,
    FLAG_LIVE_STATUSES,
    FLAG_OPEN,
    FLAG_SUPERSEDED,
    ComparisonFlag,
    ComparisonRun,
    DayEvent,
    ReportDay,
    User,
)

__all__ = ["EVENT_APPROVE", "EVENT_COMPARE", "Key", "fingerprint", "merge_flags", "norm_sign"]

EVENT_COMPARE: Final = "compare"
EVENT_APPROVE: Final = "approve"
Key = tuple[str, str, str]  # (ticket number, kind, field or "")


def norm_sign(sign: str) -> str:
    return sign.strip().upper()


def _plain(value: Decimal | None) -> str:
    return "" if value is None else format(value.normalize(), "f")


def _facts(
    t: AppTicket | AmadeusTicket | None,
) -> tuple[str, ...]:
    if t is None:
        return ()
    sign = t.staff_sign
    return (
        _plain(t.total_doc),
        " ".join(t.pax_name.split()).casefold(),
        t.airline.strip().upper(),
        t.trnc.strip().upper(),
        t.pax_type.strip().upper(),
        t.rloc.strip().upper(),
        norm_sign(sign),
    )


def fingerprint(app: AppTicket | None, amadeus: AmadeusTicket | None) -> str:
    """A short hash of the ticket facts of the side(s) that exist."""
    text = "\x1f".join(_facts(app)) + "\x1e" + "\x1f".join(_facts(amadeus))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _values(app_value: str | None, amadeus_value: str | None) -> tuple[str, str]:
    return (app_value or "", amadeus_value or "")


def key_of(flag: Flag) -> Key:
    return (flag.doc_number, flag.kind.lower(), flag.field)


def _live_flags(db: Session, agency_id: int, day_id: int) -> dict[Key, list[ComparisonFlag]]:
    """The live flags of a day by key, newest first."""
    grouped: dict[Key, list[ComparisonFlag]] = {}
    rows = db.scalars(
        select(ComparisonFlag)
        .where(
            ComparisonFlag.agency_id == agency_id,
            ComparisonFlag.report_day_id == day_id,
            ComparisonFlag.status.in_(FLAG_LIVE_STATUSES),
        )
        .order_by(ComparisonFlag.id.desc())
        .execution_options(populate_existing=True)
    )
    for row in rows:
        grouped.setdefault((row.doc_number, row.kind, row.field or ""), []).append(row)
    return grouped


def _supersede(row: ComparisonFlag, now: datetime, dropped: list[int]) -> None:
    if row.blocking and row.status in (FLAG_OPEN, FLAG_ANSWERED):
        dropped.append(row.id)
    row.status, row.updated_at = FLAG_SUPERSEDED, now


def merge_flags(
    db: Session,
    actor: User,
    report_day: ReportDay,
    run: ComparisonRun,
    found: Sequence[tuple[Flag, str]],
    now: datetime,
    ip: str | None = None,
) -> None:
    """Store ``found`` (flags with their fingerprints) as the live flags of the day."""
    existing = _live_flags(db, report_day.agency_id, report_day.id)
    seen: set[Key] = set()
    to_create: list[tuple[Flag, str]] = []
    dropped: list[int] = []
    for flag, print_ in found:
        key = key_of(flag)
        seen.add(key)
        olds = existing.get(key, [])
        for stale in olds[1:]:  # more than one live flag of a key cannot happen; heal it
            _supersede(stale, now, dropped)
        if (
            olds
            and olds[0].fingerprint == print_
            and _values(olds[0].app_value, olds[0].amadeus_value)
            == _values(flag.app_value, flag.amadeus_value)
        ):
            _follow_ticket(olds[0], flag, now)
            continue
        if olds:
            _supersede(olds[0], now, dropped)
        to_create.append((flag, print_))
    for key, olds in existing.items():
        if key not in seen:
            for old in olds:
                _supersede(old, now, dropped)
    db.flush()  # the superseded flags are out of the way before the new ones go in
    for flag, print_ in to_create:
        db.add(
            ComparisonFlag(
                agency_id=report_day.agency_id,
                report_day_id=report_day.id,
                run_id=run.id,
                doc_number=flag.doc_number,
                kind=flag.kind.lower(),
                field=flag.field or None,
                app_value=flag.app_value or None,
                amadeus_value=flag.amadeus_value or None,
                fingerprint=print_,
                sale_id=flag.sale_id,
                amadeus_seq=flag.amadeus_seq,
                responsible_sign=norm_sign(flag.responsible_sign),
                needs_validation=flag.needs_validation,
                blocking=flag.blocking,
                status=FLAG_OPEN,
                created_at=now,
                updated_at=now,
            )
        )
    db.flush()
    if dropped:
        audit.record(
            db,
            audit.COMPARISON_SUPERSEDED,
            agency_id=report_day.agency_id,
            actor_user_id=actor.id,
            ip=ip,
            details={"run_id": run.id, "report_day_id": report_day.id, "flag_ids": dropped},
            now=now,
        )


def _follow_ticket(row: ComparisonFlag, flag: Flag, now: datetime) -> None:
    """Same decision, same facts: bring up to date what only follows the ticket (which sale it is
    in now, its line in the report, who answers for it)."""
    fresh = (
        flag.sale_id,
        flag.amadeus_seq,
        norm_sign(flag.responsible_sign),
        flag.needs_validation,
        flag.blocking,
    )
    current = (
        row.sale_id,
        row.amadeus_seq,
        row.responsible_sign,
        row.needs_validation,
        row.blocking,
    )
    if current != fresh:
        (
            row.sale_id,
            row.amadeus_seq,
            row.responsible_sign,
            row.needs_validation,
            row.blocking,
        ) = fresh
        row.updated_at = now


def blocking_left(db: Session, agency_id: int, day_id: int) -> bool:
    return (
        db.scalar(
            select(func.count())
            .select_from(ComparisonFlag)
            .where(
                ComparisonFlag.agency_id == agency_id,
                ComparisonFlag.report_day_id == day_id,
                ComparisonFlag.blocking.is_(True),
                ComparisonFlag.status.in_((FLAG_OPEN, FLAG_ANSWERED)),
            )
        )
        or 0
    ) > 0


def settle_day(
    db: Session,
    actor: User,
    day: ReportDay,
    now: datetime,
    *,
    upload_id: int | None,
    may_approve: bool,
    ip: str | None = None,
) -> None:
    """``compared`` while something blocks; ``approved`` when nothing does and ``may_approve``;
    otherwise the day keeps its status (``compared`` at least). With an upload (the day was just
    compared) also remember it."""
    if blocking_left(db, day.agency_id, day.id):
        target = DAY_COMPARED
    elif may_approve:
        target = DAY_APPROVED
    else:
        target = day.status if day.status == DAY_APPROVED else DAY_COMPARED
    previous = day.status
    if upload_id is not None:
        day.compared_upload_id = upload_id
    if previous != target:
        day.status = target
        db.add(
            DayEvent(
                agency_id=day.agency_id,
                report_day_id=day.id,
                from_status=previous,
                to_status=target,
                kind=EVENT_APPROVE if target == DAY_APPROVED else EVENT_COMPARE,
                actor_user_id=actor.id,
                reason=None,
                created_at=now,
            )
        )
        if target == DAY_APPROVED:
            audit.record(
                db,
                audit.DAY_APPROVED,
                agency_id=day.agency_id,
                actor_user_id=actor.id,
                ip=ip,
                details={"report_day_id": day.id, "day": day.day.isoformat()},
                now=now,
            )
    day.updated_at = now
    db.flush()
