"""What the day pages show: the list of recent days and the app's own TJQ AVANT of one day.

Read-only. Staff only ever get the figures and lines of their own linked signs
(``sales.visible_signs``) and, about the Amadeus report, only whether one was received: the file
name, time, uploader and line counts are agency-wide and go to manager and accountant only
(``permissions.sees_all_signs``), as do the day's events. The gate is here in Python: the data is
not even read for staff. The profit is the manager's alone (``permissions.can_see_profit``): for
anybody else it is not computed (None). Everything is computed with ``Decimal``. The comparison
with the Amadeus report is not here (B8).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import permissions, repository, sales
from app.comparison import TRNC_CANX
from app.models import AmadeusLine, ReportDay, Upload, User
from app.sales_rules import SaleView, TicketRow

LIST_DAYS: Final = 60
NO_SALES = "no_sales"  # a date with an Amadeus report but no report day (nobody sold that day)

STATUS_LABELS: Final = {
    "open": "Ouverte",
    "compared": "Comparée",
    "approved": "Approuvée",
    "closed": "Clôturée",
    NO_SALES: "Pas de ventes",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


@dataclass(frozen=True)
class DayRow:
    day: date
    status: str
    sales: int
    tickets: int
    cost: Decimal
    revenue: Decimal
    profit: Decimal | None  # None for whoever may not see it (``permissions.can_see_profit``)
    report_received: bool


@dataclass(frozen=True)
class SignTotals:
    staff_sign: str
    sales: int
    tickets: int
    cost: Decimal
    revenue: Decimal
    profit: Decimal | None  # None for whoever may not see it (``permissions.can_see_profit``)


@dataclass(frozen=True)
class DayLine:
    """One ticket line of the TJQ AVANT, with the AG and CLT of its sale."""

    row: TicketRow
    ag: str
    clt: str


@dataclass(frozen=True)
class SaleGroup:
    sale_id: int
    staff_sign: str
    ticket_count: int
    pb: Decimal
    lines: tuple[DayLine, ...]


@dataclass(frozen=True)
class UploadInfo:
    """The newest upload of a date, as manager and accountant see it."""

    upload: Upload
    lines: int
    cancelled: int


def recent_days(db: Session, actor: User, *, limit: int = LIST_DAYS) -> list[DayRow]:
    """The newest days of the agency with their figures: its report days and the dates that only
    have an Amadeus report (status ``no_sales``). Staff get their own signs' figures and only the
    days where they have a sale."""
    agency_id = actor.agency_id
    statuses: dict[date, str] = {
        d: s
        for d, s in db.execute(
            select(ReportDay.day, ReportDay.status)
            .where(ReportDay.agency_id == agency_id)
            .order_by(ReportDay.day.desc())
            .limit(limit)
        )
    }
    uploaded = set(
        db.scalars(
            select(Upload.report_date)
            .where(Upload.agency_id == agency_id)
            .group_by(Upload.report_date)
            .order_by(Upload.report_date.desc())
            .limit(limit)
        )
    )
    window = sorted(statuses.keys() | uploaded, reverse=True)[:limit]
    by_day: dict[date, list[SaleView]] = defaultdict(list)
    for view in sales.sales_for_days_for(db, actor, window):
        by_day[view.day].append(view)
    rows: list[DayRow] = []
    own_only = not permissions.sees_all_signs(actor)
    with_profit = permissions.can_see_profit(actor)
    for day in window:
        totals = sales.day_totals(by_day.get(day, ()), with_profit=with_profit)
        if own_only and totals.sales == 0:
            continue
        rows.append(
            DayRow(
                day=day,
                status=statuses.get(day, NO_SALES),
                sales=totals.sales,
                tickets=totals.tickets,
                cost=totals.cost,
                revenue=totals.revenue,
                profit=totals.profit,
                report_received=day in uploaded,
            )
        )
    return rows


def sign_totals(views: Sequence[SaleView], *, with_profit: bool) -> list[SignTotals]:
    """Totals per staff sign, signs in alphabetical order (the profit only ``with_profit``)."""
    grouped: dict[str, list[SaleView]] = defaultdict(list)
    for view in views:
        grouped[view.staff_sign].append(view)
    out: list[SignTotals] = []
    for sign in sorted(grouped):
        t = sales.day_totals(grouped[sign], with_profit=with_profit)
        out.append(SignTotals(sign, t.sales, t.tickets, t.cost, t.revenue, t.profit))
    return out


def groups_of(views: Sequence[SaleView]) -> list[SaleGroup]:
    """The TJQ AVANT lines grouped by sale: sales in creation order, tickets in entry order."""
    rows_by_sale: dict[int, list[TicketRow]] = defaultdict(list)
    for row in sales.ticket_rows(views):
        rows_by_sale[row.sale_id].append(row)
    groups: list[SaleGroup] = []
    for view in views:
        rows = rows_by_sale.get(view.id)
        if not rows:
            continue
        lines = tuple(DayLine(row=r, ag=view.ag_label, clt=view.clt or "") for r in rows)
        groups.append(SaleGroup(view.id, view.staff_sign, len(rows), view.pb, lines))
    return groups


def report_received(db: Session, agency_id: int, day: date) -> bool:
    """Whether any Amadeus report for ``day`` was uploaded (all a staff member may know)."""
    return (
        db.scalar(
            select(Upload.id)
            .where(Upload.agency_id == agency_id, Upload.report_date == day)
            .limit(1)
        )
        is not None
    )


def upload_info(db: Session, actor: User, day: date) -> UploadInfo | None:
    """The newest upload of ``day`` with its line and cancelled counts. Manager and accountant
    only: for anybody else this returns None without reading anything."""
    if not permissions.sees_all_signs(actor):
        return None
    agency_id = actor.agency_id
    upload = db.scalars(
        select(Upload)
        .where(Upload.agency_id == agency_id, Upload.report_date == day)
        .order_by(Upload.id.desc())
        .limit(1)
    ).first()
    if upload is None:
        return None
    holder = repository.lines_upload_for(db, agency_id, upload.id)
    lines, cancelled = upload.ticket_count, 0  # an upload from before the line snapshot
    if holder is not None:
        counted = db.execute(
            select(
                func.count(AmadeusLine.id),
                func.count(AmadeusLine.id).filter(
                    func.upper(func.trim(AmadeusLine.trnc)) == TRNC_CANX
                ),
            ).where(AmadeusLine.agency_id == agency_id, AmadeusLine.upload_id == holder)
        ).one()
        lines, cancelled = int(counted[0]), int(counted[1])
    return UploadInfo(upload, lines, cancelled)


def actor_names(db: Session, agency_id: int, ids: set[int]) -> dict[int, tuple[str | None, str]]:
    """``{user id: (display name or None, role)}``: never a phone number."""
    if not ids:
        return {}
    rows = db.execute(
        select(User.id, User.display_name, User.role).where(
            User.agency_id == agency_id, User.id.in_(ids)
        )
    )
    return {row.id: ((row.display_name or "").strip() or None, row.role) for row in rows}


__all__ = [
    "NO_SALES",
    "STATUS_LABELS",
    "DayLine",
    "DayRow",
    "SaleGroup",
    "SignTotals",
    "UploadInfo",
    "actor_names",
    "groups_of",
    "recent_days",
    "report_received",
    "sign_totals",
    "status_label",
    "upload_info",
]
