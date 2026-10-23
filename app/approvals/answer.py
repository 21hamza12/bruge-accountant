"""Answering a flag and validating the answer (decision 18), and the reads that go with them.

* ``answer_flag``: the agent of the sign (staff), or a manager or accountant. ``amadeus_right``
  edits the sale at once when the field is the agent's own (name, airline, booking reference),
  else it waits; ``mine_right`` keeps the app value; ``void_entry`` asks for the sale to be voided;
  ``acknowledged`` records that the agent waits for a later report.
* Anything that moves money, attribution, fare class or the ticket itself, and every void, is
  **answered**, not applied: a validator who is not the answerer approves it, and only then is the
  sale edited or voided, built on the version of the sale the agent looked at. If the sale moved
  meanwhile the flag goes back to ``open`` for a new answer.
* Before an answer or an approval the flag is tied to its sale (``common.bound_sale``) and checked
  against the current sales and report (``common.ensure_current``).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, flags, permissions, sales
from app.approvals.common import (
    ALREADY_ANSWERED,
    BAD_CHOICE,
    GONE,
    NO_ANSWER_FOR_KIND,
    NO_ANSWER_RIGHT,
    NO_SIGN_CHANGE,
    NO_VALIDATE_RIGHT,
    NOT_FOR_KIND,
    NOT_SENT_ONLY,
    NOT_WAITING,
    OWN_ANSWER,
    SALE_FIELDS,
    SALE_MOVED_ON,
    AnswerResult,
    ApprovalError,
    ApprovalNotFound,
    NotSentPrefill,
    ValidateResult,
    agent_decision,
    apply_amadeus,
    audit_flag,
    bound_sale,
    claim,
    ensure_current,
    locked_flag,
    reason_for,
    record_decision,
    refuse,
    rerun,
)
from app.clock import utc_now
from app.models import (
    CHOICE_ACKNOWLEDGED,
    CHOICE_AMADEUS_RIGHT,
    CHOICE_MINE_RIGHT,
    CHOICE_REJECTED,
    CHOICE_VALIDATED,
    CHOICE_VOID_ENTRY,
    FLAG_ANSWERED,
    FLAG_DIFFERENT,
    FLAG_NOT_IN_AMADEUS,
    FLAG_NOT_SENT,
    FLAG_OPEN,
    FLAG_RESOLVED,
    STAGE_AGENT,
    STAGE_VALIDATOR,
    AmadeusLine,
    ComparisonFlag,
    ComparisonRun,
    FlagDecision,
    ReportDay,
    User,
)

ALLOWED_CHOICES: Final[dict[str, tuple[str, ...]]] = {
    FLAG_DIFFERENT: (CHOICE_AMADEUS_RIGHT, CHOICE_MINE_RIGHT),
    FLAG_NOT_IN_AMADEUS: (CHOICE_VOID_ENTRY, CHOICE_ACKNOWLEDGED),
}
EVERY_CHOICE: Final = frozenset(c for allowed in ALLOWED_CHOICES.values() for c in allowed)


def answer_flag(
    db: Session,
    actor: User,
    flag_id: int,
    choice: str,
    reason: str | None = None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> AnswerResult:
    """Answer an open flag (see the module text). Raises ``ApprovalNotFound`` (not yours),
    ``ApprovalForbidden`` (your role may not answer this), ``SituationChanged`` (commit, then show
    it), ``ApprovalError`` (already answered, wrong choice, missing reason), ``days.DayClosed``,
    and what ``sales`` raises when the edit is refused. The caller commits."""
    moment = now or utc_now()
    if not permissions.can_answer_flags(actor):
        refuse(db, actor, "answer_flag", NO_ANSWER_RIGHT, now=moment)
    flag, report_day = locked_flag(db, actor, flag_id)
    if flag.status != FLAG_OPEN:
        raise ApprovalError(ALREADY_ANSWERED)
    if choice not in EVERY_CHOICE:
        raise ApprovalError(BAD_CHOICE)
    allowed = ALLOWED_CHOICES.get(flag.kind)
    if allowed is None:
        raise ApprovalError(NO_ANSWER_FOR_KIND)
    if choice not in allowed:
        raise ApprovalError(NOT_FOR_KIND)
    if choice == CHOICE_AMADEUS_RIGHT:
        if flag.field not in SALE_FIELDS:
            raise ApprovalError(NO_ANSWER_FOR_KIND)
        if flag.field == "staff_sign" and not permissions.sees_all_signs(actor):
            refuse(db, actor, "answer_flag_sign", NO_SIGN_CHANGE, now=moment)
    clean = reason_for(choice, reason)
    if choice == CHOICE_ACKNOWLEDGED:
        return _wait(db, actor, flag, moment, ip)
    ensure_current(db, actor, report_day, moment, ip)
    view = bound_sale(db, actor, flag, single=choice == CHOICE_VOID_ENTRY)
    old, new = flag.app_value, flag.app_value
    status, applied = FLAG_ANSWERED, False
    if choice == CHOICE_AMADEUS_RIGHT:
        new = flag.amadeus_value
        if not flag.needs_validation:
            apply_amadeus(
                db, actor, flag, view, expected_version=view.version_no, now=moment, ip=ip
            )
            status, applied = FLAG_RESOLVED, True
    elif choice == CHOICE_MINE_RIGHT:
        status = FLAG_ANSWERED if flag.needs_validation else FLAG_RESOLVED
    else:  # CHOICE_VOID_ENTRY: a validator voids the sale, never the agent alone
        new = None
    row = record_decision(
        db,
        actor,
        flag,
        STAGE_AGENT,
        choice,
        reason=clean,
        old=old,
        new=new,
        sale_version_no=view.version_no,
        now=moment,
    )
    if not claim(db, flag, FLAG_OPEN, status, moment):
        raise ApprovalError(GONE)
    audit_flag(db, audit.FLAG_ANSWERED, actor, flag, choice, status, moment, ip)
    if applied:
        rerun(db, actor, report_day, moment, ip)
    return AnswerResult(flag.id, row.id, status, applied)


def _wait(
    db: Session, actor: User, flag: ComparisonFlag, now: datetime, ip: str | None
) -> AnswerResult:
    """The agent keeps the entry and waits for a later report: recorded once."""
    last = agent_decision(db, flag)
    if last is not None and last.choice == CHOICE_ACKNOWLEDGED:
        return AnswerResult(flag.id, last.id, flag.status, False)
    row = record_decision(
        db,
        actor,
        flag,
        STAGE_AGENT,
        CHOICE_ACKNOWLEDGED,
        reason=None,
        old=flag.app_value,
        new=flag.app_value,
        sale_version_no=None,
        now=now,
    )
    audit_flag(db, audit.FLAG_ANSWERED, actor, flag, CHOICE_ACKNOWLEDGED, flag.status, now, ip)
    return AnswerResult(flag.id, row.id, flag.status, False)


def validate_flag(
    db: Session,
    validator: User,
    flag_id: int,
    approve: bool,
    reason: str | None = None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> ValidateResult:
    """Approve (``resolved``) or reject (``open`` again, reason required) an answered flag.
    Manager and accountant only, and never the person who answered. Approving applies the answer
    to the sale (``amadeus_right``: the Amadeus value; ``void_entry``: the void), built on the
    version the agent saw: if the sale moved, the flag goes back to ``open`` instead."""
    moment = now or utc_now()
    if not permissions.can_validate_money(validator):
        refuse(db, validator, "validate_flag", NO_VALIDATE_RIGHT, now=moment)
    flag, report_day = locked_flag(db, validator, flag_id)
    if flag.status != FLAG_ANSWERED:
        raise ApprovalError(NOT_WAITING if flag.status == FLAG_OPEN else GONE)
    answer = agent_decision(db, flag)
    if answer is not None and answer.actor_user_id == validator.id:
        refuse(db, validator, "validate_flag_own", OWN_ANSWER, now=moment)
    clean = reason_for(CHOICE_VALIDATED if approve else CHOICE_REJECTED, reason)
    if not approve:
        return _send_back(db, validator, flag, clean, moment, ip)
    ensure_current(db, validator, report_day, moment, ip)
    choice = answer.choice if answer is not None else CHOICE_MINE_RIGHT
    view = bound_sale(db, validator, flag, single=choice == CHOICE_VOID_ENTRY)
    seen = answer.sale_version_no if answer is not None else None
    applying = choice in (CHOICE_AMADEUS_RIGHT, CHOICE_VOID_ENTRY)
    if applying and seen is not None and view.version_no != seen:
        return _send_back(db, validator, flag, SALE_MOVED_ON, moment, ip)
    if choice == CHOICE_AMADEUS_RIGHT:
        apply_amadeus(
            db,
            validator,
            flag,
            view,
            expected_version=view.version_no,
            now=moment,
            ip=ip,
        )
    elif choice == CHOICE_VOID_ENTRY:
        sales.void_sale(
            db,
            validator,
            view.id,
            answer.reason if answer is not None else None,
            now=moment,
            ip=ip,
            expected_version=view.version_no,
        )
    kept = flag.amadeus_value if choice == CHOICE_AMADEUS_RIGHT else flag.app_value
    row = record_decision(
        db,
        validator,
        flag,
        STAGE_VALIDATOR,
        CHOICE_VALIDATED,
        reason=clean,
        old=flag.app_value,
        new=None if choice == CHOICE_VOID_ENTRY else kept,
        sale_version_no=view.version_no,
        now=moment,
    )
    if not claim(db, flag, FLAG_ANSWERED, FLAG_RESOLVED, moment):
        raise ApprovalError(GONE)
    audit_flag(
        db, audit.FLAG_VALIDATED, validator, flag, CHOICE_VALIDATED, FLAG_RESOLVED, moment, ip
    )
    if applying:
        rerun(db, validator, report_day, moment, ip)
    return ValidateResult(flag.id, row.id, FLAG_RESOLVED, applying)


def _send_back(
    db: Session,
    validator: User,
    flag: ComparisonFlag,
    reason: str | None,
    now: datetime,
    ip: str | None,
) -> ValidateResult:
    """Reject: the flag is ``open`` again with the reason, for the agent to answer anew."""
    row = record_decision(
        db,
        validator,
        flag,
        STAGE_VALIDATOR,
        CHOICE_REJECTED,
        reason=reason,
        old=flag.app_value,
        new=flag.app_value,
        sale_version_no=None,
        now=now,
    )
    if not claim(db, flag, FLAG_ANSWERED, FLAG_OPEN, now):
        raise ApprovalError(GONE)
    audit_flag(db, audit.FLAG_REJECTED, validator, flag, CHOICE_REJECTED, FLAG_OPEN, now, ip)
    return ValidateResult(flag.id, row.id, FLAG_OPEN, False)


def decisions_for(
    db: Session, actor: User, flag_ids: Collection[int]
) -> dict[int, list[FlagDecision]]:
    """The decisions of the flags the actor may see, oldest first, by flag id: one query for the
    visible flags and one for their decisions, however many flags are asked for."""
    visible = flags.visible_flag_ids(db, actor, flag_ids)
    if not visible:
        return {}
    found: dict[int, list[FlagDecision]] = {}
    rows = db.scalars(
        select(FlagDecision)
        .where(FlagDecision.agency_id == actor.agency_id, FlagDecision.flag_id.in_(visible))
        .order_by(FlagDecision.id)
    )
    for row in rows:
        found.setdefault(row.flag_id, []).append(row)
    return found


def prefill_for_not_sent(db: Session, actor: User, flag_id: int) -> NotSentPrefill:
    """The values of the report line behind a ``not_sent`` flag, for the entry form."""
    flag = flags.flag_for(db, actor, flag_id)
    if flag is None:
        raise ApprovalNotFound
    if flag.kind != FLAG_NOT_SENT or flag.amadeus_seq is None:
        raise ApprovalError(NOT_SENT_ONLY)
    run = db.get(ComparisonRun, flag.run_id)
    day = db.get(ReportDay, flag.report_day_id)
    if run is None or day is None:
        raise ApprovalNotFound
    line = db.scalars(
        select(AmadeusLine).where(
            AmadeusLine.agency_id == flag.agency_id,
            AmadeusLine.upload_id == run.upload_id,
            AmadeusLine.seq_order == flag.amadeus_seq,
        )
    ).first()
    if line is None:
        raise ApprovalNotFound
    return NotSentPrefill(
        flag_id=flag.id,
        report_day=day.day,
        doc_number=flag.doc_number,
        airline=line.airline,
        total_doc=line.total_doc,
        pax_name=line.pax_name,
        pax_type=line.pax_type,
        rloc=line.rloc,
        trnc=line.trnc,
        staff_sign=line.staff_sign,
    )
