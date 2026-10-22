"""Partner aliases: the other names an agency is known by in the Amadeus text.

The paste screen reads the agency of a booking from the ``AP`` line (``... - REND TOURISME ET
VOYAGES - A``) and looks for a partner of that name. Agencies write themselves differently from
one booking to the next, so an agent who chose a partner for a text that did not match exactly
**proposes** that text as an alias of the partner (``pending``); a manager or accountant
**approves** it (it then matches like the partner's own name), **rejects** it (never proposed
again) or **reassigns** it to another partner. An approved alias can later be removed, which
turns it into a rejected one: rows are never deleted, so a decision is never forgotten.

* The partner's own name is an implicit alias and is not stored.
* ``alias_key`` (``pnr_sales.normalise_key``: accents removed, case folded, punctuation and
  spaces collapsed) is unique per agency: one row per text. A rejected text is therefore never
  proposed again, for any partner. A pending or rejected alias never matches anything.
* Every function filters on the agency explicitly, on top of row-level security. An alias or a
  partner of another agency is "not found" (:class:`AliasError`).
* Audit events carry ids only (``alias_id``, ``partner_id``), never the text.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, partners
from app.accounts import is_unique_violation
from app.clock import utc_now
from app.errors import UserError
from app.models import (
    ALIAS_APPROVED,
    ALIAS_PENDING,
    ALIAS_REJECTED,
    Partner,
    PartnerAlias,
    User,
)
from app.parsing.amadeus import clean_text
from app.parsing.pnr_sales import normalise_key

MAX_ALIAS_LENGTH = 120
MAX_ALIAS_KEY_LENGTH = 480
MAX_PENDING = 20  # pending names per agency: beyond it nothing more is proposed

NOT_FOUND = "Nom d'agence introuvable."
NOT_REJECTED = "Ce nom d'agence n'est pas refusé."
NAME_IS_PARTNER = "Ce nom est déjà le nom d'un partenaire."
NOT_PENDING = "Ce nom d'agence a déjà été traité."
NOT_APPROVED = "Ce nom d'agence n'est pas validé."
PARTNER_NOT_FOUND = "Partenaire introuvable."
PARTNER_INACTIVE = "Ce partenaire est désactivé."


class AliasError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


class AliasNotFound(AliasError):
    """No such alias in the actor's agency (another agency's is the same: the routes answer 404)."""


def _clean(text: str) -> tuple[str, str]:
    alias = " ".join(clean_text(text).split())[:MAX_ALIAS_LENGTH].strip() if text else ""
    return alias, normalise_key(alias)[:MAX_ALIAS_KEY_LENGTH]


# --- reading ------------------------------------------------------------------------------------


def approved_pairs(db: Session, agency_id: int) -> list[tuple[int, str]]:
    """``(partner id, alias)`` of the approved aliases of active partners: what the paste screen
    adds to the partners' own names when it looks for the agency of a booking."""
    rows = db.execute(
        select(PartnerAlias.partner_id, PartnerAlias.alias)
        .join(
            Partner,
            (Partner.agency_id == PartnerAlias.agency_id) & (Partner.id == PartnerAlias.partner_id),
        )
        .where(
            PartnerAlias.agency_id == agency_id,
            PartnerAlias.status == ALIAS_APPROVED,
            Partner.is_active.is_(True),
        )
        .order_by(PartnerAlias.id)
    )
    return [(row.partner_id, row.alias) for row in rows]


def approved_for(db: Session, agency_id: int, partner_id: int) -> list[PartnerAlias]:
    return list(
        db.scalars(
            select(PartnerAlias)
            .where(
                PartnerAlias.agency_id == agency_id,
                PartnerAlias.partner_id == partner_id,
                PartnerAlias.status == ALIAS_APPROVED,
            )
            .order_by(PartnerAlias.alias_key, PartnerAlias.id)
        )
    )


def pending(db: Session, agency_id: int) -> list[PartnerAlias]:
    return list(
        db.scalars(
            select(PartnerAlias)
            .where(PartnerAlias.agency_id == agency_id, PartnerAlias.status == ALIAS_PENDING)
            .order_by(PartnerAlias.created_at, PartnerAlias.id)
        )
    )


def rejected(db: Session, agency_id: int) -> list[PartnerAlias]:
    return list(
        db.scalars(
            select(PartnerAlias)
            .where(PartnerAlias.agency_id == agency_id, PartnerAlias.status == ALIAS_REJECTED)
            .order_by(PartnerAlias.alias_key, PartnerAlias.id)
        )
    )


def at_cap(db: Session, agency_id: int) -> bool:
    return pending_count(db, agency_id) >= MAX_PENDING


def pending_count(db: Session, agency_id: int) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(PartnerAlias)
            .where(PartnerAlias.agency_id == agency_id, PartnerAlias.status == ALIAS_PENDING)
        )
        or 0
    )


# --- proposing ----------------------------------------------------------------------------------


def _is_partner_name(db: Session, agency_id: int, key: str) -> bool:
    names = db.scalars(select(Partner.name).where(Partner.agency_id == agency_id))
    return any(normalise_key(name) == key for name in names)


def _partner(db: Session, agency_id: int, partner_id: int) -> Partner:
    partner = partners.get_partner(db, agency_id, partner_id)
    if partner is None:
        raise AliasError(PARTNER_NOT_FOUND)
    if not partner.is_active:
        raise AliasError(PARTNER_INACTIVE)
    return partner


def propose(
    db: Session,
    actor: User,
    partner_id: int,
    text: str,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> PartnerAlias | None:
    """Record ``text`` as a pending alias of the partner (the caller commits); audits
    ``alias_proposed``. Returns None when there is nothing to propose: an empty text, BRUGE, a
    text that already is a partner's name, or one already known (pending, approved or rejected).
    AliasError for a partner that is not an active partner of the actor's agency."""
    agency_id = actor.agency_id
    _partner(db, agency_id, partner_id)
    alias, key = _clean(text)
    if not key or key.replace(" ", "") == "bruge":
        return None
    if db.scalars(
        select(PartnerAlias.id)
        .where(PartnerAlias.agency_id == agency_id, PartnerAlias.alias_key == key)
        .limit(1)
    ).first():
        return None
    if _is_partner_name(db, agency_id, key) or at_cap(db, agency_id):
        return None
    row = PartnerAlias(
        agency_id=agency_id,
        partner_id=partner_id,
        alias=alias,
        alias_key=key,
        status=ALIAS_PENDING,
        proposed_by_user_id=actor.id,
    )
    if now is not None:
        row.created_at = now
    try:
        with db.begin_nested():  # a lost race on the unique key is "already known"
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        if is_unique_violation(exc):
            return None
        raise
    audit.record(
        db,
        audit.ALIAS_PROPOSED,
        agency_id=agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"alias_id": row.id, "partner_id": partner_id},
        now=now,
    )
    return row


# --- deciding -----------------------------------------------------------------------------------


def _locked(db: Session, actor: User, alias_id: int) -> PartnerAlias:
    row = db.scalars(
        select(PartnerAlias)
        .where(PartnerAlias.agency_id == actor.agency_id, PartnerAlias.id == alias_id)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise AliasNotFound(NOT_FOUND)
    return row


def _decide(
    db: Session,
    actor: User,
    row: PartnerAlias,
    status: str,
    event: str,
    *,
    partner_id: int,
    now: datetime | None,
    ip: str | None,
) -> None:
    row.status = status
    row.partner_id = partner_id
    row.decided_by_user_id = actor.id
    row.decided_at = now or utc_now()
    db.flush()
    audit.record(
        db,
        event,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"alias_id": row.id, "partner_id": partner_id},
        now=now,
    )


def approve(
    db: Session,
    actor: User,
    alias_id: int,
    *,
    partner_id: int | None = None,
    now: datetime | None = None,
    ip: str | None = None,
) -> PartnerAlias:
    """Approve a pending alias, for its own partner or (``partner_id``) another active one."""
    row = _locked(db, actor, alias_id)
    if row.status != ALIAS_PENDING:
        raise AliasError(NOT_PENDING)
    target = _partner(db, actor.agency_id, partner_id if partner_id is not None else row.partner_id)
    db.refresh(target, with_for_update=True)  # the partner cannot be deactivated meanwhile
    if _is_partner_name(db, actor.agency_id, row.alias_key):
        raise AliasError(NAME_IS_PARTNER)  # a partner took this very name since the proposal
    _decide(
        db, actor, row, ALIAS_APPROVED, audit.ALIAS_APPROVED, partner_id=target.id, now=now, ip=ip
    )
    return row


def reject(
    db: Session,
    actor: User,
    alias_id: int,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> PartnerAlias:
    """Reject a pending alias: the text is never proposed again."""
    row = _locked(db, actor, alias_id)
    if row.status != ALIAS_PENDING:
        raise AliasError(NOT_PENDING)
    _decide(
        db,
        actor,
        row,
        ALIAS_REJECTED,
        audit.ALIAS_REJECTED,
        partner_id=row.partner_id,
        now=now,
        ip=ip,
    )
    return row


def reactivate(
    db: Session,
    actor: User,
    alias_id: int,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> PartnerAlias:
    """Put a rejected alias back to pending ; audits ``alias_reactivated``."""
    row = _locked(db, actor, alias_id)
    if row.status != ALIAS_REJECTED:
        raise AliasError(NOT_REJECTED)
    row.status = ALIAS_PENDING
    row.decided_by_user_id = None
    row.decided_at = None
    db.flush()
    audit.record(
        db,
        audit.ALIAS_REACTIVATED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"alias_id": row.id, "partner_id": row.partner_id},
        now=now,
    )
    return row


def remove(
    db: Session,
    actor: User,
    alias_id: int,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> PartnerAlias:
    """Take an approved alias out of use: it becomes rejected (the row is kept)."""
    row = _locked(db, actor, alias_id)
    if row.status != ALIAS_APPROVED:
        raise AliasError(NOT_APPROVED)
    _decide(
        db,
        actor,
        row,
        ALIAS_REJECTED,
        audit.ALIAS_REJECTED,
        partner_id=row.partner_id,
        now=now,
        ip=ip,
    )
    return row


__all__ = [
    "AliasError",
    "AliasNotFound",
    "PartnerAlias",
    "approve",
    "approved_for",
    "approved_pairs",
    "at_cap",
    "pending",
    "pending_count",
    "propose",
    "reactivate",
    "reject",
    "rejected",
    "remove",
]
