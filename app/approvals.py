"""Answering comparison flags, validating, and the gate in front of closing a day (B9).

Decision 18 (docs/design/02-open-decisions.md): the agent who issued the ticket answers; a change of
cost, ticket number or ticket type also needs the accountant or the manager (``needs_validation``
on the flag), and **nobody validates their own answer**.

**Answering** (``answer_flag``; staff for the flags of their own signs, manager and accountant for
any flag):

* ``amadeus_right`` on a ``different`` flag: the Amadeus value goes into the sale through
  ``sales.edit_sale`` (a new version, source ``comparison``, reason ``AMADEUS_REASON``, built on the
  version read under the lock). For a flag that needs validation (cost, type, number) nothing is
  touched yet: the flag becomes ``answered`` and the correction is applied **when a validator
  approves it**, so a rejected answer never changes money. (The brief applied it at once; then the
  re-run would have superseded the flag and the validation with it.)
* ``mine_right`` (reason): the app value stays. ``resolved`` at once, or ``answered`` when the flag
  needs validation. The approved value is a correction the TJQ APRES shows (``corrections_for``).
* ``void_entry`` (reason) on ``not_in_amadeus``: the sale is voided (one-ticket sales only: a
  longer sale is corrected from its own page). ``acknowledged`` on ``not_in_amadeus``: the agent
  keeps the entry and waits for a later report; recorded, the flag stays open and blocking.
* ``not_sent`` is answered by entering the sale (``prefill_for_not_sent`` gives the B5 form its
  values); the next run resolves it. ``elsewhere`` and ``bad_number`` are settled by correcting or
  moving the sale. Neither takes an answer.

**Validating** (``validate_flag``, ``can_validate_money``): approve -> ``resolved`` (and the
Amadeus correction is applied now when the agent chose ``amadeus_right``); reject (reason) -> back
to ``open``, the reason is shown to the agent. A status change is a compare-and-set
(``claim``) under the day lock, so two validators on one flag cannot both win.

**Fee overrides** (a partner fee changed by hand, with a reason): ``validation_queue`` lists the
ones of a day; ``validate_override`` approves (a ``day_acknowledgements`` row bound to the fee and
its reason, so an edit that keeps the fee stays approved) or rejects (the validator edits the sale
back to the standard fee with a reason). Nobody approves an override whose current version they
wrote.

**After a decision that changes a sale** the comparison of the day is run again (``flags``), so the
flags stay true to the sales (the TODO in ``flag_merge``).

**The close gate** (``can_close``, ``require_can_close``, ``close_day``): a report was received and
compared with the latest upload; no blocking flag is open or answered; every fee override of the
day is approved; the partner tickets that are not in the report have been seen. If the flags may be
stale (a sale changed, a newer upload) the comparison is run again first, which is a write: the
caller commits even when the gate says no. B10 (the export) hooks in after ``close_day`` succeeds.

Audit events carry ids only: ``flag_answered``, ``flag_validated``, ``flag_rejected``,
``override_validated``, ``partner_list_acknowledged``. A refusal is ``permission_denied``.

Functions never commit (a refusal is the usual exception: the audit row is committed before the
error). The caller rolls back on any other error.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NoReturn

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import audit, days, flags, permissions, sales
from app.clock import utc_now
from app.errors import UserError
from app.models import (
    ACK_OVERRIDE,
    ACK_PARTNER_LIST,
    AG_DIRECT,
    CHOICE_ACKNOWLEDGED,
    CHOICE_AMADEUS_RIGHT,
    CHOICE_MINE_RIGHT,
    CHOICE_REJECTED,
    CHOICE_VALIDATED,
    CHOICE_VOID_ENTRY,
    DAY_CLOSED,
    FLAG_ANSWERED,
    FLAG_DIFFERENT,
    FLAG_NOT_IN_AMADEUS,
    FLAG_NOT_SENT,
    FLAG_OPEN,
    FLAG_RESOLVED,
    REASON_REQUIRED_CHOICES,
    SALE_ACTIVE,
    STAGE_AGENT,
    STAGE_VALIDATOR,
    AmadeusLine,
    ComparisonFlag,
    ComparisonRun,
    DayAcknowledgement,
    FlagDecision,
    ReportDay,
    User,
)
from app.reasons import ReasonError, clean_reason, required_reason
from app.sales_rules import SaleInput, SaleView, TicketInput
from app.tenancy import bind_tenant

__all__ = [
    "AMADEUS_REASON",
    "COLUMN_TITLES",
    "AnswerResult",
    "ApprovalError",
    "ApprovalForbidden",
    "ApprovalNotFound",
    "CloseBlocked",
    "GateStatus",
    "NotSentPrefill",
    "ValidateResult",
    "ValidationQueue",
    "acknowledge_partner_list",
    "answer_flag",
    "can_close",
    "claim",
    "close_day",
    "corrections_for",
    "decisions_for",
    "gate_status",
    "partner_list_seen",
    "pending_override_count",
    "prefill_for_not_sent",
    "require_can_close",
    "to_answer_count",
    "to_validate_count",
    "validate_flag",
    "validate_override",
    "validation_queue",
]

AMADEUS_REASON: Final = "Corrigé selon le rapport Amadeus"
# The fields a sale owns (a flag on any other field cannot be settled by editing the sale).
SALE_FIELDS: Final = frozenset(
    {"airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc", "staff_sign"}
)
# TJQ column titles of the flag fields that have a column (pax type has none).
TICKET_FIELDS: Final = frozenset({"airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc"})
COLUMN_TITLES: Final = {
    "airline": "A/L",
    "total_doc": "TOTAL DOC",
    "pax_name": "PAX NAME",
    "staff_sign": "AS",
    "rloc": "RLOC",
    "trnc": "TRNC",
}
Corrections = Mapping[tuple[str, str], tuple[str, str, str]]

NOT_FOUND: Final = "Écart introuvable."
SALE_NOT_FOUND: Final = "Vente introuvable."
NO_ANSWER_RIGHT: Final = "Vous n'avez pas le droit de répondre aux écarts."
NO_VALIDATE_RIGHT: Final = "Seul le gérant ou le comptable peut valider."
OWN_ANSWER: Final = "Vous ne pouvez pas valider votre propre réponse."
OWN_OVERRIDE: Final = "Vous ne pouvez pas valider des frais que vous avez saisis vous-même."
ALREADY_ANSWERED: Final = "Cet écart a déjà reçu une réponse."
NOT_WAITING: Final = "Cet écart n'attend pas de validation."
GONE: Final = "Cet écart a déjà été traité par quelqu'un d'autre : rechargez la page."
BAD_CHOICE: Final = "Cette réponse n'existe pas."
NOT_FOR_KIND: Final = "Cette réponse ne convient pas à ce type d'écart."
NO_ANSWER_FOR_KIND: Final = (
    "Cet écart se règle en saisissant ou en corrigeant la vente, puis en relançant la comparaison."
)
MULTI_TICKET: Final = (
    "Cette vente contient plusieurs billets : corrigez-la depuis la page de la vente."
)
NOT_OVERRIDE: Final = "Cette vente n'a pas de frais exceptionnel à valider."
OVERRIDE_DONE: Final = "Ces frais exceptionnels sont déjà validés."
NO_LIST: Final = "Aucun billet partenaire absent du rapport à valider."
NO_RUN: Final = "Comparez d'abord la journée avec le rapport Amadeus."
BAD_VALUE: Final = "La valeur du rapport Amadeus ne peut pas être appliquée à la vente."
NOT_SENT_ONLY: Final = "Seul un billet non envoyé se saisit depuis l'écart."

GATE_NO_REPORT: Final = "Aucun rapport Amadeus n'a été reçu pour cette journée."
GATE_LIST: Final = (
    "La liste des billets partenaires absents du rapport n'a pas été vue : "
    "cliquez sur « J'ai vu la liste »."
)
BLOCKED_PREFIX: Final = "La journée ne peut pas être clôturée : "


class ApprovalError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


class ApprovalNotFound(ApprovalError):
    """No such flag (or sale) for this user: another agency's, or another sign's for staff."""

    def __init__(self, message: str = NOT_FOUND) -> None:
        super().__init__(message)


class ApprovalForbidden(ApprovalError):
    """The user may not do this (a refusal that is also audited)."""


class CloseBlocked(days.DayError):
    """The day cannot be closed yet; ``reasons`` lists what blocks, in French."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__(BLOCKED_PREFIX + " ".join(reasons))


@dataclass(frozen=True)
class AnswerResult:
    flag_id: int
    decision_id: int
    status: str  # the flag's status after the answer
    applied: bool  # a sale was edited or voided


@dataclass(frozen=True)
class ValidateResult:
    flag_id: int
    decision_id: int
    status: str
    applied: bool


@dataclass(frozen=True)
class ValidationQueue:
    """What waits for a validator on one day: answered flags and unapproved fee overrides."""

    flags: tuple[ComparisonFlag, ...]
    overrides: tuple[SaleView, ...]


@dataclass(frozen=True)
class NotSentPrefill:
    """The values of the Amadeus line of a ticket that was not entered, for the entry form."""

    flag_id: int
    report_day: date
    doc_number: str
    airline: str
    total_doc: Decimal | None
    pax_name: str
    pax_type: str
    rloc: str
    trnc: str
    staff_sign: str


# --- small helpers ------------------------------------------------------------------------------


def _refuse(db: Session, actor: User, action: str, message: str, *, now: datetime) -> NoReturn:
    audit.refused(db, action, agency_id=actor.agency_id, actor_user_id=actor.id, now=now)
    raise ApprovalForbidden(message)


def _reason(choice: str, raw: str | None) -> str | None:
    try:
        if choice in REASON_REQUIRED_CHOICES:
            return required_reason(raw)
        return clean_reason(raw)
    except ReasonError as exc:
        raise ApprovalError(str(exc)) from None


def claim(db: Session, flag: ComparisonFlag, old: str, new: str, now: datetime) -> bool:
    """Change a flag's status from ``old`` to ``new`` only if it still is ``old`` in the database
    (a compare-and-set, so a stale copy cannot overwrite somebody else's decision). Returns
    whether this call made the change."""
    done = db.execute(
        update(ComparisonFlag)
        .where(
            ComparisonFlag.agency_id == flag.agency_id,
            ComparisonFlag.id == flag.id,
            ComparisonFlag.status == old,
        )
        .values(status=new, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.expire(flag, ["status", "updated_at"])
    return bool(done.rowcount == 1)  # type: ignore[attr-defined]


def _locked_flag(db: Session, actor: User, flag_id: int) -> tuple[ComparisonFlag, ReportDay]:
    """The flag as the actor may see it, with its day locked (``FOR UPDATE``: day first, then the
    flag and the sales) and both re-read under the lock."""
    found = flags.flag_for(db, actor, flag_id)
    if found is None:
        raise ApprovalNotFound
    report_day = db.scalars(
        select(ReportDay).where(
            ReportDay.agency_id == actor.agency_id, ReportDay.id == found.report_day_id
        )
    ).one()
    days.lock_day(db, actor, report_day)
    db.refresh(found, with_for_update=True)
    days.require_writable(report_day)
    return found, report_day


def _decision(
    db: Session,
    actor: User,
    flag: ComparisonFlag,
    stage: str,
    choice: str,
    reason: str | None,
    old: str | None,
    new: str | None,
    now: datetime,
) -> FlagDecision:
    row = FlagDecision(
        agency_id=flag.agency_id,
        flag_id=flag.id,
        stage=stage,
        choice=choice,
        reason=reason,
        old_value=old,
        new_value=new,
        actor_user_id=actor.id,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def _audit(
    db: Session,
    event: str,
    actor: User,
    flag: ComparisonFlag,
    choice: str,
    status: str,
    now: datetime,
    ip: str | None,
) -> None:
    audit.record(
        db,
        event,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "flag_id": flag.id,
            "report_day_id": flag.report_day_id,
            "kind": flag.kind,
            "field": flag.field,
            "choice": choice,
            "status": status,
        },
        now=now,
    )


def _ticket_inputs(
    view: SaleView, doc: str | None, field: str | None, value: object
) -> list[TicketInput]:
    tickets: list[TicketInput] = []
    for t in view.tickets:
        item = TicketInput(
            doc_number=t.doc_number,
            airline=t.airline,
            total_doc=t.total_doc,
            pax_name=t.pax_name,
            pax_type=t.pax_type,
            rloc=t.rloc,
            trnc=t.trnc,
        )
        if field in TICKET_FIELDS and t.doc_number == doc:
            changes: dict[str, Any] = {field: value}
            item = replace(item, **changes)
        tickets.append(item)
    return tickets


def _input_from(
    view: SaleView,
    *,
    doc: str | None = None,
    field: str | None = None,
    value: object = None,
    reset_fee: bool = False,
) -> SaleInput:
    """The sale as a ``SaleInput`` (what the form would post), with one field of one ticket
    replaced, or the fee reset to the standard one. The price of a direct sale and the fee of a
    partner sale are carried by the service."""
    return SaleInput(
        tickets=_ticket_inputs(view, doc, field, value),
        ag_kind=view.ag_kind,
        partner_id=view.partner_id,
        pb=view.pb if view.ag_kind == AG_DIRECT else None,
        clt=view.clt,
        ap=view.ap,
        client_refused_phone=view.client_refused_phone,
        payment_method=view.payment_method,
        staff_sign=str(value) if field == "staff_sign" else view.staff_sign,
        reset_fee=reset_fee,
    )


def _amadeus_value(flag: ComparisonFlag) -> object:
    raw = flag.amadeus_value or ""
    if flag.field == "total_doc":
        try:
            return Decimal(raw)
        except InvalidOperation:
            raise ApprovalError(BAD_VALUE) from None
    if not raw.strip():
        raise ApprovalError(BAD_VALUE)
    return raw


def _sale_of(db: Session, actor: User, flag: ComparisonFlag) -> SaleView:
    if flag.sale_id is None:
        raise ApprovalError(NO_ANSWER_FOR_KIND)
    view = sales.get_sale_for(db, actor, flag.sale_id)
    if view is None:
        raise ApprovalNotFound(SALE_NOT_FOUND)
    return view


def _apply_amadeus(
    db: Session,
    actor: User,
    flag: ComparisonFlag,
    *,
    now: datetime,
    ip: str | None,
    expected_version: int | None,
) -> None:
    """Put the Amadeus value of the flag into the sale (a new version)."""
    view = _sale_of(db, actor, flag)
    data = _input_from(view, doc=flag.doc_number, field=flag.field, value=_amadeus_value(flag))
    sales.edit_sale(
        db,
        actor,
        view.id,
        data,
        AMADEUS_REASON,
        now=now,
        ip=ip,
        source="comparison",
        expected_version=view.version_no if expected_version is None else expected_version,
        any_day=True,
    )


def _rerun(db: Session, actor: User, report_day: ReportDay, now: datetime, ip: str | None) -> None:
    flags.run_comparison(db, actor, report_day, now=now, ip=ip)


def _agent_decision(db: Session, flag: ComparisonFlag) -> FlagDecision | None:
    """The newest answer of the agent on the flag (after a rejection, the new one)."""
    return db.scalars(
        select(FlagDecision)
        .where(
            FlagDecision.agency_id == flag.agency_id,
            FlagDecision.flag_id == flag.id,
            FlagDecision.stage == STAGE_AGENT,
        )
        .order_by(FlagDecision.id.desc())
        .limit(1)
    ).first()


# --- answering ----------------------------------------------------------------------------------

ALLOWED_CHOICES: Final = {
    FLAG_DIFFERENT: (CHOICE_AMADEUS_RIGHT, CHOICE_MINE_RIGHT),
    FLAG_NOT_IN_AMADEUS: (CHOICE_VOID_ENTRY, CHOICE_ACKNOWLEDGED),
}


def answer_flag(
    db: Session,
    actor: User,
    flag_id: int,
    choice: str,
    reason: str | None = None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
    expected_version: int | None = None,
) -> AnswerResult:
    """Answer an open flag (see the module text). Raises ``ApprovalNotFound`` (not yours),
    ``ApprovalForbidden`` (your role may not answer), ``ApprovalError`` (already answered, wrong
    choice, missing reason), ``days.DayClosed``, and what ``sales`` raises when the edit or void
    is refused. The caller commits."""
    moment = now or utc_now()
    if not permissions.can_answer_flags(actor):
        _refuse(db, actor, "answer_flag", NO_ANSWER_RIGHT, now=moment)
    bind_tenant(db, actor.agency_id)
    flag, report_day = _locked_flag(db, actor, flag_id)
    if flag.status != FLAG_OPEN:
        raise ApprovalError(ALREADY_ANSWERED)
    if choice not in {c for allowed in ALLOWED_CHOICES.values() for c in allowed}:
        raise ApprovalError(BAD_CHOICE)
    allowed = ALLOWED_CHOICES.get(flag.kind)
    if allowed is None:
        raise ApprovalError(NO_ANSWER_FOR_KIND)
    if choice not in allowed:
        raise ApprovalError(NOT_FOR_KIND)
    clean = _reason(choice, reason)
    old, new = flag.app_value, flag.app_value
    applied = False
    status = flag.status
    if choice == CHOICE_AMADEUS_RIGHT:
        if flag.field not in SALE_FIELDS:
            raise ApprovalError(NO_ANSWER_FOR_KIND)
        new = flag.amadeus_value
        if flag.needs_validation:
            status = FLAG_ANSWERED
        else:
            _apply_amadeus(db, actor, flag, now=moment, ip=ip, expected_version=expected_version)
            status, applied = FLAG_RESOLVED, True
    elif choice == CHOICE_MINE_RIGHT:
        status = FLAG_ANSWERED if flag.needs_validation else FLAG_RESOLVED
    elif choice == CHOICE_VOID_ENTRY:
        view = _sale_of(db, actor, flag)
        if len(view.tickets) != 1:
            raise ApprovalError(MULTI_TICKET)
        sales.void_sale(
            db, actor, view.id, clean, now=moment, ip=ip, any_day=True,
            expected_version=view.version_no if expected_version is None else expected_version,
        )  # fmt: skip
        new, status, applied = None, FLAG_RESOLVED, True
    # CHOICE_ACKNOWLEDGED: recorded only; the flag stays open and blocking
    row = _decision(db, actor, flag, STAGE_AGENT, choice, clean, old, new, moment)
    if status != flag.status and not claim(db, flag, FLAG_OPEN, status, moment):
        raise ApprovalError(GONE)
    _audit(db, audit.FLAG_ANSWERED, actor, flag, choice, status, moment, ip)
    if applied:
        _rerun(db, actor, report_day, moment, ip)
    return AnswerResult(flag.id, row.id, status, applied)


# --- validating ---------------------------------------------------------------------------------


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
    Manager and accountant only, and never the person who answered. Approving an
    ``amadeus_right`` answer applies the Amadeus value to the sale now."""
    moment = now or utc_now()
    if not permissions.can_validate_money(validator):
        _refuse(db, validator, "validate_flag", NO_VALIDATE_RIGHT, now=moment)
    bind_tenant(db, validator.agency_id)
    flag, report_day = _locked_flag(db, validator, flag_id)
    if flag.status != FLAG_ANSWERED:
        raise ApprovalError(NOT_WAITING if flag.status == FLAG_OPEN else GONE)
    answer = _agent_decision(db, flag)
    if answer is not None and answer.actor_user_id == validator.id:
        _refuse(db, validator, "validate_flag_own", OWN_ANSWER, now=moment)
    clean = _reason(CHOICE_VALIDATED if approve else CHOICE_REJECTED, reason)
    if not approve:
        row = _decision(
            db, validator, flag, STAGE_VALIDATOR, CHOICE_REJECTED, clean,
            flag.app_value, flag.app_value, moment,
        )  # fmt: skip
        if not claim(db, flag, FLAG_ANSWERED, FLAG_OPEN, moment):
            raise ApprovalError(GONE)
        _audit(db, audit.FLAG_REJECTED, validator, flag, CHOICE_REJECTED, FLAG_OPEN, moment, ip)
        return ValidateResult(flag.id, row.id, FLAG_OPEN, False)
    applied = answer is not None and answer.choice == CHOICE_AMADEUS_RIGHT
    if applied:
        _apply_amadeus(db, validator, flag, now=moment, ip=ip, expected_version=None)
    row = _decision(
        db, validator, flag, STAGE_VALIDATOR, CHOICE_VALIDATED, clean,
        flag.app_value, flag.amadeus_value if applied else flag.app_value, moment,
    )  # fmt: skip
    if not claim(db, flag, FLAG_ANSWERED, FLAG_RESOLVED, moment):
        raise ApprovalError(GONE)
    _audit(db, audit.FLAG_VALIDATED, validator, flag, CHOICE_VALIDATED, FLAG_RESOLVED, moment, ip)
    if applied:
        _rerun(db, validator, report_day, moment, ip)
    return ValidateResult(flag.id, row.id, FLAG_RESOLVED, applied)


# --- fee overrides ------------------------------------------------------------------------------


def _override_digest(view: SaleView) -> str:
    fee = "" if view.fee_applied is None else format(view.fee_applied.normalize(), "f")
    text = f"{fee}\x1f{view.fee_override_reason or ''}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _approved_overrides(
    db: Session, agency_id: int, sale_ids: Collection[int]
) -> set[tuple[int, str]]:
    if not sale_ids:
        return set()
    rows = db.execute(
        select(DayAcknowledgement.sale_id, DayAcknowledgement.digest).where(
            DayAcknowledgement.agency_id == agency_id,
            DayAcknowledgement.kind == ACK_OVERRIDE,
            DayAcknowledgement.sale_id.in_(list(sale_ids)),
        )
    )
    return {(int(sale_id), digest) for sale_id, digest in rows if sale_id is not None}


def _unapproved(db: Session, agency_id: int, views: list[SaleView]) -> tuple[SaleView, ...]:
    done = _approved_overrides(db, agency_id, [v.id for v in views])
    return tuple(v for v in views if (v.id, _override_digest(v)) not in done)


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
    fee set in the version they wrote themselves. The comparison is run again after a rejection."""
    moment = now or utc_now()
    if not permissions.can_validate_money(validator):
        _refuse(db, validator, "validate_override", NO_VALIDATE_RIGHT, now=moment)
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
    if not _unapproved(db, validator.agency_id, [view]):
        raise ApprovalError(OVERRIDE_DONE)
    if approve:
        if view.actor_user_id == validator.id:
            _refuse(db, validator, "validate_override_own", OWN_OVERRIDE, now=moment)
        db.add(
            DayAcknowledgement(
                agency_id=validator.agency_id,
                report_day_id=view.report_day_id,
                kind=ACK_OVERRIDE,
                sale_id=view.id,
                version_no=view.version_no,
                digest=_override_digest(view),
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
            db, validator, view.id, _input_from(view, reset_fee=True), clean,
            now=moment, ip=ip, expected_version=view.version_no,
        )  # fmt: skip
        if flags.latest_run(db, validator, report_day) is not None:
            _rerun(db, validator, report_day, moment, ip)
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


def validation_queue(
    db: Session, validator: User, report_day: ReportDay | None = None
) -> ValidationQueue:
    """The answered flags that need a validator and the unapproved fee overrides: of one day, or
    (no day) of the whole agency, overrides only on days not closed. Manager and accountant."""
    if not permissions.can_validate_money(validator):
        raise ApprovalForbidden(NO_VALIDATE_RIGHT)
    if report_day is not None and report_day.agency_id != validator.agency_id:
        raise ApprovalNotFound
    query = select(ComparisonFlag).where(
        ComparisonFlag.agency_id == validator.agency_id,
        ComparisonFlag.status == FLAG_ANSWERED,
        ComparisonFlag.needs_validation.is_(True),
    )
    if report_day is not None:
        query = query.where(ComparisonFlag.report_day_id == report_day.id)
    waiting = db.scalars(query.order_by(ComparisonFlag.report_day_id, ComparisonFlag.id))
    candidates = (
        sales.overrides_on_open_days(db, validator.agency_id)
        if report_day is None
        else sales.overrides_for_day(db, validator, report_day.day)
    )
    return ValidationQueue(tuple(waiting), _unapproved(db, validator.agency_id, candidates))


def pending_override_count(db: Session, validator: User, report_day: ReportDay) -> int:
    return len(validation_queue(db, validator, report_day).overrides)


# --- the partner list ---------------------------------------------------------------------------


def _list_digest(docs: Collection[str]) -> str:
    return hashlib.sha256("|".join(sorted(docs)).encode()).hexdigest()[:16]


def _partner_docs(db: Session, actor: User, report_day: ReportDay) -> list[str]:
    summary = flags.day_summary(db, actor, report_day)
    return [t.doc_number for t in summary.partner_not_in_report]


def partner_list_seen(db: Session, actor: User, report_day: ReportDay) -> bool:
    """The partner tickets missing from the report were acknowledged (an empty list needs none).
    A different list (a ticket added or removed since) must be seen again."""
    docs = _partner_docs(db, actor, report_day)
    if not docs:
        return True
    return (
        db.scalars(
            select(DayAcknowledgement.id)
            .where(
                DayAcknowledgement.agency_id == report_day.agency_id,
                DayAcknowledgement.report_day_id == report_day.id,
                DayAcknowledgement.kind == ACK_PARTNER_LIST,
                DayAcknowledgement.digest == _list_digest(docs),
            )
            .limit(1)
        ).first()
        is not None
    )


def acknowledge_partner_list(
    db: Session,
    validator: User,
    report_day: ReportDay,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    """Record that the partner tickets not in the report were seen (manager or accountant)."""
    moment = now or utc_now()
    if not permissions.can_validate_money(validator):
        _refuse(db, validator, "acknowledge_partner_list", NO_VALIDATE_RIGHT, now=moment)
    if report_day.agency_id != validator.agency_id:
        raise ApprovalNotFound
    bind_tenant(db, validator.agency_id)
    days.lock_day(db, validator, report_day)
    days.require_writable(report_day)
    run = flags.latest_run(db, validator, report_day)
    if run is None:
        raise ApprovalError(NO_RUN)
    docs = _partner_docs(db, validator, report_day)
    if not docs:
        raise ApprovalError(NO_LIST)
    db.add(
        DayAcknowledgement(
            agency_id=validator.agency_id,
            report_day_id=report_day.id,
            kind=ACK_PARTNER_LIST,
            run_id=run.id,
            digest=_list_digest(docs),
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
        details={"report_day_id": report_day.id, "run_id": run.id, "tickets": len(docs)},
        now=moment,
    )


# --- the close gate -----------------------------------------------------------------------------


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


def _gate_reasons(db: Session, actor: User, report_day: ReportDay) -> list[str]:
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
    with_override = [
        v
        for v in sales.agency_sales_for_day(db, actor.agency_id, report_day.day)
        if v.fee_override_reason is not None
    ]
    overrides = _unapproved(db, actor.agency_id, with_override)
    if overrides:
        reasons.append(
            _plural(
                len(overrides),
                "1 frais exceptionnel de partenaire attend la validation.",
                "{n} frais exceptionnels de partenaires attendent la validation.",
            )
        )
    if not partner_list_seen(db, actor, report_day):
        reasons.append(GATE_LIST)
    return reasons


@dataclass(frozen=True)
class GateStatus:
    """What the day page shows before anybody tries to close: the reasons as the stored flags and
    sales give them, and whether the comparison would be run again at the closing (``stale``)."""

    reasons: tuple[str, ...]
    stale: bool


def gate_status(db: Session, actor: User, report_day: ReportDay) -> GateStatus:
    """Read-only preview of the close gate (nothing is run or written)."""
    if report_day.agency_id != actor.agency_id:
        raise days.DayError(days.NOT_FOUND)
    if flags.latest_upload_for_day(db, actor.agency_id, report_day.day) is None:
        return GateStatus((GATE_NO_REPORT,), False)
    stale = not flags.is_current(db, actor, report_day)
    return GateStatus(tuple(_gate_reasons(db, actor, report_day)), stale)


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
    run cannot be made (no lines, the days changed meanwhile) the session is rolled back and the
    error is the reason, so call this before writing anything else in the transaction."""
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
            _rerun(db, actor, report_day, moment, ip)
        except UserError as exc:
            db.rollback()  # the run stopped part-way: keep nothing of it (the caller wrote nothing)
            return [str(exc)]
    return _gate_reasons(db, actor, report_day)


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
    ``days.close_day``. B10 (the TJQ APRES) stores the export after this succeeds."""
    if permissions.can_close_day(actor):
        require_can_close(db, actor, report_day, now=now, ip=ip)
    return days.close_day(db, actor, report_day, now=now, ip=ip)


# --- reading ------------------------------------------------------------------------------------


def to_answer_count(db: Session, actor: User) -> int:
    """Open blocking flags the actor may see (the sidebar badge)."""
    return flags.open_blocking_count(db, actor)


def to_validate_count(db: Session, actor: User) -> int:
    """What waits for the actor as a validator: answered flags that need validation, and fee
    overrides on days not closed (0 for anybody who may not validate)."""
    if not permissions.can_validate_money(actor):
        return 0
    queue = validation_queue(db, actor)
    return len(queue.flags) + len(queue.overrides)


def decisions_for(
    db: Session, actor: User, flag_ids: Collection[int]
) -> dict[int, list[FlagDecision]]:
    """The decisions of the flags the actor may see, oldest first, by flag id."""
    found: dict[int, list[FlagDecision]] = {}
    for flag_id in dict.fromkeys(flag_ids):
        if flags.flag_for(db, actor, flag_id) is None:
            continue
        rows = list(
            db.scalars(
                select(FlagDecision)
                .where(FlagDecision.agency_id == actor.agency_id, FlagDecision.flag_id == flag_id)
                .order_by(FlagDecision.id)
            )
        )
        if rows:
            found[flag_id] = rows
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
    line = db.scalars(
        select(AmadeusLine).where(
            AmadeusLine.agency_id == flag.agency_id,
            AmadeusLine.upload_id == (run.upload_id if run else -1),
            AmadeusLine.seq_order == flag.amadeus_seq,
        )
    ).first()
    if run is None or day is None or line is None:
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


def corrections_for(db: Session, agency_id: int, report_day_id: int) -> Corrections:
    """What the TJQ APRES shows as corrected: ``{(doc number, column title): (Amadeus original,
    approved value, who approved)}`` for the resolved ``different`` flags of the day whose app
    value was kept. Only fields with a TJQ column (not pax type). A value taken from Amadeus is no
    correction: the sale was edited and the flag is gone. Keys use the flag's 10-digit number."""
    resolved = list(
        db.scalars(
            select(ComparisonFlag)
            .where(
                ComparisonFlag.agency_id == agency_id,
                ComparisonFlag.report_day_id == report_day_id,
                ComparisonFlag.kind == FLAG_DIFFERENT,
                ComparisonFlag.status == FLAG_RESOLVED,
            )
            .order_by(ComparisonFlag.id)
        )
    )
    wanted = [f for f in resolved if f.field in COLUMN_TITLES and f.app_value != f.amadeus_value]
    if not wanted:
        return {}
    decided = db.scalars(
        select(FlagDecision)
        .where(
            FlagDecision.agency_id == agency_id,
            FlagDecision.flag_id.in_([f.id for f in wanted]),
        )
        .order_by(FlagDecision.id)
    )
    who_id: dict[int, int] = {}
    for (
        d
    ) in decided:  # the last decision of the flag decides who approved; a validator over an agent
        if d.choice in (CHOICE_VALIDATED, CHOICE_MINE_RIGHT) and (
            d.flag_id not in who_id or d.stage == STAGE_VALIDATOR
        ):
            who_id[d.flag_id] = d.actor_user_id
    users = {
        u.id: u
        for u in db.scalars(
            select(User).where(User.agency_id == agency_id, User.id.in_(set(who_id.values())))
        )
    }
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    for f in wanted:
        user = users.get(who_id.get(f.id, -1))
        who = (
            (user.display_name or "").strip() or f"Utilisateur {user.id}"
            if user is not None
            else "—"
        )
        out[(f.doc_number, COLUMN_TITLES[f.field or ""])] = (
            f.amadeus_value or "",
            f.app_value or "",
            who,
        )
    return out
