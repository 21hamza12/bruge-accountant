"""The switch-over to partner accounts (phase F, slice F5): each partner's opening balance, and
the check that the first half-month matches the partner's spreadsheet.

**The opening balance** is one ``OPENING`` line per partner (migration 0017's partial unique
index), written once by an active manager (``permissions.can_enter_partner_opening``) and never
edited: the three tables are append-only, and a mistake is corrected by an ``AJUSTEMENT``. Its
date is the switch-over day (today by default, never in the future) and its amount is the
spreadsheet's balance at the end of the day before, positive when the partner owes the agency.
From that day on the account reads nothing dated earlier (``app.partner_book``): the sales,
lines and payments before it are in the spreadsheet's balance already.

**No rewriting of history.** The opening is refused when the partner already has one, and when
any period of the partner is closed: a closed period's opening balance, totals and snapshot are
frozen and its statement may have been sent, so an opening entered afterwards would change what
the account says about the past (whatever its date: before the closed period it would shift
every frozen balance, inside or after it every line, payment or sale counted there). The check
runs under the EXCLUSIVE advisory lock of migration 0017 (Postgres), the one the close takes, so
no period closes and no line is written for that partner between the check and the insert. The
database refuses a second ``OPENING`` (unique index) and an ``OPENING`` dated in a closed period
(the insert trigger) on its own as well.

**Double clicks.** The form's ``request_key`` is stored on the line: the same form posted again
returns the stored line; the same key with another amount, sign, date or partner is refused
(``StaleRequestKey``) and the form comes back with a fresh key.

**Audit** ``partner_opening_entered``: the partner's and the line's ids and the amount, never a
name or the reason. A refused attempt by someone else is a ``permission_denied`` event.

**The check** (« Vérification du passage », manager and accountant): for every partner with an
opening, the app's balance at the end of the half-month holding the switch-over day, and the
difference with the balance typed from the spreadsheet for that date. The typed value is never
stored nor audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, money, permissions, reasons
from app.clock import utc_now
from app.formatting import date_fr, money_fr
from app.models import ENTRY_OPENING, ENTRY_SOURCE_MANUAL, Partner, PartnerEntry, User
from app.partner_book import (
    KEY_REUSED,
    KEY_USED_ELSEWHERE,
    AccountConflict,
    AccountForbidden,
    AccountNotFound,
    PartnerAccountError,
    StaleRequestKey,
    Written,
    accounts_of,
    check_request_key,
    get_partner,
    latest_closed_end,
    lock_partner,
    opening_entry,
    parse_day,
)
from app.partner_book import _insert as insert_once
from app.partner_ledger import check_entry_amount, check_entry_source

OPENING_ENTERED = audit.PARTNER_OPENING_ENTERED

SIGN_PARTNER_OWES = "partenaire_doit"  # positive: the partner owes the agency
SIGN_AGENCY_OWES = "agence_doit"  # negative: the agency owes the partner
SIGN_LABELS = {
    SIGN_PARTNER_OWES: "Le partenaire doit à l'agence",
    SIGN_AGENCY_OWES: "L'agence doit au partenaire",
}

NOT_MANAGER = "Seul le gérant saisit le solde d'ouverture d'un partenaire."
NO_CHECK = "Seuls le gérant et le comptable vérifient le passage."
INACTIVE_ACTOR = "Votre compte est désactivé."
NOT_FOUND = "Compte partenaire introuvable."
BAD_SIGN = "Indiquez qui doit à qui : le partenaire à l'agence, ou l'agence au partenaire."
FUTURE_DATE = "La date du passage ne peut pas être dans le futur."
ALREADY_OPENED = (
    "Ce partenaire a déjà un solde d'ouverture (au {day}) : il ne se modifie pas. Une erreur "
    "se corrige par un ajustement."
)
PERIOD_ALREADY_CLOSED = (
    "Une période de ce partenaire est déjà clôturée (jusqu'au {end}) : un solde d'ouverture "
    "changerait ce qui a été arrêté et envoyé. Corrigez l'écart par un ajustement."
)


class OpeningError(PartnerAccountError):
    """A mistake in the opening form or the comparison (a 400); ``str(error)`` is French."""


# --- entering the opening balance -------------------------------------------------------------


def signed_amount(raw: str, sign: str) -> Decimal:
    """The typed amount (always positive, ``app.money``) with the sign chosen; zero needs none."""
    try:
        amount = money.parse_amount(raw)
    except money.MoneyError as exc:
        raise OpeningError(str(exc)) from None
    if amount == 0:
        return amount
    if sign not in SIGN_LABELS:
        raise OpeningError(BAD_SIGN)
    return -amount if sign == SIGN_AGENCY_OWES else amount


def _switch_day(raw: str, today: date) -> date:
    try:
        day = parse_day(raw, today)
    except PartnerAccountError as exc:
        raise OpeningError(str(exc)) from None
    if day > today:
        raise OpeningError(FUTURE_DATE)
    return day


def _require_manager(db: Session, actor: User, now: datetime, ip: str | None) -> None:
    message = None
    if not permissions.can_enter_partner_opening(actor):
        message = NOT_MANAGER
    elif not actor.is_active:
        message = INACTIVE_ACTOR
    if message is not None:
        audit.refused(
            db,
            "enter_partner_opening",
            agency_id=actor.agency_id,
            actor_user_id=actor.id,
            ip=ip,
            now=now,
        )
        raise AccountForbidden(message)


def _entry_by_key(db: Session, agency_id: int, key: str) -> PartnerEntry | None:
    return db.scalars(
        select(PartnerEntry)
        .where(PartnerEntry.agency_id == agency_id, PartnerEntry.request_key == key)
        .execution_options(populate_existing=True)
    ).one_or_none()


def _refuse_rewrite(db: Session, agency_id: int, partner_id: int) -> None:
    """An opening exists already, or a closed period: the opening would rewrite history."""
    existing = opening_entry(db, agency_id, partner_id)
    if existing is not None:
        raise AccountConflict(ALREADY_OPENED.format(day=date_fr(existing.entry_date)))
    closed_end = latest_closed_end(db, agency_id, partner_id)
    if closed_end is not None:
        raise AccountConflict(PERIOD_ALREADY_CLOSED.format(end=date_fr(closed_end)))


def enter_opening(
    db: Session,
    actor: User,
    partner_id: int,
    *,
    amount: str,
    sign: str,
    reason: str,
    request_key: str,
    switch_date: str = "",
    now: datetime | None = None,
    ip: str | None = None,
) -> Written:
    """Write the partner's ``OPENING`` line (see the module's docstring). Never commits, except
    the audit row of a refusal; after an error the caller rolls back."""
    moment = now or utc_now()
    _require_manager(db, actor, moment, ip)
    try:
        key = check_request_key(request_key)
    except PartnerAccountError as exc:
        raise OpeningError(str(exc)) from None
    partner = get_partner(db, actor.agency_id, partner_id)
    if partner is None:
        raise AccountNotFound(NOT_FOUND)
    signed = check_entry_amount(ENTRY_OPENING, signed_amount(amount, sign))
    today = moment.date()
    day = _switch_day(switch_date, today)

    def same(row: PartnerEntry) -> bool:
        return (row.partner_id, row.kind, row.amount, row.entry_date) == (
            partner.id,
            ENTRY_OPENING,
            signed,
            day,
        )

    stored = _entry_by_key(db, actor.agency_id, key)
    if stored is not None:
        if stored.partner_id != partner.id:
            raise StaleRequestKey(KEY_USED_ELSEWHERE)
        if not same(stored):
            raise StaleRequestKey(KEY_REUSED)
        return Written(stored, False)
    try:
        clean = reasons.required_reason(reason)
    except reasons.ReasonError as exc:
        raise OpeningError(str(exc)) from None
    # The close's EXCLUSIVE lock: no period closes and no line lands while we look.
    lock_partner(db, partner.id, shared=False)
    _refuse_rewrite(db, actor.agency_id, partner.id)
    row = PartnerEntry(
        agency_id=actor.agency_id,
        partner_id=partner.id,
        entry_date=day,
        kind=ENTRY_OPENING,
        amount=signed,
        reason=clean,
        source=check_entry_source(ENTRY_OPENING, ENTRY_SOURCE_MANUAL),
        created_by_user_id=actor.id,
        request_key=key,
        created_at=moment,
    )
    try:
        written, created = insert_once(
            db, row, lambda: _entry_by_key(db, actor.agency_id, key), same, "partner_entries"
        )
    except AccountConflict:
        # Another opening won the unique index (or a period closed): say which, in French.
        _refuse_rewrite(db, actor.agency_id, partner.id)
        raise
    if not created:
        return Written(written, False)
    audit.record(
        db,
        OPENING_ENTERED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"partner_id": partner.id, "entry_id": written.id, "amount": str(signed)},
        now=moment,
    )
    return Written(written, True)


def without_opening(db: Session, actor: User) -> list[Partner]:
    """The active partners of the actor's agency that have no opening balance yet, by name."""
    if not permissions.can_enter_partner_opening(actor):
        raise AccountForbidden(NOT_MANAGER)
    opened = select(PartnerEntry.partner_id).where(
        PartnerEntry.agency_id == actor.agency_id, PartnerEntry.kind == ENTRY_OPENING
    )
    return list(
        db.scalars(
            select(Partner)
            .where(
                Partner.agency_id == actor.agency_id,
                Partner.is_active.is_(True),
                Partner.id.not_in(opened),
            )
            .order_by(Partner.name_key, Partner.id)
        )
    )


# --- the switch-over check --------------------------------------------------------------------


@dataclass(frozen=True)
class CheckRow:
    """One partner with an opening: the first half-month (the one holding the switch-over day)
    and the app's balance at its end. ``provisional`` while that period is not closed."""

    partner: Partner
    opening: PartnerEntry
    start: date
    end: date
    app_balance: Decimal
    provisional: bool


def check_rows(db: Session, actor: User, today: date) -> list[CheckRow]:
    """Every partner of the actor's agency with an opening balance, by name: a constant number
    of queries (``partner_book.accounts_of``)."""
    if not permissions.can_check_partner_switchover(actor):
        raise AccountForbidden(NO_CHECK)
    found = list(
        db.scalars(
            select(Partner)
            .join(
                PartnerEntry,
                (PartnerEntry.agency_id == Partner.agency_id)
                & (PartnerEntry.partner_id == Partner.id),
            )
            .where(Partner.agency_id == actor.agency_id, PartnerEntry.kind == ENTRY_OPENING)
            .order_by(Partner.name_key, Partner.id)
        )
    )
    rows: list[CheckRow] = []
    for acc in accounts_of(db, actor.agency_id, found, today):
        if acc.opening_entry is None or not acc.periods:  # pragma: no cover - joined above
            continue
        first = acc.periods[0]
        rows.append(
            CheckRow(
                acc.partner,
                acc.opening_entry,
                first.start,
                first.end,
                first.closing,
                not first.closed,
            )
        )
    return rows


@dataclass(frozen=True)
class Comparison:
    """The spreadsheet's balance as typed, and the app's minus it."""

    excel: Decimal
    difference: Decimal
    currency: str

    @property
    def same(self) -> bool:
        return self.difference == 0

    @property
    def label(self) -> str:
        if self.same:
            return "Identique"
        return f"Écart de {money_fr(abs(self.difference))} {self.currency}"

    @property
    def explanation(self) -> str:
        if self.same:
            return "Le solde de l'application est celui du fichier."
        more = "plus" if self.difference > 0 else "moins"
        return (
            f"L'application compte {money_fr(abs(self.difference))} {self.currency} de {more} "
            "que le fichier du partenaire."
        )


def compare(app_balance: Decimal, typed: str, sign: str, *, currency: str) -> Comparison:
    """The difference between the app's balance and the one typed from the spreadsheet
    (positive when the partner owes the agency, like the app's)."""
    excel = signed_amount(typed, sign)
    return Comparison(excel, app_balance - excel, currency)


__all__ = [
    "SIGN_AGENCY_OWES",
    "SIGN_LABELS",
    "SIGN_PARTNER_OWES",
    "CheckRow",
    "Comparison",
    "OpeningError",
    "check_rows",
    "compare",
    "enter_opening",
    "signed_amount",
    "without_opening",
]
