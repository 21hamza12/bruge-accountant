"""One-off partner fees (overrides) and the validators' queue.

A fee set by hand needs a manager or accountant who did not set it. The approval is a
``day_acknowledgements`` row holding the sale, the version it was given on and the
``override_digest`` of that version (partner, fee, reason, tickets with their costs). A sale is
**approved** when such a row has the digest of the sale's current version and no version after the
row carries another digest: a digest that changes and changes back is not approved again. All of
that is one SQL query, so the sidebar badge costs two counts however many overrides there are.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, exists, func, select
from sqlalchemy.orm import Session, aliased

from app import audit, days, flags, permissions, sales
from app.approvals.common import (
    NO_VALIDATE_RIGHT,
    NOT_OVERRIDE,
    OVERRIDE_DONE,
    OVERRIDE_LEGACY,
    OWN_OVERRIDE,
    SALE_NOT_FOUND,
    ApprovalError,
    ApprovalForbidden,
    ApprovalNotFound,
    ValidationQueue,
    input_from,
    refuse,
    rerun,
)
from app.clock import utc_now
from app.models import (
    ACK_OVERRIDE,
    DAY_CLOSED,
    FLAG_ANSWERED,
    SALE_ACTIVE,
    ComparisonFlag,
    DayAcknowledgement,
    ReportDay,
    Sale,
    SaleVersion,
    User,
)
from app.reasons import ReasonError, required_reason
from app.sales_rules import SaleView
from app.tenancy import bind_tenant


def _unapproved(agency_id: int, *, day_id: int | None, open_days: bool) -> Select[Any]:
    """The ids of the active sales whose current version carries an override nobody approved."""
    current = aliased(SaleVersion)
    ack = aliased(DayAcknowledgement)
    later = aliased(SaleVersion)
    changed_since = exists().where(
        later.agency_id == ack.agency_id,
        later.sale_id == ack.sale_id,
        later.version_no > ack.version_no,
        later.override_digest.is_distinct_from(ack.digest),
    )
    approved = exists().where(
        ack.agency_id == Sale.agency_id,
        ack.kind == ACK_OVERRIDE,
        ack.sale_id == Sale.id,
        ack.digest == current.override_digest,
        ~changed_since,
    )
    query = (
        select(Sale.id)
        .join(
            current,
            and_(
                current.agency_id == Sale.agency_id,
                current.sale_id == Sale.id,
                current.version_no == Sale.current_version,
            ),
        )
        .where(
            Sale.agency_id == agency_id,
            Sale.status == SALE_ACTIVE,
            current.fee_override_reason.is_not(None),
            ~approved,
        )
    )
    if day_id is not None:
        query = query.where(Sale.report_day_id == day_id)
    if open_days:
        query = query.join(
            ReportDay,
            and_(ReportDay.agency_id == Sale.agency_id, ReportDay.id == Sale.report_day_id),
        ).where(ReportDay.status != DAY_CLOSED)
    return query


def unapproved_overrides(
    db: Session, agency_id: int, *, day_id: int | None = None, open_days: bool = False
) -> tuple[SaleView, ...]:
    """The sales with a fee override waiting for a validator: of one day, or of every day not
    closed. In sale order."""
    ids = list(db.scalars(_unapproved(agency_id, day_id=day_id, open_days=open_days)))
    views = sales.agency_sales_by_id(db, agency_id, ids)
    return tuple(sorted(views, key=lambda v: v.id))


def count_unapproved(
    db: Session, agency_id: int, *, day_id: int | None = None, open_days: bool = False
) -> int:
    """How many sales ``unapproved_overrides`` would list (one query, nothing loaded)."""
    query = _unapproved(agency_id, day_id=day_id, open_days=open_days).subquery()
    return db.scalar(select(func.count()).select_from(query)) or 0


def validation_queue(
    db: Session, validator: User, report_day: ReportDay | None = None
) -> ValidationQueue:
    """The answered flags and the unapproved fee overrides, of one day or of the whole agency
    (overrides then only on days not closed). Manager and accountant."""
    if not permissions.can_validate_money(validator):
        raise ApprovalForbidden(NO_VALIDATE_RIGHT)
    if report_day is not None and report_day.agency_id != validator.agency_id:
        raise ApprovalNotFound
    query = select(ComparisonFlag).where(
        ComparisonFlag.agency_id == validator.agency_id, ComparisonFlag.status == FLAG_ANSWERED
    )
    if report_day is not None:
        query = query.where(ComparisonFlag.report_day_id == report_day.id)
    waiting = db.scalars(query.order_by(ComparisonFlag.report_day_id, ComparisonFlag.id))
    overrides = unapproved_overrides(
        db,
        validator.agency_id,
        day_id=None if report_day is None else report_day.id,
        open_days=report_day is None,
    )
    return ValidationQueue(tuple(waiting), overrides)


def pending_override_count(db: Session, validator: User, report_day: ReportDay) -> int:
    return count_unapproved(db, validator.agency_id, day_id=report_day.id)


def to_validate_query(agency_id: int) -> ColumnElement[int]:
    """Answered flags plus unapproved fee overrides on days not closed, as one SQL expression made
    of two scalar subqueries (the page shell fetches every sidebar badge in one SELECT)."""
    answered = (
        select(func.count())
        .select_from(ComparisonFlag)
        .where(ComparisonFlag.agency_id == agency_id, ComparisonFlag.status == FLAG_ANSWERED)
        .scalar_subquery()
    )
    overrides = (
        select(func.count())
        .select_from(_unapproved(agency_id, day_id=None, open_days=True).subquery())
        .scalar_subquery()
    )
    return answered + overrides


def to_validate_count(db: Session, actor: User) -> int:
    """What waits for the actor as a validator (0 for anybody who may not validate)."""
    if not permissions.can_validate_money(actor):
        return 0
    return int(db.scalar(select(to_validate_query(actor.agency_id))) or 0)


def _wrote_the_fee(db: Session, validator: User, view: SaleView) -> bool:
    """The validator wrote a version of this sale in the run of versions that carry the current
    partner, fee and reason (walking back from the current one)."""
    versions = db.scalars(
        select(SaleVersion)
        .where(SaleVersion.agency_id == view.agency_id, SaleVersion.sale_id == view.id)
        .order_by(SaleVersion.version_no.desc())
    )
    for v in versions:
        same = (
            not v.voided
            and v.partner_id == view.partner_id
            and v.fee_applied == view.fee_applied
            and v.fee_override_reason == view.fee_override_reason
        )
        if not same:
            return False
        if v.actor_user_id == validator.id:
            return True
    return False


def validate_override(
    db: Session,
    validator: User,
    sale_id: int,
    approve: bool,
    reason: str | None = None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    """Approve a one-off partner fee (recorded), or reject it: the validator edits the sale back
    to the standard fee of its day, with the reason. Manager and accountant only; nobody approves a
    fee they wrote in any version that carries it. The comparison is run again after a
    rejection."""
    moment = now or utc_now()
    if not permissions.can_validate_money(validator):
        refuse(db, validator, "validate_override", NO_VALIDATE_RIGHT, now=moment)
    bind_tenant(db, validator.agency_id)
    view = sales.get_sale_for(db, validator, sale_id)
    if view is None or view.status != SALE_ACTIVE:
        raise ApprovalNotFound(SALE_NOT_FOUND)
    if view.fee_override_reason is None:
        raise ApprovalError(NOT_OVERRIDE)
    report_day = db.scalars(
        select(ReportDay).where(
            ReportDay.agency_id == validator.agency_id, ReportDay.id == view.report_day_id
        )
    ).one()
    days.lock_day(db, validator, report_day)
    days.require_writable(report_day)
    view = sales.get_sale_for(db, validator, sale_id) or view  # re-read under the lock
    waiting = db.scalars(
        _unapproved(validator.agency_id, day_id=view.report_day_id, open_days=False).where(
            Sale.id == view.id
        )
    ).first()
    if waiting is None:
        raise ApprovalError(OVERRIDE_DONE)
    if approve:
        if view.override_digest is None:
            raise ApprovalError(OVERRIDE_LEGACY)
        if _wrote_the_fee(db, validator, view):
            refuse(db, validator, "validate_override_own", OWN_OVERRIDE, now=moment)
        db.add(
            DayAcknowledgement(
                agency_id=validator.agency_id,
                report_day_id=view.report_day_id,
                kind=ACK_OVERRIDE,
                sale_id=view.id,
                version_no=view.version_no,
                digest=view.override_digest,
                actor_user_id=validator.id,
                created_at=moment,
            )
        )
        db.flush()
    else:
        try:
            clean = required_reason(reason)
        except ReasonError as exc:
            raise ApprovalError(str(exc)) from None
        sales.edit_sale(
            db,
            validator,
            view.id,
            input_from(view, reset_fee=True),
            clean,
            now=moment,
            ip=ip,
            expected_version=view.version_no,
        )
        if flags.latest_run(db, validator, report_day) is not None:
            rerun(db, validator, report_day, moment, ip)
    audit.record(
        db,
        audit.OVERRIDE_VALIDATED,
        agency_id=validator.agency_id,
        actor_user_id=validator.id,
        ip=ip,
        details={
            "sale_id": view.id,
            "version_no": view.version_no,
            "report_day_id": view.report_day_id,
            "approved": approve,
        },
        now=moment,
    )
