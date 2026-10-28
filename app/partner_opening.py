"""The switch-over to partner accounts (phase F, slice F5): each partner's opening balance, and
the check that the first half-month matches the partner's spreadsheet.

**The opening balance** is one ``OPENING`` line per partner (migration 0017's partial unique
index), written once by an active manager (``permissions.can_enter_partner_opening``) for an
active partner, and never edited: the three tables are append-only, and a mistake is corrected
by an ``AJUSTEMENT``. Its date is the switch-over day: typed (never defaulted here), never in the
future, never more than ``MAX_DAYS_BACK`` days ago nor before ``models.PARTNER_DATE_FLOOR`` (a
typo such as 2002 would make every account page walk hundreds of half-months; migration 0019
refuses such dates in the database too). Its amount is the spreadsheet's balance at the end of
the day before, positive when the partner owes the agency; a zero balance needs its own « solde
nul » tick. From that day on the account reads nothing dated earlier (``app.partner_book``).

**What stops counting** (``dropped_before``): the sales, the lines (pending ones included) and
the payments already in the app and dated before the switch-over day. When there are any, the
first post raises ``NeedsConfirmation`` with the list, their net effect on the balance and a
fingerprint; the second must tick « J'ai vérifié que ces éléments sont dans le solde du fichier
Excel » and carry that fingerprint. The list is counted again under the lock at insert time: if
it changed, the opening is refused again with the new list. When nothing would stop counting,
one post is enough.

**No rewriting of history.** The opening is refused when the partner already has one, and when
any period of the partner is closed: a closed period's opening balance, totals and snapshot are
frozen and its statement may have been sent, so an opening entered afterwards would change what
the account says about the past (whatever its date: before the closed period it would shift
every frozen balance, inside or after it every line, payment or sale counted there). The checks
run under the EXCLUSIVE advisory lock of migration 0017 (Postgres), the one the close takes, so
no period closes and no line is written for that partner between the check and the insert. The
database refuses a second ``OPENING`` (unique index) and an ``OPENING`` dated in a closed period
(the insert trigger) on its own as well.

**Double clicks.** The form's ``request_key`` is stored on the line: the same form posted again
returns the stored line; the same key with another amount, sign, date or partner is refused
(``StaleRequestKey``) and the form comes back with a fresh key. The key is looked up again once
the lock is held, so a second post racing the first gets the stored line, not a refusal.

**Audit** ``partner_opening_entered``: the partner's and the line's ids, the amount, and how
many lines, payments and sales stopped counting with their net sum (numbers only), never a name
or the reason. A refused attempt by someone else is a ``permission_denied`` event.

**The check** (« Vérification du passage », manager and accountant): for every partner with an
opening, the app's balance at the end of the half-month holding the switch-over day, and the
difference with the balance typed from the spreadsheet for that date. The typed value is never
stored nor audited.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, money, permissions, reasons
from app.clock import utc_now
from app.formatting import date_fr, money_fr
from app.models import (
    ENTRY_OPENING,
    ENTRY_SOURCE_MANUAL,
    PARTNER_DATE_FLOOR,
    Partner,
    PartnerEntry,
    PartnerPayment,
    User,
)
from app.partner_accounts import PartnerSaleLine, partner_sale_lines
from app.partner_book import (
    KEY_REUSED,
    KEY_USED_ELSEWHERE,
    AccountConflict,
    AccountForbidden,
    AccountNotFound,
    EntryView,
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
from app.partner_book import _entries as entries_between
from app.partner_book import _insert as insert_once
from app.partner_book import _payments as payments_between
from app.partner_ledger import ZERO, check_entry_amount, check_entry_source

OPENING_ENTERED = audit.PARTNER_OPENING_ENTERED
MAX_DAYS_BACK = 400  # the oldest switch-over day accepted, counted back from today
DATE_FLOOR = date.fromisoformat(PARTNER_DATE_FLOOR)

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
PARTNER_INACTIVE = (
    "Ce partenaire est désactivé : réactivez-le avant de saisir son solde d'ouverture."
)
BAD_SIGN = "Indiquez qui doit à qui : le partenaire à l'agence, ou l'agence au partenaire."
ZERO_NOT_TICKED = "Pour un solde de zéro, cochez « Solde nul »."
ZERO_BUT_AMOUNT = (
    "« Solde nul » est coché mais le montant n'est pas zéro : corrigez l'un ou l'autre."
)
NO_DATE = "Saisissez la date du passage."
FUTURE_DATE = "La date du passage ne peut pas être dans le futur."
TOO_OLD = (
    "La date du passage ne peut pas remonter à plus de {days} jours : au plus tôt le {day}. "
    "Vérifiez l'année."
)
ALREADY_OPENED = (
    "Ce partenaire a déjà un solde d'ouverture (au {day}) : il ne se modifie pas. Une erreur "
    "se corrige par un ajustement."
)
PERIOD_ALREADY_CLOSED = (
    "Une période de ce partenaire est déjà clôturée (jusqu'au {end}) : un solde d'ouverture "
    "changerait ce qui a été arrêté et envoyé. Corrigez l'écart par un ajustement."
)
CONFIRM_NEEDED = (
    "Vérifiez la liste ci-dessous, puis cochez « J'ai vérifié que ces éléments sont dans le "
    "solde du fichier Excel » pour confirmer."
)
PREVIEW_CHANGED = (
    "Le compte a changé depuis l'aperçu : vérifiez la nouvelle liste, puis confirmez de nouveau."
)


class OpeningError(PartnerAccountError):
    """A mistake in the opening form or the comparison (a 400); ``str(error)`` is French."""


# --- what stops counting ----------------------------------------------------------------------


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'s' if count > 1 else ''}"


@dataclass(frozen=True)
class Dropped:
    """What the account holds dated before ``before`` and will no longer count once the opening
    is written: sales, lines (pending ones too) and payments."""

    before: date
    sales: tuple[PartnerSaleLine, ...] = ()
    lines: tuple[EntryView, ...] = ()
    payments: tuple[PartnerPayment, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.sales or self.lines or self.payments)

    @property
    def counted_sum(self) -> Decimal:
        """What these items add to the balance today (sales + counted lines - payments)."""
        total = sum((s.pb for s in self.sales), ZERO)
        total += sum((v.entry.amount for v in self.lines if v.counted), ZERO)
        total -= sum((p.amount for p in self.payments), ZERO)
        return total

    @property
    def effect(self) -> Decimal:
        """The change to the balance once they stop counting."""
        return -self.counted_sum

    @property
    def fingerprint(self) -> str:
        """Stands for exactly this list: the counts, the sum and a digest of the ids."""
        ids = "|".join(
            ",".join(str(i) for i in sorted(group))
            for group in (
                [v.entry.id for v in self.lines],
                [p.id for p in self.payments],
                [s.sale_id for s in self.sales],
            )
        )
        digest = hashlib.sha256(f"{self.before.isoformat()}#{ids}".encode()).hexdigest()[:16]
        return (
            f"{len(self.lines)}.{len(self.payments)}.{len(self.sales)}.{self.counted_sum}.{digest}"
        )

    def summary(self, currency: str) -> str:
        return (
            f"{_plural(len(self.lines), 'ligne')}, {_plural(len(self.payments), 'paiement')} et "
            f"{_plural(len(self.sales), 'vente')} datés avant le {self.before:%d/%m} ne seront "
            f"plus comptés (effet net : {money_fr(self.effect)} {currency})"
        )


def dropped_before(db: Session, agency_id: int, partner_id: int, day: date) -> Dropped:
    """The sales, lines (not the opening) and payments of the partner dated before ``day``."""
    last = day - timedelta(days=1)
    return Dropped(
        day,
        tuple(partner_sale_lines(db, agency_id, partner_id, date.min, last)),
        tuple(entries_between(db, agency_id, partner_id, date.min, last)),
        tuple(payments_between(db, agency_id, partner_id, date.min, last)),
    )


class NeedsConfirmation(OpeningError):
    """Items would stop counting: show them and ask for the tick. ``changed`` when the list is
    not the one the user confirmed."""

    def __init__(self, dropped: Dropped, *, changed: bool) -> None:
        super().__init__(PREVIEW_CHANGED if changed else CONFIRM_NEEDED)
        self.dropped = dropped
        self.changed = changed


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


def _opening_amount(raw: str, sign: str, zero_balance: bool) -> Decimal:
    """The signed opening amount; a zero one only with the « solde nul » tick, and the tick
    only with a zero amount."""
    if zero_balance and not raw.strip():
        raw = "0"
    amount = signed_amount(raw, sign)
    if amount == 0 and not zero_balance:
        raise OpeningError(ZERO_NOT_TICKED)
    if amount != 0 and zero_balance:
        raise OpeningError(ZERO_BUT_AMOUNT)
    return check_entry_amount(ENTRY_OPENING, amount)


def earliest_switch_day(today: date) -> date:
    """The oldest switch-over day accepted: ``MAX_DAYS_BACK`` days ago, never before the floor."""
    return max(today - timedelta(days=MAX_DAYS_BACK), DATE_FLOOR)


def _switch_day(raw: str, today: date) -> date:
    if not isinstance(raw, str) or not raw.strip():
        raise OpeningError(NO_DATE)
    try:
        day = parse_day(raw, today)
    except PartnerAccountError as exc:
        raise OpeningError(str(exc)) from None
    if day > today:
        raise OpeningError(FUTURE_DATE)
    earliest = earliest_switch_day(today)
    if day < earliest:
        raise OpeningError(TOO_OLD.format(days=MAX_DAYS_BACK, day=date_fr(earliest)))
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


def refusal_for(db: Session, partner: Partner) -> str | None:
    """Why no opening may be entered for ``partner`` now (inactive, opened, a period closed),
    or None: the form is then worth showing."""
    if not partner.is_active:
        return PARTNER_INACTIVE
    try:
        _refuse_rewrite(db, partner.agency_id, partner.id)
    except AccountConflict as exc:
        return str(exc)
    return None


def enter_opening(
    db: Session,
    actor: User,
    partner_id: int,
    *,
    amount: str,
    sign: str,
    reason: str,
    request_key: str,
    switch_date: str,
    zero_balance: bool = False,
    confirmed: bool = False,
    acknowledged: str = "",
    now: datetime | None = None,
    ip: str | None = None,
) -> Written:
    """Write the partner's ``OPENING`` line (see the module's docstring). ``confirmed`` is the
    tick and ``acknowledged`` the fingerprint of the list it was given for
    (``NeedsConfirmation``). Never commits, except the audit row of a refusal; after an error
    the caller rolls back."""
    moment = now or utc_now()
    _require_manager(db, actor, moment, ip)
    try:
        key = check_request_key(request_key)
    except PartnerAccountError as exc:
        raise OpeningError(str(exc)) from None
    partner = get_partner(db, actor.agency_id, partner_id)
    if partner is None:
        raise AccountNotFound(NOT_FOUND)
    signed = _opening_amount(amount, sign, zero_balance)
    today = moment.date()
    day = _switch_day(switch_date, today)

    def same(row: PartnerEntry) -> bool:
        return (row.partner_id, row.kind, row.amount, row.entry_date) == (
            partner.id,
            ENTRY_OPENING,
            signed,
            day,
        )

    def replayed() -> Written | None:
        """The row this key already wrote: returned when it is this form, else refused."""
        stored = _entry_by_key(db, actor.agency_id, key)
        if stored is None:
            return None
        if stored.partner_id != partner.id:
            raise StaleRequestKey(KEY_USED_ELSEWHERE)
        if not same(stored):
            raise StaleRequestKey(KEY_REUSED)
        return Written(stored, False)

    found = replayed()
    if found is not None:
        return found
    if not partner.is_active:
        raise AccountConflict(PARTNER_INACTIVE)
    try:
        clean = reasons.required_reason(reason)
    except reasons.ReasonError as exc:
        raise OpeningError(str(exc)) from None
    # The close's EXCLUSIVE lock: no period closes and no line lands while we look.
    lock_partner(db, partner.id, shared=False)
    found = replayed()  # a twin post may have committed while we waited for the lock
    if found is not None:
        return found
    _refuse_rewrite(db, actor.agency_id, partner.id)
    dropped = dropped_before(db, actor.agency_id, partner.id, day)
    if not dropped.empty and not (confirmed and acknowledged == dropped.fingerprint):
        raise NeedsConfirmation(
            dropped, changed=bool(acknowledged) and acknowledged != dropped.fingerprint
        )
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
        details={
            "partner_id": partner.id,
            "entry_id": written.id,
            "amount": str(signed),
            "dropped_lines": len(dropped.lines),
            "dropped_payments": len(dropped.payments),
            "dropped_sales": len(dropped.sales),
            "dropped_sum": str(dropped.counted_sum),
        },
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
    "MAX_DAYS_BACK",
    "SIGN_AGENCY_OWES",
    "SIGN_LABELS",
    "SIGN_PARTNER_OWES",
    "CheckRow",
    "Comparison",
    "Dropped",
    "NeedsConfirmation",
    "OpeningError",
    "check_rows",
    "compare",
    "dropped_before",
    "earliest_switch_day",
    "enter_opening",
    "refusal_for",
    "signed_amount",
    "without_opening",
]
