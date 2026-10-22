"""Partners (agencies that buy from this agency) and their fee history.

Every function filters on the agency explicitly, on top of row-level security (SQLite has none,
and a forgotten filter must not become an IDOR there either). A partner of another agency is
"not found": the routes answer 404, the service raises :class:`PartnerError`.

* Names: ``clean_text``, whitespace collapsed, 1 to 120 characters. ``name_key`` (NFKC,
  casefolded, whitespace collapsed, at most 480 characters) is unique per agency, so "Badi",
  " BADI " and "BA  DI" are the same partner as "badi" (only the case and the runs of spaces
  differ; "ba di" with a space is another name). ``bruge`` is refused: AG = BRUGE means a direct
  sale, not a partner.
* Fees: an append-only history. ``fee_on(day)`` is the entry with the largest ``valid_from`` on or
  before the day; on a tie the latest ``created_at``, then the highest id. A fee can be dated in
  the past or the future. Amounts are ``Decimal`` (see ``app.money``), never ``float``.
* Deactivating keeps the row and its history (and the name stays taken); a deactivated partner can
  be neither renamed nor given a new fee until it is reactivated. Writers lock the partner row
  (``FOR UPDATE``, a no-op on SQLite) before checking its state, so two requests cannot race.

Errors the user can fix are :class:`PartnerError` with a French message. Audit details carry ids,
the fee and its date, never the name or the phone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit
from app.accounts import is_unique_violation
from app.auth.phone import InvalidPhoneError, normalize_phone
from app.errors import UserError
from app.models import Partner, PartnerFee, User
from app.money import MoneyError, to_amount
from app.parsing.amadeus import clean_text

MAX_NAME_LENGTH = 120
MAX_NAME_KEY_LENGTH = 480  # the width of partners.name_key: NFKC + casefold can expand a name
MAX_NOTE_LENGTH = 500
DIRECT_SALE_KEY = "bruge"

NAME_REQUIRED = "Le nom du partenaire est obligatoire."
NAME_TOO_LONG = f"Le nom ne peut pas dépasser {MAX_NAME_LENGTH} caractères."
NAME_IS_DIRECT_SALE = "Ce nom est réservé : BRUGE désigne une vente directe, pas un partenaire."
NAME_TAKEN = "Un partenaire portant ce nom existe déjà."
NOT_FOUND = "Partenaire introuvable."
INACTIVE = "Ce partenaire est désactivé."
ALREADY_INACTIVE = "Ce partenaire est déjà désactivé."
ALREADY_ACTIVE = "Ce partenaire est déjà actif."
BAD_DATE = "Saisissez une date valide (AAAA-MM-JJ)."
DATE_REQUIRED = "Saisissez la date à partir de laquelle ce tarif s'applique."
NOTE_TOO_LONG = f"La note ne peut pas dépasser {MAX_NOTE_LENGTH} caractères."
BAD_TEXT = "Texte invalide."

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
# Newest first: the one order used to pick "the fee valid on a day" everywhere.
_NEWEST = (PartnerFee.valid_from.desc(), PartnerFee.created_at.desc(), PartnerFee.id.desc())


class PartnerError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


@dataclass(frozen=True)
class PartnerRow:
    partner: Partner
    current_fee: PartnerFee | None  # the fee valid today, None when there is none yet


# --- normalising --------------------------------------------------------------------------------


def _collapse(text: str) -> str:
    return " ".join(text.split())


def clean_name(raw: str) -> tuple[str, str]:
    """``(name, name_key)`` for a typed name, or PartnerError (empty, too long, a key too long
    once expanded, or ``bruge``)."""
    if not isinstance(raw, str):
        raise PartnerError(NAME_REQUIRED)
    name = _collapse(unicodedata.normalize("NFC", clean_text(raw)))
    if not name:
        raise PartnerError(NAME_REQUIRED)
    if len(name) > MAX_NAME_LENGTH:
        raise PartnerError(NAME_TOO_LONG)
    key = _collapse(unicodedata.normalize("NFKC", name).casefold())
    if len(key) > MAX_NAME_KEY_LENGTH:  # e.g. 120 x U+FDFA expands to 2160 characters
        raise PartnerError(NAME_TOO_LONG)
    if key == DIRECT_SALE_KEY:
        raise PartnerError(NAME_IS_DIRECT_SALE)
    return name, key


def _clean_phone(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PartnerError(BAD_TEXT)
    if not raw.strip():
        return None
    try:
        return normalize_phone(raw)
    except InvalidPhoneError as exc:
        raise PartnerError(str(exc)) from None


def _clean_note(raw: str | None) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PartnerError(BAD_TEXT)
    note = _collapse(clean_text(raw))
    if len(note) > MAX_NOTE_LENGTH:
        raise PartnerError(NOTE_TOO_LONG)
    return note or None


def _clean_fee(raw: Decimal | int | str) -> Decimal:
    try:
        return to_amount(raw)
    except MoneyError as exc:
        raise PartnerError(str(exc)) from None


def parse_valid_from(raw: str) -> date:
    """The date typed in a form (``YYYY-MM-DD`` only); PartnerError for an empty or bad one."""
    if not isinstance(raw, str) or not raw.strip():
        raise PartnerError(DATE_REQUIRED)
    text = raw.strip()
    if _ISO_DATE.fullmatch(text) is None:
        raise PartnerError(BAD_DATE)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PartnerError(BAD_DATE) from None


# --- reading ------------------------------------------------------------------------------------


def get_partner(db: Session, agency_id: int, partner_id: int) -> Partner | None:
    """The partner of this agency, or None (another agency's id is "not found")."""
    return db.scalars(
        select(Partner).where(Partner.agency_id == agency_id, Partner.id == partner_id)
    ).one_or_none()


def list_active(db: Session, agency_id: int) -> list[Partner]:
    """The agency's active partners by name (what a sale can name; no fee lookup)."""
    return list(
        db.scalars(
            select(Partner)
            .where(Partner.agency_id == agency_id, Partner.is_active.is_(True))
            .order_by(Partner.name_key, Partner.id)
        )
    )


def _newest_first(statement: Select[PartnerFee]) -> Select[PartnerFee]:
    return statement.order_by(*_NEWEST)


def _fees_of(agency_id: int, partner_id: int) -> Select[PartnerFee]:
    return select(PartnerFee).where(
        PartnerFee.agency_id == agency_id, PartnerFee.partner_id == partner_id
    )


def fee_history(db: Session, agency_id: int, partner_id: int) -> list[PartnerFee]:
    """Every fee entry of the partner, newest ``valid_from`` first."""
    return list(db.scalars(_newest_first(_fees_of(agency_id, partner_id))))


def fee_on(db: Session, agency_id: int, partner_id: int, day: date) -> PartnerFee | None:
    """The fee valid on ``day``: the entry with the largest ``valid_from <= day`` (ties: latest
    ``created_at``, then highest id); None before the first fee."""
    query = _newest_first(_fees_of(agency_id, partner_id).where(PartnerFee.valid_from <= day))
    return db.scalars(query.limit(1)).first()


def list_partners(db: Session, agency_id: int, *, today: date) -> list[PartnerRow]:
    """The agency's partners (active first, then by name) with the fee valid on ``today``
    (one query picks each partner's newest applicable entry with a window function)."""
    found = list(
        db.scalars(
            select(Partner)
            .where(Partner.agency_id == agency_id)
            .order_by(Partner.is_active.desc(), Partner.name_key, Partner.id)
        )
    )
    ranked = (
        select(
            PartnerFee.id.label("fee_id"),
            func.row_number().over(partition_by=PartnerFee.partner_id, order_by=_NEWEST).label("n"),
        )
        .where(PartnerFee.agency_id == agency_id, PartnerFee.valid_from <= today)
        .subquery()
    )
    current = {
        fee.partner_id: fee
        for fee in db.scalars(
            select(PartnerFee).join(ranked, ranked.c.fee_id == PartnerFee.id).where(ranked.c.n == 1)
        )
    }
    return [PartnerRow(p, current.get(p.id)) for p in found]


# --- writing ------------------------------------------------------------------------------------


def _lock(db: Session, actor: User, partner: Partner) -> None:
    """Check the partner is the actor's agency's, then lock its row and reload it, so the state
    checks that follow see what a concurrent request committed."""
    if partner.agency_id != actor.agency_id:
        raise PartnerError(NOT_FOUND)
    db.refresh(partner, with_for_update=True)


def _active(partner: Partner) -> None:
    if not partner.is_active:
        raise PartnerError(INACTIVE)


def _taken(db: Session, agency_id: int, key: str, *, except_id: int | None = None) -> bool:
    query = select(Partner.id).where(Partner.agency_id == agency_id, Partner.name_key == key)
    if except_id is not None:
        query = query.where(Partner.id != except_id)
    return db.scalars(query.limit(1)).first() is not None


def _flush_unique(db: Session) -> None:
    """Flush; a unique violation (two requests creating the same name at once) is the same
    mistake as the duplicate we look for beforehand."""
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        if is_unique_violation(exc):
            raise PartnerError(NAME_TAKEN) from None
        raise


def create_partner(
    db: Session,
    actor: User,
    name: str,
    phone: str | None = None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> Partner:
    """Add a partner to the actor's agency (the caller commits); audits ``partner_created``."""
    clean, key = clean_name(name)
    normalized_phone = _clean_phone(phone)
    if _taken(db, actor.agency_id, key):
        raise PartnerError(NAME_TAKEN)
    partner = Partner(
        agency_id=actor.agency_id, name=clean, name_key=key, phone=normalized_phone, is_active=True
    )
    if now is not None:
        partner.created_at = partner.updated_at = now
    db.add(partner)
    _flush_unique(db)
    audit.record(
        db,
        audit.PARTNER_CREATED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"partner_id": partner.id},
        now=now,
    )
    return partner


def update_partner(
    db: Session,
    actor: User,
    partner: Partner,
    *,
    name: str,
    phone: str | None,
    now: datetime | None = None,
    ip: str | None = None,
) -> Partner:
    """Change the name and phone of an active partner; no event when nothing changed."""
    _lock(db, actor, partner)
    _active(partner)
    clean, key = clean_name(name)
    normalized_phone = _clean_phone(phone)
    if _taken(db, actor.agency_id, key, except_id=partner.id):
        raise PartnerError(NAME_TAKEN)
    changed = []
    if clean != partner.name:
        changed.append("name")
    if normalized_phone != partner.phone:
        changed.append("phone")
    if not changed:
        return partner
    partner.name, partner.name_key, partner.phone = clean, key, normalized_phone
    if now is not None:
        partner.updated_at = now
    _flush_unique(db)
    audit.record(
        db,
        audit.PARTNER_UPDATED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"partner_id": partner.id, "changed": changed},
        now=now,
    )
    return partner


def set_fee(
    db: Session,
    partner: Partner,
    fee: Decimal | int | str,
    valid_from: date,
    actor: User,
    note: str | None = None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> PartnerFee:
    """Append a fee entry: ``fee`` applies from ``valid_from`` (past or future) until a later
    entry takes over. ``fee`` is a Decimal (or int), or text typed by a person; never a float."""
    _lock(db, actor, partner)
    _active(partner)
    amount = _clean_fee(fee)
    if isinstance(valid_from, datetime) or not isinstance(valid_from, date):
        raise PartnerError(BAD_DATE)
    clean_note = _clean_note(note)
    entry = PartnerFee(
        agency_id=actor.agency_id,
        partner_id=partner.id,
        fee=amount,
        valid_from=valid_from,
        set_by_user_id=actor.id,
        note=clean_note,
    )
    if now is not None:
        entry.created_at = now
    db.add(entry)
    db.flush()
    audit.record(
        db,
        audit.PARTNER_FEE_SET,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "partner_id": partner.id,
            "fee_id": entry.id,
            "fee": str(amount),
            "valid_from": valid_from.isoformat(),
        },
        now=now,
    )
    return entry


def _set_active(
    db: Session,
    actor: User,
    partner: Partner,
    *,
    active: bool,
    event: str,
    already: str,
    now: datetime | None,
    ip: str | None,
) -> None:
    _lock(db, actor, partner)
    if partner.is_active == active:
        raise PartnerError(already)
    partner.is_active = active
    if now is not None:
        partner.updated_at = now
    db.flush()
    audit.record(
        db,
        event,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"partner_id": partner.id},
        now=now,
    )


def deactivate_partner(
    db: Session,
    actor: User,
    partner: Partner,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    """Deactivate (history and name kept); PartnerError when it already is."""
    _set_active(
        db,
        actor,
        partner,
        active=False,
        event=audit.PARTNER_DEACTIVATED,
        already=ALREADY_INACTIVE,
        now=now,
        ip=ip,
    )


def reactivate_partner(
    db: Session,
    actor: User,
    partner: Partner,
    *,
    now: datetime | None = None,
    ip: str | None = None,
) -> None:
    """Undo a deactivation; PartnerError when the partner is already active."""
    _set_active(
        db,
        actor,
        partner,
        active=True,
        event=audit.PARTNER_REACTIVATED,
        already=ALREADY_ACTIVE,
        now=now,
        ip=ip,
    )
