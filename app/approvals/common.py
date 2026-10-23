"""What the parts of ``app.approvals`` share: the errors, the messages, the results, and the small
helpers that lock a flag, tie it to its sale, record a decision and change a status.

Nothing here is public API: callers import from ``app.approvals``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, NoReturn, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from app import audit, days, flags, sales
from app.comparison import normalise_doc
from app.errors import UserError
from app.models import (
    AG_DIRECT,
    REASON_REQUIRED_CHOICES,
    SALE_ACTIVE,
    STAGE_AGENT,
    ComparisonFlag,
    FlagDecision,
    ReportDay,
    User,
)
from app.reasons import ReasonError, clean_reason, required_reason
from app.sales_rules import SaleInput, SaleView, TicketInput
from app.tenancy import bind_tenant

AMADEUS_REASON: Final = "Corrigé selon le rapport Amadeus"
# The fields a sale owns (a flag on any other field cannot be settled by editing the sale).
SALE_FIELDS: Final = frozenset(
    {"airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc", "staff_sign"}
)
TICKET_FIELDS: Final = frozenset({"airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc"})

NOT_FOUND: Final = "Écart introuvable."
SALE_NOT_FOUND: Final = "Vente introuvable."
NO_ANSWER_RIGHT: Final = "Vous n'avez pas le droit de répondre aux écarts."
NO_VALIDATE_RIGHT: Final = "Seul le gérant ou le comptable peut valider."
NO_SIGN_CHANGE: Final = "Seul le gérant ou le comptable peut changer le signe d'une vente."
OWN_ANSWER: Final = "Vous ne pouvez pas valider votre propre réponse."
OWN_OVERRIDE: Final = "Vous ne pouvez pas valider des frais que vous avez saisis vous-même."
OWN_LIST: Final = "Vous ne pouvez pas valider une liste dont vous avez saisi des ventes."
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
SITUATION_CHANGED: Final = "La situation a changé : rechargez la page."
SALE_MOVED_ON: Final = "La vente a changé depuis la réponse de l'agent : l'agent doit re-répondre."
NOT_OVERRIDE: Final = "Cette vente n'a pas de frais exceptionnel à valider."
OVERRIDE_DONE: Final = "Ces frais exceptionnels sont déjà validés."
OVERRIDE_LEGACY: Final = "Ces frais datent d'avant la mise à jour : enregistrez la vente à nouveau."
NO_LIST: Final = "Aucun billet partenaire absent du rapport à valider."
LIST_CHANGED: Final = "La liste a changé depuis votre lecture : rechargez la page."
NO_RUN: Final = "Comparez d'abord la journée avec le rapport Amadeus."
BAD_VALUE: Final = "La valeur du rapport Amadeus ne peut pas être appliquée à la vente."
NOT_SENT_ONLY: Final = "Seul un billet non envoyé se saisit depuis l'écart."
BLOCKED_PREFIX: Final = "La journée ne peut pas être clôturée : "


class ApprovalError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


class ApprovalNotFound(ApprovalError):
    """No such flag (or sale) for this user: another agency's, or another sign's for staff."""

    def __init__(self, message: str = NOT_FOUND) -> None:
        super().__init__(message)


class ApprovalForbidden(ApprovalError):
    """The user may not do this (a refusal that is also audited)."""


class SituationChanged(ApprovalError):
    """The sales or the report moved since the flag was made. The comparison was run again, so the
    caller commits before showing the message: the screen then shows the flags as they are now."""

    def __init__(self, message: str = SITUATION_CHANGED) -> None:
        super().__init__(message)


class CloseBlocked(days.DayError):
    """The day cannot be closed yet; ``reasons`` lists what blocks, in French."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__(BLOCKED_PREFIX + "; ".join(reasons))


@dataclass(frozen=True)
class AnswerResult:
    flag_id: int
    decision_id: int
    status: str  # the flag's status after the answer
    applied: bool  # a sale was edited


@dataclass(frozen=True)
class ValidateResult:
    flag_id: int
    decision_id: int
    status: str
    applied: bool  # a sale was edited or voided


@dataclass(frozen=True)
class ValidationQueue:
    """What waits for a validator: answered flags and unapproved fee overrides."""

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


def refuse(db: Session, actor: User, action: str, message: str, *, now: datetime) -> NoReturn:
    """Audit the refusal (``permission_denied``, action name only) and raise it."""
    audit.refused(db, action, agency_id=actor.agency_id, actor_user_id=actor.id, now=now)
    raise ApprovalForbidden(message)


def reason_for(choice: str, raw: str | None) -> str | None:
    """The cleaned reason; required for the choices the schema requires one for."""
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
    return cast("CursorResult[Any]", done).rowcount == 1


def locked_flag(db: Session, actor: User, flag_id: int) -> tuple[ComparisonFlag, ReportDay]:
    """The flag as the actor may see it, with its day locked (``FOR UPDATE``: day first, then the
    flag and the sales) and both re-read under the lock."""
    bind_tenant(db, actor.agency_id)
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


def rerun(db: Session, actor: User, report_day: ReportDay, now: datetime, ip: str | None) -> None:
    """Run the comparison of the day again (after a decision that changed a sale)."""
    flags.run_comparison(db, actor, report_day, now=now, ip=ip)


def ensure_current(
    db: Session, actor: User, report_day: ReportDay, now: datetime, ip: str | None
) -> None:
    """Refuse to act on flags that no longer match the sales or the report. The comparison is run
    again first, so the next screen is true; the caller commits before showing the error."""
    if not flags.is_current(db, actor, report_day):
        rerun(db, actor, report_day, now, ip)
        raise SituationChanged


def bound_sale(db: Session, actor: User, flag: ComparisonFlag, *, single: bool = False) -> SaleView:
    """The sale a flag is about, checked: it is active, on the flag's day, and holds the flag's
    ticket (by its normalised number); with ``single`` it holds no other ticket."""
    view = sales.get_sale_for(db, actor, flag.sale_id) if flag.sale_id is not None else None
    if view is None:
        raise ApprovalNotFound(SALE_NOT_FOUND)
    holds = any(normalise_doc(t.doc_number, t.airline) == flag.doc_number for t in view.tickets)
    if view.status != SALE_ACTIVE or view.report_day_id != flag.report_day_id or not holds:
        raise SituationChanged
    if single and len(view.tickets) != 1:
        raise ApprovalError(MULTI_TICKET)
    return view


def record_decision(
    db: Session,
    actor: User,
    flag: ComparisonFlag,
    stage: str,
    choice: str,
    *,
    reason: str | None,
    old: str | None,
    new: str | None,
    sale_version_no: int | None,
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
        sale_version_no=sale_version_no,
        actor_user_id=actor.id,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def audit_flag(
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


def agent_decision(db: Session, flag: ComparisonFlag) -> FlagDecision | None:
    """The newest answer of an agent on the flag (after a rejection, the new one)."""
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


def input_from(
    view: SaleView,
    *,
    doc: str | None = None,
    field: str | None = None,
    value: object = None,
    reset_fee: bool = False,
) -> SaleInput:
    """The sale as a ``SaleInput`` (what the form would post), with one field of the ticket whose
    normalised number is ``doc`` replaced, or the fee reset to the standard one. The price of a
    direct sale and the fee of a partner sale are carried by the service."""
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
        if field in TICKET_FIELDS and normalise_doc(t.doc_number, t.airline) == doc:
            changes: dict[str, Any] = {str(field): value}
            item = replace(item, **changes)
        tickets.append(item)
    return SaleInput(
        tickets=tickets,
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


def amadeus_value(flag: ComparisonFlag) -> object:
    """The Amadeus value of a ``different`` flag, typed for the sale."""
    raw = flag.amadeus_value or ""
    if flag.field == "total_doc":
        try:
            return Decimal(raw)
        except InvalidOperation:
            raise ApprovalError(BAD_VALUE) from None
    if not raw.strip():
        raise ApprovalError(BAD_VALUE)
    return raw


def apply_amadeus(
    db: Session,
    actor: User,
    flag: ComparisonFlag,
    view: SaleView,
    *,
    expected_version: int,
    now: datetime,
    ip: str | None,
) -> None:
    """Put the Amadeus value of the flag into the sale (a new version, built on the version the
    agent looked at)."""
    data = input_from(view, doc=flag.doc_number, field=flag.field, value=amadeus_value(flag))
    sales.edit_sale(
        db,
        actor,
        view.id,
        data,
        AMADEUS_REASON,
        now=now,
        ip=ip,
        source="comparison",
        expected_version=expected_version,
        any_day=True,
    )
