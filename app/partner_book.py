"""Partner accounts, the application side (phase F, slice F2): the account of one partner, its
manual lines and their decisions, its payments, the close of a period, and the corrections
written when a sale counted in a closed period changes.

**Periods.** A period is a half-month (``partner_ledger.half_month``). They run from the
partner's first period to the one holding today, in order: the first one is the half-month of
the ``OPENING`` line when there is one, else of the earliest closed period or of the earliest
activity (a sale, a line, a payment). A ``partner_periods`` row is only written when a period
closes (built on demand): an open period is computed from the live data every time it is shown,
and a closed one is read from its row and its snapshot, never recomputed. The opening of a
period is the previous period's closing balance, or the ``OPENING`` amount (else 0) for the
first one. Reads defer the snapshot column (only the period page loads it).

**Opening balance.** The ``OPENING`` line's date is the switch-over day and its amount is the
spreadsheet's balance at the end of the day before. Nothing dated before the switch-over day is
read (sales, lines, payments, even inside the first half-month), accepted (``_check_date``) or
corrected (``_covered``): it is in the spreadsheet's balance already.

**Who.** Every write needs an active manager or accountant of the agency
(``permissions.can_validate_money``); an ``AJUSTEMENT`` is approved or rejected only by a manager
(``permissions.can_approve_partner_adjustment``) who did not enter it (four eyes), so one is only
accepted when another active user could approve it (``NO_APPROVER``: a manager alone asks the
accountant to enter it); an ``RFND`` proposed from Amadeus is confirmed or rejected by the
accountant or the manager. A refused attempt is a ``permission_denied`` audit event, committed
before the error is raised.

**Dates.** A line or a payment is dated today or earlier in an open period: never after today,
never on or before the end of the latest closed period (``BACKDATED``: a late line is written
today, its reason naming the old date), never before the opening balance's day. The database
refuses the same thing (migration 0017's triggers, which take a shared advisory lock per
partner); that refusal, if it slips past the check here, is shown as ``PERIOD_JUST_CLOSED``, and
any other refusal by the database as ``DB_REFUSED`` (logged without data), never as a 500.

**Closing** (``close_period``): the EXCLUSIVE ``pg_advisory_xact_lock(170017, partner id folded
into an integer)`` first (Postgres; nothing on SQLite), so every line, payment or decision in
flight has committed and every later one sees the closed period. Then the report days of the
period ``FOR SHARE`` (none may be reopened under us), the period row ``FOR UPDATE`` (inserted
first if it does not exist), and only then the totals, each a fresh statement. Refused when the
period is not over, an earlier period is open, a day of the period is not closed, or a line
waits for a decision. The totals and the snapshot are computed here by
``partner_ledger.compute_period``; the UPDATE must change exactly one row.

**Corrections** (``correct_after_close``, called by ``app.sales`` in the sale's transaction): a
partner sale counted in a closed period that is created, edited, voided, moved to another day or
to another partner writes the difference into the open period as a ``CORRECTION`` dated today
(``source=system``, ``sale_id`` set, and the sale is checked to be one of that partner's). The
shared advisory lock is only taken when a closed period could cover the sale's day.

**Double clicks.** A line and a payment carry the form's ``request_key`` (unique per agency): the
same form posted again returns the row already written (``Written.created`` is False); a key
whose stored row differs (kind, amount, date, method) is refused (``StaleRequestKey``). A close
keeps its key in the snapshot, so the same form posted twice returns the closed period. A
decision is idempotent by its own key (one decision per line): the same decision by the same
person returns the one stored.

**Audit** (ids and amounts only, never a name or a reason): ``partner_entry_added``,
``partner_entry_decided``, ``partner_payment_recorded``, ``partner_period_closed``,
``partner_correction_added``.

Every query filters on the agency explicitly (on top of row-level security). Functions never
commit, except the audit row of a refusal; after an error the caller rolls back.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, and_, cast, func, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from app import audit, money, permissions, reasons
from app.clock import utc_now
from app.errors import UserError
from app.formatting import date_fr
from app.models import (
    AG_PARTNER,
    DAY_CLOSED,
    DECISION_APPROVE,
    DECISION_CONFIRM,
    DECISION_REJECT,
    ENTRY_ADM,
    ENTRY_AJUSTEMENT,
    ENTRY_CHANGEMENT,
    ENTRY_CORRECTION,
    ENTRY_OPENING,
    ENTRY_RFND,
    ENTRY_SOURCE_MANUAL,
    ENTRY_SOURCE_SYSTEM,
    MAX_PAYMENT_REFERENCE,
    MAX_REQUEST_KEY,
    PARTNER_PAYMENT_METHODS,
    PERIOD_CLOSED,
    PERIOD_OPEN,
    SALE_ACTIVE,
    Partner,
    PartnerEntry,
    PartnerEntryDecision,
    PartnerPayment,
    PartnerPeriod,
    ReportDay,
    Sale,
    SaleVersion,
    User,
)
from app.parsing.amadeus import clean_text
from app.partner_accounts import PartnerSaleLine, partner_sale_lines, partner_sale_lines_many
from app.partner_ledger import (
    ALERT_DUE_SOON,
    ALERT_NONE,
    ALERT_OVERDUE,
    ZERO,
    LedgerEntry,
    LedgerPayment,
    PeriodTotals,
    alert_level,
    check_entry_amount,
    check_entry_source,
    compute_period,
    due_date,
    half_month,
    needs_decision,
)
from app.sales_rules import ticket_json

log = logging.getLogger(__name__)

# migration 0017's advisory-lock namespace and the fold of a partner id into an integer.
ADVISORY_NAMESPACE = 170017
ADVISORY_MODULUS = 2147483647
# migration 0017's closed-period triggers raise this text (Postgres: sqlstate 23514,
# check_violation; SQLite: RAISE(ABORT, ...)). tests/test_partner_book.py pins the two equal.
CLOSED_PERIOD_TRIGGER_MESSAGE = "a closed partner period ends on or after this date"
CHECK_VIOLATION = "23514"

MANUAL_KINDS = (ENTRY_ADM, ENTRY_RFND, ENTRY_CHANGEMENT, ENTRY_AJUSTEMENT)
KIND_LABELS = {
    ENTRY_OPENING: "Solde d'ouverture",
    ENTRY_ADM: "ADM (pénalité de la compagnie)",
    ENTRY_RFND: "Remboursement (RFND)",
    ENTRY_CHANGEMENT: "Frais de changement",
    ENTRY_AJUSTEMENT: "Ajustement",
    ENTRY_CORRECTION: "Correction après clôture",
}
METHOD_LABELS = {
    "cash": "Espèces",
    "bankily": "Bankily",
    "masrvi": "Masrvi",
    "sedad": "Sedad",
    "virement": "Virement",
    "cheque": "Chèque",
}
DIRECTION_MORE = "plus"  # an AJUSTEMENT the partner owes on top
DIRECTION_LESS = "moins"  # an AJUSTEMENT in the partner's favour
DIRECTIONS = (DIRECTION_MORE, DIRECTION_LESS)
ACTION_ADDED = "ajoutée"
ACTION_EDITED = "modifiée"
ACTION_VOIDED = "annulée"
ACTION_MOVED = "déplacée"

# Audit events: ids and amounts only.
ENTRY_ADDED = "partner_entry_added"
ENTRY_DECIDED = "partner_entry_decided"
PAYMENT_RECORDED = "partner_payment_recorded"
PERIOD_CLOSED_EVENT = "partner_period_closed"
CORRECTION_ADDED = "partner_correction_added"

NO_PERMISSION = "Seuls le gérant et le comptable gèrent les comptes partenaires."
NOT_MANAGER = "Seul le gérant peut approuver ou rejeter un ajustement."
OWN_LINE = "Vous avez saisi cet ajustement : un autre gérant doit l'approuver ou le rejeter."
NO_APPROVER = (
    "Aucun autre gérant ne peut approuver cette ligne : demandez à la comptable de la saisir, "
    "le gérant l'approuvera."
)
INACTIVE_ACTOR = "Votre compte est désactivé."
NOT_FOUND = "Compte partenaire introuvable."
LINE_NOT_FOUND = "Ligne introuvable."
PERIOD_NOT_FOUND = "Période introuvable."
BAD_KIND = "Choisissez le type de ligne."
BAD_DIRECTION = "Indiquez si l'ajustement augmente ou diminue ce que doit le partenaire."
ZERO_AMOUNT = "Le montant doit être supérieur à zéro."
BAD_DATE = "Saisissez une date valide (AAAA-MM-JJ)."
FUTURE_DATE = "La date ne peut pas être dans le futur."
BACKDATED = (
    "Cette date tombe dans une période déjà clôturée (jusqu'au {end}). Saisissez la ligne à "
    "une date de la période ouverte, par exemple aujourd'hui, et citez l'ancienne date dans "
    "le motif."
)
BEFORE_OPENING = (
    "Cette date est antérieure au solde d'ouverture du partenaire ({start}) : ce qui précède "
    "est déjà compris dans ce solde."
)
BAD_METHOD = "Choisissez un mode de paiement."
BAD_RECEIVER = "Choisissez la personne qui a reçu le paiement parmi les utilisateurs actifs."
REFERENCE_TOO_LONG = f"La référence ne peut pas dépasser {MAX_PAYMENT_REFERENCE} caractères."
BAD_REQUEST_KEY = "Ce formulaire n'est plus valable : rechargez la page et recommencez."
KEY_USED_ELSEWHERE = "Ce formulaire a déjà servi pour un autre compte : rechargez la page."
KEY_REUSED = (
    "Ce formulaire a déjà été envoyé avec d'autres valeurs. Vérifiez la saisie et envoyez-le "
    "de nouveau."
)
NO_DECISION_NEEDED = "Cette ligne n'attend aucune décision."
BAD_DECISION = "Cette décision ne convient pas à cette ligne."
ALREADY_DECIDED = "Cette ligne a déjà été traitée."
ALREADY_CLOSED = "Cette période est déjà clôturée."
NOT_OVER = "La période n'est pas terminée : elle se clôture à partir du {day}."
EARLIER_OPEN = "Clôturez d'abord la période précédente (du {start} au {end})."
DAYS_NOT_CLOSED = "Des journées de la période ne sont pas clôturées : {days}."
LINES_PENDING = "Des lignes attendent encore une décision : traitez-les avant de clôturer."
CLOSE_LOST = "La période a changé pendant la clôture : rechargez la page et recommencez."
PERIOD_JUST_CLOSED = (
    "Enregistrement refusé : la période vient d'être clôturée. Rechargez la page et saisissez "
    "la ligne dans la période ouverte."
)
DB_REFUSED = (
    "Enregistrement refusé : la saisie ne respecte pas une règle du compte. Rechargez la page "
    "et vérifiez la saisie."
)
CORRECTION_REASON = "Vente #{sale} {action} après la clôture de la période du {start} au {end}"

_REQUEST_KEY = re.compile(rf"[A-Za-z0-9_-]{{16,{MAX_REQUEST_KEY}}}", re.ASCII)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


class PartnerAccountError(UserError):
    """A mistake the user can correct, or a refusal; ``str(error)`` is a French message."""


class AccountNotFound(PartnerAccountError):
    """No such partner, line or period in the actor's agency (a 404)."""


class AccountForbidden(PartnerAccountError):
    """The actor may not do this (a 403)."""


class AccountConflict(PartnerAccountError):
    """The state does not allow it now: closed, already decided, lines waiting (a 409)."""


class StaleRequestKey(PartnerAccountError):
    """The form's key was used already, for other values or another account: the form must be
    shown again with a fresh key."""


# --- small helpers ----------------------------------------------------------------------------


def new_request_key() -> str:
    """The hidden ``request_key`` of a new form: 32 URL-safe characters."""
    return secrets.token_urlsafe(24)


def check_request_key(raw: object) -> str:
    if not isinstance(raw, str) or _REQUEST_KEY.fullmatch(raw) is None:
        raise PartnerAccountError(BAD_REQUEST_KEY)
    return raw


def parse_day(raw: object, today: date) -> date:
    """A posted ``AAAA-MM-JJ`` date; an empty field means today."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return today
    if not isinstance(raw, str) or _ISO_DATE.fullmatch(raw.strip()) is None:
        raise PartnerAccountError(BAD_DATE)
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise PartnerAccountError(BAD_DATE) from None


def period_label(start: date, end: date) -> str:
    return f"{start.day} au {date_fr(end)}"


def _next_start(end: date) -> date:
    return end + timedelta(days=1)


def half_months(first: date, last: date) -> list[tuple[date, date]]:
    """Every half-month from the one holding ``first`` to the one holding ``last``, in order."""
    out: list[tuple[date, date]] = []
    start, end = half_month(first)
    stop = half_month(last)[0]
    while start <= stop:
        out.append((start, end))
        start, end = half_month(_next_start(end))
    return out


def _advisory_key(partner_id: int) -> int:
    return partner_id % ADVISORY_MODULUS


def lock_partner(db: Session, partner_id: int, *, shared: bool) -> None:
    """Postgres: the transaction-scoped advisory lock of migration 0017 for this partner, shared
    (writers of lines, payments, corrections) or exclusive (the close). SQLite: nothing (one
    writer at a time anyway). Bound parameters only: no SQL text."""
    if db.get_bind().dialect.name != "postgresql":
        return
    lock = func.pg_advisory_xact_lock_shared if shared else func.pg_advisory_xact_lock
    db.execute(
        select(
            lock(
                cast(literal(ADVISORY_NAMESPACE), Integer),
                cast(literal(_advisory_key(partner_id)), Integer),
            )
        )
    )


def is_closed_period_refusal(exc: IntegrityError) -> bool:
    """The database refused a row because a closed period covers its date: migration 0017's
    trigger (its message, and on Postgres its sqlstate ``check_violation``). Anything else (a
    CHECK, a foreign key, a unique key) is not this refusal."""
    original = exc.orig
    if CLOSED_PERIOD_TRIGGER_MESSAGE not in str(original):
        return False
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate is None or sqlstate == CHECK_VIOLATION


def _refused_by_database(exc: IntegrityError, what: str) -> AccountConflict:
    """The French refusal for a write the database turned down; the log names the kind of
    refusal only (no value, no name)."""
    if is_closed_period_refusal(exc):
        return AccountConflict(PERIOD_JUST_CLOSED)
    original = exc.orig
    log.warning(
        "partner account write refused by the database",
        extra={
            "what": what,
            "error": type(original).__name__,
            "sqlstate": getattr(original, "sqlstate", None),
        },
    )
    return AccountConflict(DB_REFUSED)


def _refuse(
    db: Session, actor: User, action: str, message: str, now: datetime, ip: str | None
) -> AccountForbidden:
    audit.refused(db, action, agency_id=actor.agency_id, actor_user_id=actor.id, ip=ip, now=now)
    return AccountForbidden(message)


def _require_writer(db: Session, actor: User, action: str, now: datetime, ip: str | None) -> None:
    """An active manager or accountant: the only people who write a partner account."""
    if not permissions.can_validate_money(actor) or not permissions.can_manage_partners(actor):
        raise _refuse(db, actor, action, NO_PERMISSION, now, ip)
    if not actor.is_active:
        raise _refuse(db, actor, action, INACTIVE_ACTOR, now, ip)


def get_partner(db: Session, agency_id: int, partner_id: int) -> Partner | None:
    return db.scalars(
        select(Partner).where(Partner.agency_id == agency_id, Partner.id == partner_id)
    ).one_or_none()


def _partner_or_404(db: Session, actor: User, partner_id: int) -> Partner:
    partner = get_partner(db, actor.agency_id, partner_id)
    if partner is None:
        raise AccountNotFound(NOT_FOUND)
    return partner


# --- reading an account -----------------------------------------------------------------------


@dataclass(frozen=True)
class EntryView:
    entry: PartnerEntry
    decision: PartnerEntryDecision | None

    @property
    def needs_decision(self) -> bool:
        return needs_decision(self.entry.kind, self.entry.source)

    @property
    def pending(self) -> bool:
        return self.needs_decision and self.decision is None

    @property
    def counted(self) -> bool:
        if not self.needs_decision:
            return True
        return self.decision is not None and self.decision.decision != DECISION_REJECT

    @property
    def ledger(self) -> LedgerEntry:
        return LedgerEntry(
            kind=self.entry.kind,
            amount=self.entry.amount,
            source=self.entry.source,
            decision=None if self.decision is None else self.decision.decision,
            entry_id=self.entry.id,
        )


@dataclass(frozen=True)
class PeriodView:
    """One half-month of the account. ``row`` is set once it was closed (or a row exists);
    ``totals`` and the lines are live for an open period. A closed period carries its row
    only: what it shows comes from ``row.snapshot``."""

    start: date
    end: date
    row: PartnerPeriod | None
    opening: Decimal
    closing: Decimal
    totals: PeriodTotals | None = None
    sales: tuple[PartnerSaleLine, ...] = ()
    entries: tuple[EntryView, ...] = ()
    payments: tuple[PartnerPayment, ...] = ()

    @property
    def closed(self) -> bool:
        return self.row is not None and self.row.status == PERIOD_CLOSED

    @property
    def label(self) -> str:
        return period_label(self.start, self.end)

    @property
    def pending(self) -> tuple[EntryView, ...]:
        return tuple(e for e in self.entries if e.pending)


@dataclass(frozen=True)
class Account:
    partner: Partner
    opening_entry: PartnerEntry | None
    periods: tuple[PeriodView, ...]  # oldest first
    # Payments dated after the end of the first closed period (``oldest_unpaid``).
    later_payments: tuple[PartnerPayment, ...] = ()

    @property
    def balance(self) -> Decimal:
        return self.periods[-1].closing if self.periods else self.opening_amount

    @property
    def opening_amount(self) -> Decimal:
        return ZERO if self.opening_entry is None else self.opening_entry.amount

    @property
    def floor(self) -> date | None:
        """The switch-over day: nothing dated before it belongs to this account."""
        return None if self.opening_entry is None else self.opening_entry.entry_date

    @property
    def current(self) -> PeriodView | None:
        return self.periods[-1] if self.periods else None

    @property
    def first_open(self) -> PeriodView | None:
        return next((p for p in self.periods if not p.closed), None)

    @property
    def newest_first(self) -> tuple[PeriodView, ...]:
        return tuple(reversed(self.periods))


def opening_entry(db: Session, agency_id: int, partner_id: int) -> PartnerEntry | None:
    return db.scalars(
        select(PartnerEntry).where(
            PartnerEntry.agency_id == agency_id,
            PartnerEntry.partner_id == partner_id,
            PartnerEntry.kind == ENTRY_OPENING,
        )
    ).one_or_none()


def latest_closed_end(db: Session, agency_id: int, partner_id: int) -> date | None:
    """The last day of the latest closed period: nothing may be dated on or before it."""
    found: date | None = db.scalar(
        select(func.max(PartnerPeriod.end_date)).where(
            PartnerPeriod.agency_id == agency_id,
            PartnerPeriod.partner_id == partner_id,
            PartnerPeriod.status == PERIOD_CLOSED,
        )
    )
    return found


def _entries_query(agency_id: int) -> Any:
    return (
        select(PartnerEntry, PartnerEntryDecision)
        .outerjoin(
            PartnerEntryDecision,
            and_(
                PartnerEntryDecision.agency_id == PartnerEntry.agency_id,
                PartnerEntryDecision.entry_id == PartnerEntry.id,
            ),
        )
        .where(PartnerEntry.agency_id == agency_id, PartnerEntry.kind != ENTRY_OPENING)
        .order_by(PartnerEntry.entry_date, PartnerEntry.id)
        .execution_options(populate_existing=True)
    )


def _entries(
    db: Session, agency_id: int, partner_id: int, start: date, end: date
) -> list[EntryView]:
    rows = db.execute(
        _entries_query(agency_id).where(
            PartnerEntry.partner_id == partner_id,
            PartnerEntry.entry_date >= start,
            PartnerEntry.entry_date <= end,
        )
    )
    return [EntryView(entry, decision) for entry, decision in rows]


def _payments(
    db: Session, agency_id: int, partner_id: int, start: date, end: date
) -> list[PartnerPayment]:
    return list(
        db.scalars(
            select(PartnerPayment)
            .where(
                PartnerPayment.agency_id == agency_id,
                PartnerPayment.partner_id == partner_id,
                PartnerPayment.paid_on >= start,
                PartnerPayment.paid_on <= end,
            )
            .order_by(PartnerPayment.paid_on, PartnerPayment.id)
            .execution_options(populate_existing=True)
        )
    )


def _totals(
    opening: Decimal,
    sale_lines: Sequence[PartnerSaleLine],
    entries: Sequence[EntryView],
    payments: Sequence[PartnerPayment],
) -> PeriodTotals:
    return compute_period(
        opening,
        sale_lines,
        [e.ledger for e in entries],
        [LedgerPayment(p.amount, p.id) for p in payments],
    )


def _live(
    db: Session,
    agency_id: int,
    partner_id: int,
    start: date,
    end: date,
    opening: Decimal,
    floor: date | None,
) -> tuple[PeriodTotals, list[PartnerSaleLine], list[EntryView], list[PartnerPayment]]:
    """The totals of an open period from what the database holds now (fresh statements), from
    ``floor`` (the switch-over day) when it falls inside the period."""
    since = max(start, floor) if floor is not None else start
    sale_lines = partner_sale_lines(db, agency_id, partner_id, since, end) if since <= end else []
    entries = _entries(db, agency_id, partner_id, since, end)
    payments = _payments(db, agency_id, partner_id, since, end)
    return _totals(opening, sale_lines, entries, payments), sale_lines, entries, payments


@dataclass
class _Loaded:
    """What the accounts of several partners of one agency need, read in a constant number of
    queries (never one per partner)."""

    rows: dict[int, list[PartnerPeriod]] = field(default_factory=dict)
    openings: dict[int, PartnerEntry] = field(default_factory=dict)
    first: dict[int, date] = field(default_factory=dict)  # the first period's start
    live_from: dict[int, date] = field(default_factory=dict)  # first day read live
    sales: dict[int, list[PartnerSaleLine]] = field(default_factory=dict)
    entries: dict[int, list[EntryView]] = field(default_factory=dict)
    payments: dict[int, list[PartnerPayment]] = field(default_factory=dict)


def _first_activity(db: Session, agency_id: int, partner_ids: Collection[int]) -> dict[int, date]:
    """The earliest day of a current partner sale, a line (not the opening) or a payment, per
    partner: three grouped queries for all of them."""
    ids = sorted(set(partner_ids))
    found: dict[int, date] = {}

    def keep(pairs: Iterable[Any]) -> None:
        for partner_id, day in pairs:
            if partner_id is not None and day is not None:
                found[partner_id] = min(day, found.get(partner_id, day))

    keep(
        db.execute(
            select(SaleVersion.partner_id, func.min(ReportDay.day))
            .select_from(Sale)
            .join(
                SaleVersion,
                and_(
                    SaleVersion.agency_id == Sale.agency_id,
                    SaleVersion.sale_id == Sale.id,
                    SaleVersion.version_no == Sale.current_version,
                ),
            )
            .join(
                ReportDay,
                and_(ReportDay.agency_id == Sale.agency_id, ReportDay.id == Sale.report_day_id),
            )
            .where(
                Sale.agency_id == agency_id,
                Sale.status == SALE_ACTIVE,
                SaleVersion.voided.is_(False),
                SaleVersion.ag_kind == AG_PARTNER,
                SaleVersion.partner_id.in_(ids),
            )
            .group_by(SaleVersion.partner_id)
        )
    )
    keep(
        db.execute(
            select(PartnerEntry.partner_id, func.min(PartnerEntry.entry_date))
            .where(
                PartnerEntry.agency_id == agency_id,
                PartnerEntry.partner_id.in_(ids),
                PartnerEntry.kind != ENTRY_OPENING,
            )
            .group_by(PartnerEntry.partner_id)
        )
    )
    keep(
        db.execute(
            select(PartnerPayment.partner_id, func.min(PartnerPayment.paid_on))
            .where(PartnerPayment.agency_id == agency_id, PartnerPayment.partner_id.in_(ids))
            .group_by(PartnerPayment.partner_id)
        )
    )
    return found


def _load(
    db: Session,
    agency_id: int,
    partner_ids: Collection[int],
    today: date,
    *,
    snapshots: bool = False,
) -> _Loaded:
    """Read the accounts of ``partner_ids`` (of ``agency_id``): at most eight queries whatever
    their number. Only the open periods after each partner's last closed one are read live; the
    closed ones come from their rows (the snapshot deferred unless ``snapshots``)."""
    loaded = _Loaded()
    ids = sorted(set(partner_ids))
    if not ids:
        return loaded
    rows_query = (
        select(PartnerPeriod)
        .where(PartnerPeriod.agency_id == agency_id, PartnerPeriod.partner_id.in_(ids))
        .order_by(PartnerPeriod.partner_id, PartnerPeriod.start_date)
        .execution_options(populate_existing=True)
    )
    if not snapshots:
        rows_query = rows_query.options(defer(PartnerPeriod.snapshot))
    for row in db.scalars(rows_query):
        loaded.rows.setdefault(row.partner_id, []).append(row)
    for entry in db.scalars(
        select(PartnerEntry).where(
            PartnerEntry.agency_id == agency_id,
            PartnerEntry.partner_id.in_(ids),
            PartnerEntry.kind == ENTRY_OPENING,
        )
    ):
        loaded.openings[entry.partner_id] = entry
    need_activity: list[int] = []
    for partner_id in ids:
        opening = loaded.openings.get(partner_id)
        closed = [r for r in loaded.rows.get(partner_id, []) if r.status == PERIOD_CLOSED]
        if opening is not None:
            loaded.first[partner_id] = half_month(opening.entry_date)[0]
        elif closed:
            loaded.first[partner_id] = closed[0].start_date
        else:
            need_activity.append(partner_id)
    activity = _first_activity(db, agency_id, need_activity) if need_activity else {}
    for partner_id in need_activity:
        starts = [r.start_date for r in loaded.rows.get(partner_id, [])]
        if partner_id in activity:
            starts.append(half_month(activity[partner_id])[0])
        if starts:
            loaded.first[partner_id] = min(starts)
    pay_from: dict[int, date] = {}
    for partner_id, first in loaded.first.items():
        closed = [r for r in loaded.rows.get(partner_id, []) if r.status == PERIOD_CLOSED]
        live = _next_start(closed[-1].end_date) if closed else first
        opening = loaded.openings.get(partner_id)
        if opening is not None:
            live = max(live, opening.entry_date)
        loaded.live_from[partner_id] = live
        pay_from[partner_id] = min(live, _next_start(closed[0].end_date)) if closed else live
    if not loaded.live_from:
        return loaded
    live_ids = sorted(loaded.live_from)
    since = min(loaded.live_from.values())
    until = max(half_month(today)[1], since)
    for partner_id, lines in partner_sale_lines_many(db, agency_id, live_ids, since, until).items():
        start = loaded.live_from[partner_id]
        loaded.sales[partner_id] = [line for line in lines if line.day >= start]
    for entry, decision in db.execute(
        _entries_query(agency_id).where(
            PartnerEntry.partner_id.in_(live_ids), PartnerEntry.entry_date >= since
        )
    ):
        if entry.entry_date >= loaded.live_from[entry.partner_id]:
            loaded.entries.setdefault(entry.partner_id, []).append(EntryView(entry, decision))
    for payment in db.scalars(
        select(PartnerPayment)
        .where(
            PartnerPayment.agency_id == agency_id,
            PartnerPayment.partner_id.in_(live_ids),
            PartnerPayment.paid_on >= min(pay_from.values()),
        )
        .order_by(PartnerPayment.paid_on, PartnerPayment.id)
        .execution_options(populate_existing=True)
    ):
        if payment.paid_on >= pay_from[payment.partner_id]:
            loaded.payments.setdefault(payment.partner_id, []).append(payment)
    return loaded


def _assemble(partner: Partner, loaded: _Loaded, today: date) -> Account:
    """The account of ``partner`` from what ``_load`` read: no query."""
    opening = loaded.openings.get(partner.id)
    first = loaded.first.get(partner.id)
    if first is None:
        return Account(partner, opening, ())
    by_start = {r.start_date: r for r in loaded.rows.get(partner.id, [])}
    live_from = loaded.live_from[partner.id]
    sales = loaded.sales.get(partner.id, [])
    entries = loaded.entries.get(partner.id, [])
    payments = loaded.payments.get(partner.id, [])
    balance = ZERO if opening is None else opening.amount
    periods: list[PeriodView] = []
    for start, end in half_months(first, max(today, first)):
        row = by_start.get(start)
        if row is not None and row.status == PERIOD_CLOSED:
            if row.opening_balance is None or row.closing_balance is None:
                raise RuntimeError(f"closed partner period {row.id} has no balance")
            periods.append(PeriodView(start, end, row, row.opening_balance, row.closing_balance))
            balance = row.closing_balance
            continue
        since = max(start, live_from)
        in_sales = tuple(s for s in sales if since <= s.day <= end)
        in_entries = tuple(e for e in entries if since <= e.entry.entry_date <= end)
        in_payments = tuple(p for p in payments if since <= p.paid_on <= end)
        totals = _totals(balance, in_sales, in_entries, in_payments)
        periods.append(
            PeriodView(
                start,
                end,
                row,
                totals.opening,
                totals.closing,
                totals,
                in_sales,
                in_entries,
                in_payments,
            )
        )
        balance = totals.closing
    closed = [p for p in periods if p.closed]
    later = tuple(p for p in payments if closed and p.paid_on > closed[0].end)
    return Account(partner, opening, tuple(periods), later)


def account(
    db: Session, agency_id: int, partner: Partner, today: date, *, snapshots: bool = False
) -> Account:
    """The account of ``partner`` (of ``agency_id``): its periods from the first one to the one
    holding today, closed ones from their rows, open ones computed now."""
    return _assemble(partner, _load(db, agency_id, [partner.id], today, snapshots=snapshots), today)


def accounts_of(
    db: Session, agency_id: int, partners: Sequence[Partner], today: date
) -> list[Account]:
    """The accounts of several partners of ``agency_id``, in their order: a constant number of
    queries (``_load``), never one per partner."""
    loaded = _load(db, agency_id, [p.id for p in partners], today)
    return [_assemble(p, loaded, today) for p in partners]


def get_account(
    db: Session, actor: User, partner_id: int, today: date, *, snapshots: bool = False
) -> Account:
    """The account as a manager or accountant may read it; ``AccountNotFound`` otherwise."""
    if not permissions.can_manage_partners(actor):
        raise AccountForbidden(NO_PERMISSION)
    partner = _partner_or_404(db, actor, partner_id)
    return account(db, actor.agency_id, partner, today, snapshots=snapshots)


# --- the list of every partner ----------------------------------------------------------------


@dataclass(frozen=True)
class Overdue:
    """The oldest closed period whose closing balance is not paid off yet."""

    start: date
    end: date
    amount: Decimal
    due: date
    age_days: int  # since the period ended
    level: str  # partner_ledger.ALERT_*


@dataclass(frozen=True)
class AccountRow:
    partner: Partner
    balance: Decimal
    open_total: Decimal  # the movements of the current period: sales + lines - payments
    oldest: Overdue | None

    @property
    def rank(self) -> int:
        level = self.oldest.level if self.oldest else ALERT_NONE
        return {ALERT_OVERDUE: 0, ALERT_DUE_SOON: 1}.get(level, 2)


def oldest_unpaid(acc: Account, today: date) -> Overdue | None:
    """First in, first out, never more than what is owed now: what a closed period still lacks
    is its closing balance minus every payment dated after its end, capped by today's balance
    (a refund or a credit after the close pays the oldest debt too). The oldest closed period
    still lacking something is returned; None when nothing is owed."""
    owed = max(ZERO, acc.balance)
    if owed == 0:
        return None
    for period in (p for p in acc.periods if p.closed):
        paid_after = sum((p.amount for p in acc.later_payments if p.paid_on > period.end), ZERO)
        lacking = min(period.closing - paid_after, owed)
        if lacking > 0:
            due = due_date(period.end)
            return Overdue(
                period.start,
                period.end,
                lacking,
                due,
                (today - period.end).days,
                alert_level(today, due),
            )
    return None


def account_rows(db: Session, actor: User, today: date) -> list[AccountRow]:
    """Every active partner of the actor's agency with its balance: overdue first, then due
    soon, then the oldest debt, then by name. A constant number of queries (``_load``)."""
    if not permissions.can_manage_partners(actor):
        raise AccountForbidden(NO_PERMISSION)
    agency_id = actor.agency_id
    found = list(
        db.scalars(
            select(Partner)
            .where(Partner.agency_id == agency_id, Partner.is_active.is_(True))
            .order_by(Partner.name_key, Partner.id)
        )
    )
    loaded = _load(db, agency_id, [p.id for p in found], today)
    rows: list[AccountRow] = []
    for partner in found:
        acc = _assemble(partner, loaded, today)
        current = acc.current
        open_total = ZERO
        if current is not None and current.totals is not None:
            open_total = current.closing - current.opening
        rows.append(AccountRow(partner, acc.balance, open_total, oldest_unpaid(acc, today)))
    return sorted(
        rows,
        key=lambda r: (r.rank, -(r.oldest.age_days if r.oldest else 0), r.partner.name_key),
    )


# --- dates of a new line or payment -----------------------------------------------------------


def _check_date(db: Session, agency_id: int, partner_id: int, day: date, today: date) -> None:
    """Today or earlier, after the latest closed period, not before the switch-over day."""
    if day > today:
        raise PartnerAccountError(FUTURE_DATE)
    closed_end = latest_closed_end(db, agency_id, partner_id)
    if closed_end is not None and day <= closed_end:
        raise PartnerAccountError(BACKDATED.format(end=date_fr(closed_end)))
    opening = opening_entry(db, agency_id, partner_id)
    if opening is not None and day < opening.entry_date:
        raise PartnerAccountError(BEFORE_OPENING.format(start=date_fr(opening.entry_date)))


def _active_user(db: Session, agency_id: int, user_id: int | None) -> User | None:
    if user_id is None:
        return None
    return db.scalars(
        select(User).where(
            User.agency_id == agency_id, User.id == user_id, User.is_active.is_(True)
        )
    ).one_or_none()


def _someone_else_approves(db: Session, actor: User) -> bool:
    """Another active user of the agency could approve an AJUSTEMENT ``actor`` enters."""
    others = db.scalars(
        select(User).where(
            User.agency_id == actor.agency_id, User.is_active.is_(True), User.id != actor.id
        )
    )
    return any(permissions.can_approve_partner_adjustment(u) for u in others)


@dataclass(frozen=True)
class Written:
    """What a write produced: the row, and whether this request wrote it (False: the same form
    was posted before and the row is the one already stored)."""

    row: Any
    created: bool


def _insert(
    db: Session,
    row: PartnerEntry | PartnerPayment,
    find: Callable[[], PartnerEntry | PartnerPayment | None],
    same: Callable[[Any], bool],
    what: str,
) -> tuple[PartnerEntry | PartnerPayment, bool]:
    """Insert ``row`` in a savepoint. A unique violation on the request key returns the row
    stored by the request that won (when it holds the same values, else ``StaleRequestKey``);
    the closed-period trigger becomes ``PERIOD_JUST_CLOSED``, any other refusal ``DB_REFUSED``,
    never a 500."""
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        existing = find()
        if existing is not None:
            if not same(existing):
                raise StaleRequestKey(KEY_REUSED) from None
            return existing, False
        raise _refused_by_database(exc, what) from None
    return row, True


# --- add a manual line ------------------------------------------------------------------------


def _entry_by_key(db: Session, agency_id: int, key: str) -> PartnerEntry | None:
    return db.scalars(
        select(PartnerEntry)
        .where(PartnerEntry.agency_id == agency_id, PartnerEntry.request_key == key)
        .execution_options(populate_existing=True)
    ).one_or_none()


def _payment_by_key(db: Session, agency_id: int, key: str) -> PartnerPayment | None:
    return db.scalars(
        select(PartnerPayment)
        .where(PartnerPayment.agency_id == agency_id, PartnerPayment.request_key == key)
        .execution_options(populate_existing=True)
    ).one_or_none()


def _signed_amount(kind: str, raw: str, direction: str) -> Decimal:
    try:
        amount = money.parse_amount(raw)
    except money.MoneyError as exc:
        raise PartnerAccountError(str(exc)) from None
    if amount == 0:
        raise PartnerAccountError(ZERO_AMOUNT)
    if kind == ENTRY_RFND:
        amount = -amount
    elif kind == ENTRY_AJUSTEMENT:
        if direction not in DIRECTIONS:
            raise PartnerAccountError(BAD_DIRECTION)
        if direction == DIRECTION_LESS:
            amount = -amount
    return check_entry_amount(kind, amount)


def add_line(
    db: Session,
    actor: User,
    partner_id: int,
    *,
    kind: str,
    amount: str,
    reason: str,
    request_key: str,
    entry_date: str = "",
    direction: str = "",
    now: datetime | None = None,
    ip: str | None = None,
) -> Written:
    """A manual ``ADM``, ``RFND``, ``CHANGEMENT`` or ``AJUSTEMENT`` line (``amount`` as typed,
    always positive; the kind gives the sign, and ``direction`` for an ``AJUSTEMENT``)."""
    moment = now or utc_now()
    _require_writer(db, actor, "add_partner_line", moment, ip)
    key = check_request_key(request_key)
    partner = _partner_or_404(db, actor, partner_id)
    if kind not in MANUAL_KINDS:
        raise PartnerAccountError(BAD_KIND)
    signed = _signed_amount(kind, amount, direction)
    today = moment.date()
    day = parse_day(entry_date, today)

    def same(row: PartnerEntry) -> bool:
        return (row.partner_id, row.kind, row.amount, row.entry_date) == (
            partner.id,
            kind,
            signed,
            day,
        )

    existing = _entry_by_key(db, actor.agency_id, key)
    if existing is not None:
        if existing.partner_id != partner.id:
            raise StaleRequestKey(KEY_USED_ELSEWHERE)
        if not same(existing):
            raise StaleRequestKey(KEY_REUSED)
        return Written(existing, False)
    try:
        clean = reasons.required_reason(reason)
    except reasons.ReasonError as exc:
        raise PartnerAccountError(str(exc)) from None
    if kind == ENTRY_AJUSTEMENT and not _someone_else_approves(db, actor):
        raise PartnerAccountError(NO_APPROVER)
    lock_partner(db, partner.id, shared=True)
    _check_date(db, actor.agency_id, partner.id, day, today)
    row = PartnerEntry(
        agency_id=actor.agency_id,
        partner_id=partner.id,
        entry_date=day,
        kind=kind,
        amount=signed,
        reason=clean,
        source=check_entry_source(kind, ENTRY_SOURCE_MANUAL),
        created_by_user_id=actor.id,
        request_key=key,
        created_at=moment,
    )
    stored, created = _insert(
        db, row, lambda: _entry_by_key(db, actor.agency_id, key), same, "partner_entries"
    )
    if not created:
        return Written(stored, False)
    audit.record(
        db,
        ENTRY_ADDED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "partner_id": partner.id,
            "entry_id": stored.id,
            "kind": kind,
            "amount": str(signed),
            "entry_date": day.isoformat(),
        },
        now=moment,
    )
    return Written(stored, True)


# --- decide a line ----------------------------------------------------------------------------


def decide_line(
    db: Session,
    actor: User,
    partner_id: int,
    entry_id: int,
    *,
    decision: str,
    note: str = "",
    now: datetime | None = None,
    ip: str | None = None,
) -> Written:
    """Approve or reject an ``AJUSTEMENT`` (a manager who did not enter it), confirm or reject
    an ``RFND`` proposed from Amadeus (the accountant or the manager). A rejection needs a
    reason (``note``). The same decision posted twice by the same person returns the first."""
    moment = now or utc_now()
    _require_writer(db, actor, "decide_partner_line", moment, ip)
    partner = _partner_or_404(db, actor, partner_id)
    entry = db.scalars(
        select(PartnerEntry)
        .where(
            PartnerEntry.agency_id == actor.agency_id,
            PartnerEntry.partner_id == partner.id,
            PartnerEntry.id == entry_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()
    if entry is None:
        raise AccountNotFound(LINE_NOT_FOUND)
    if not needs_decision(entry.kind, entry.source):
        raise PartnerAccountError(NO_DECISION_NEEDED)
    accepting = DECISION_APPROVE if entry.kind == ENTRY_AJUSTEMENT else DECISION_CONFIRM
    if decision not in (accepting, DECISION_REJECT):
        raise PartnerAccountError(BAD_DECISION)
    if entry.kind == ENTRY_AJUSTEMENT:
        if not permissions.can_approve_partner_adjustment(actor):
            raise _refuse(db, actor, "decide_partner_adjustment", NOT_MANAGER, moment, ip)
        if entry.created_by_user_id == actor.id:
            raise _refuse(db, actor, "decide_own_partner_adjustment", OWN_LINE, moment, ip)
    lock_partner(db, partner.id, shared=True)
    stored = _decision_of(db, actor.agency_id, entry.id)
    if stored is not None:
        if stored.decision == decision and stored.decided_by_user_id == actor.id:
            return Written(stored, False)
        raise AccountConflict(ALREADY_DECIDED)
    try:
        clean = (
            reasons.required_reason(note)
            if decision == DECISION_REJECT
            else reasons.clean_reason(note)
        )
    except reasons.ReasonError as exc:
        raise PartnerAccountError(str(exc)) from None
    closed_end = latest_closed_end(db, actor.agency_id, partner.id)
    if closed_end is not None and entry.entry_date <= closed_end:
        raise AccountConflict(PERIOD_JUST_CLOSED)
    row = PartnerEntryDecision(
        agency_id=actor.agency_id,
        entry_id=entry.id,
        entry_kind=entry.kind,
        entry_source=entry.source,
        decision=decision,
        decided_by_user_id=actor.id,
        note=clean,
        created_at=moment,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        stored = _decision_of(db, actor.agency_id, entry.id)
        if stored is None:
            raise _refused_by_database(exc, "partner_entry_decisions") from None
        if stored.decision == decision and stored.decided_by_user_id == actor.id:
            return Written(stored, False)
        raise AccountConflict(ALREADY_DECIDED) from None
    audit.record(
        db,
        ENTRY_DECIDED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "partner_id": partner.id,
            "entry_id": entry.id,
            "kind": entry.kind,
            "source": entry.source,
            "amount": str(entry.amount),
            "decision": decision,
        },
        now=moment,
    )
    return Written(row, True)


def _decision_of(db: Session, agency_id: int, entry_id: int) -> PartnerEntryDecision | None:
    return db.scalars(
        select(PartnerEntryDecision)
        .where(
            PartnerEntryDecision.agency_id == agency_id,
            PartnerEntryDecision.entry_id == entry_id,
        )
        .execution_options(populate_existing=True)
    ).one_or_none()


# --- record a payment -------------------------------------------------------------------------


def _clean_reference(raw: str) -> str | None:
    text = " ".join(clean_text(raw if isinstance(raw, str) else "").split())
    if not text:
        return None
    if len(text) > MAX_PAYMENT_REFERENCE:
        raise PartnerAccountError(REFERENCE_TOO_LONG)
    return text


def record_payment(
    db: Session,
    actor: User,
    partner_id: int,
    *,
    amount: str,
    method: str,
    received_by_user_id: int | None,
    request_key: str,
    paid_on: str = "",
    reference: str = "",
    now: datetime | None = None,
    ip: str | None = None,
) -> Written:
    """Money received from the partner (owner's answer 1: the accountant or the manager
    records it). The method is one of the sales' payment list except ``credit``; whoever
    received it is an active user of the agency."""
    moment = now or utc_now()
    _require_writer(db, actor, "record_partner_payment", moment, ip)
    key = check_request_key(request_key)
    partner = _partner_or_404(db, actor, partner_id)
    try:
        value = money.parse_amount(amount)
    except money.MoneyError as exc:
        raise PartnerAccountError(str(exc)) from None
    if value <= 0:
        raise PartnerAccountError(ZERO_AMOUNT)
    if method not in PARTNER_PAYMENT_METHODS:
        raise PartnerAccountError(BAD_METHOD)
    today = moment.date()
    day = parse_day(paid_on, today)

    def same(row: PartnerPayment) -> bool:
        return (row.partner_id, row.amount, row.method, row.paid_on) == (
            partner.id,
            value,
            method,
            day,
        )

    existing = _payment_by_key(db, actor.agency_id, key)
    if existing is not None:
        if existing.partner_id != partner.id:
            raise StaleRequestKey(KEY_USED_ELSEWHERE)
        if not same(existing):
            raise StaleRequestKey(KEY_REUSED)
        return Written(existing, False)
    receiver = _active_user(db, actor.agency_id, received_by_user_id)
    if receiver is None:
        raise PartnerAccountError(BAD_RECEIVER)
    ref = _clean_reference(reference)
    lock_partner(db, partner.id, shared=True)
    _check_date(db, actor.agency_id, partner.id, day, today)
    row = PartnerPayment(
        agency_id=actor.agency_id,
        partner_id=partner.id,
        paid_on=day,
        amount=value,
        method=method,
        reference=ref,
        received_by_user_id=receiver.id,
        recorded_by_user_id=actor.id,
        request_key=key,
        created_at=moment,
    )
    stored, created = _insert(
        db, row, lambda: _payment_by_key(db, actor.agency_id, key), same, "partner_payments"
    )
    if not created:
        return Written(stored, False)
    audit.record(
        db,
        PAYMENT_RECORDED,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "partner_id": partner.id,
            "payment_id": stored.id,
            "amount": str(value),
            "method": method,
            "paid_on": day.isoformat(),
            "received_by_user_id": receiver.id,
        },
        now=moment,
    )
    return Written(stored, True)


# --- close a period ---------------------------------------------------------------------------


def _money_text(value: Decimal) -> str:
    return str(value)


def _snapshot(
    key: str,
    totals: PeriodTotals,
    sale_lines: Iterable[PartnerSaleLine],
    entries: Iterable[EntryView],
    payments: Iterable[PartnerPayment],
) -> dict[str, Any]:
    """Every line of the period as it is sent to the partner (money as text)."""
    return {
        "request_key": key,
        "opening": _money_text(totals.opening),
        "sales_total": _money_text(totals.sales_total),
        "entries_total": _money_text(totals.entries_total),
        "payments_total": _money_text(totals.payments_total),
        "closing": _money_text(totals.closing),
        "sales": [
            {
                "sale_id": line.sale_id,
                "version_no": line.version_no,
                "day": line.day.isoformat(),
                "fee": _money_text(line.fee),
                "cost_total": _money_text(line.cost_total),
                "pb": _money_text(line.pb),
                "tickets": [ticket_json(t) for t in line.tickets],
            }
            for line in sale_lines
        ],
        "entries": [
            {
                "id": e.entry.id,
                "date": e.entry.entry_date.isoformat(),
                "kind": e.entry.kind,
                "source": e.entry.source,
                "amount": _money_text(e.entry.amount),
                "reason": e.entry.reason,
                "sale_id": e.entry.sale_id,
                "doc_number": e.entry.doc_number,
                "decision": None if e.decision is None else e.decision.decision,
                "counted": e.counted,
            }
            for e in entries
        ],
        "payments": [
            {
                "id": p.id,
                "date": p.paid_on.isoformat(),
                "amount": _money_text(p.amount),
                "method": p.method,
                "reference": p.reference,
                "received_by_user_id": p.received_by_user_id,
            }
            for p in payments
        ],
    }


def _open_days(db: Session, agency_id: int, start: date, end: date) -> list[date]:
    """The report days of the range that are not closed, read ``FOR SHARE`` so none of them is
    reopened while the close runs."""
    rows = db.scalars(
        select(ReportDay)
        .where(ReportDay.agency_id == agency_id, ReportDay.day >= start, ReportDay.day <= end)
        .order_by(ReportDay.day)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return [r.day for r in rows if r.status != DAY_CLOSED]


def _period_row(db: Session, agency_id: int, partner_id: int, start: date) -> PartnerPeriod | None:
    return db.scalars(
        select(PartnerPeriod)
        .where(
            PartnerPeriod.agency_id == agency_id,
            PartnerPeriod.partner_id == partner_id,
            PartnerPeriod.start_date == start,
        )
        .options(defer(PartnerPeriod.snapshot))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def close_period(
    db: Session,
    actor: User,
    partner_id: int,
    start: date,
    *,
    request_key: str,
    now: datetime | None = None,
    ip: str | None = None,
) -> Written:
    """Close the period of ``partner_id`` starting on ``start`` (see the module docstring)."""
    moment = now or utc_now()
    _require_writer(db, actor, "close_partner_period", moment, ip)
    key = check_request_key(request_key)
    partner = _partner_or_404(db, actor, partner_id)
    agency_id = actor.agency_id
    if half_month(start)[0] != start:
        raise AccountNotFound(PERIOD_NOT_FOUND)
    today = moment.date()
    # 1. Every insert of this partner in flight commits first; later ones see the close.
    lock_partner(db, partner.id, shared=False)
    acc = account(db, agency_id, partner, today)
    target = next((p for p in acc.periods if p.start == start), None)
    if target is None:
        raise AccountNotFound(PERIOD_NOT_FOUND)
    if target.closed:
        stored = (target.row.snapshot if target.row is not None else None) or {}
        if stored.get("request_key") == key:
            return Written(target.row, False)
        raise AccountConflict(ALREADY_CLOSED)
    earlier = next((p for p in acc.periods if p.start < start and not p.closed), None)
    if earlier is not None:
        raise AccountConflict(
            EARLIER_OPEN.format(start=date_fr(earlier.start), end=date_fr(earlier.end))
        )
    if target.end >= today:
        raise AccountConflict(NOT_OVER.format(day=date_fr(_next_start(target.end))))
    # 2. No day of the period may be reopened under us.
    open_days = _open_days(db, agency_id, target.start, target.end)
    if open_days:
        raise AccountConflict(DAYS_NOT_CLOSED.format(days=", ".join(date_fr(d) for d in open_days)))
    # 3. The period row, locked (born open if it was never written).
    row = _period_row(db, agency_id, partner.id, start)
    if row is None:
        try:
            with db.begin_nested():
                db.add(
                    PartnerPeriod(
                        agency_id=agency_id,
                        partner_id=partner.id,
                        start_date=target.start,
                        end_date=target.end,
                        status=PERIOD_OPEN,
                        created_at=moment,
                    )
                )
                db.flush()
        except IntegrityError:
            raise AccountConflict(CLOSE_LOST) from None
        row = _period_row(db, agency_id, partner.id, start)
    if row is None or row.status != PERIOD_OPEN:
        raise AccountConflict(CLOSE_LOST)
    # 4. Only now the totals, each a fresh statement.
    index = acc.periods.index(target)
    opening = acc.periods[index - 1].closing if index > 0 else acc.opening_amount
    totals, sale_lines, entries, payments = _live(
        db, agency_id, partner.id, target.start, target.end, opening, acc.floor
    )
    if totals.pending:
        raise AccountConflict(LINES_PENDING)
    snapshot = _snapshot(key, totals, sale_lines, entries, payments)
    try:
        with db.begin_nested():
            result = db.execute(
                update(PartnerPeriod)
                .where(
                    PartnerPeriod.agency_id == agency_id,
                    PartnerPeriod.id == row.id,
                    PartnerPeriod.status == PERIOD_OPEN,
                )
                .values(
                    status=PERIOD_CLOSED,
                    opening_balance=totals.opening,
                    sales_total=totals.sales_total,
                    entries_total=totals.entries_total,
                    payments_total=totals.payments_total,
                    closing_balance=totals.closing,
                    closed_at=moment,
                    closed_by_user_id=actor.id,
                    snapshot=snapshot,
                )
                .execution_options(synchronize_session=False)
            )
            if getattr(result, "rowcount", None) != 1:
                raise AccountConflict(CLOSE_LOST)
    except IntegrityError:
        raise AccountConflict(CLOSE_LOST) from None
    db.refresh(row)
    audit.record(
        db,
        PERIOD_CLOSED_EVENT,
        agency_id=agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "partner_id": partner.id,
            "period_id": row.id,
            "start_date": target.start.isoformat(),
            "end_date": target.end.isoformat(),
            "opening": str(totals.opening),
            "sales_total": str(totals.sales_total),
            "entries_total": str(totals.entries_total),
            "payments_total": str(totals.payments_total),
            "closing": str(totals.closing),
            "sale_count": len(sale_lines),
            "entry_count": len(entries),
            "payment_count": len(payments),
        },
        now=moment,
    )
    return Written(row, True)


# --- corrections after a close ----------------------------------------------------------------


@dataclass(frozen=True)
class SaleShare:
    """What one version of a sale puts on a partner's account: its P.B on its day."""

    partner_id: int
    day: date
    pb: Decimal


def _covered(db: Session, agency_id: int, share: SaleShare) -> tuple[date, date] | None:
    """The closed period that counted ``share`` (its half-month), or None when that day is
    still in an open period, or before the switch-over day (in the opening balance)."""
    closed_end = latest_closed_end(db, agency_id, share.partner_id)
    if closed_end is None or share.day > closed_end:
        return None
    opening = opening_entry(db, agency_id, share.partner_id)
    if opening is not None and share.day < opening.entry_date:
        return None
    return half_month(share.day)


def _sale_names_partner(db: Session, agency_id: int, sale_id: int, partner_id: int) -> bool:
    """A version of the sale names this partner: a correction only ever points at a sale of the
    same partner."""
    return (
        db.scalar(
            select(SaleVersion.id)
            .where(
                SaleVersion.agency_id == agency_id,
                SaleVersion.sale_id == sale_id,
                SaleVersion.ag_kind == AG_PARTNER,
                SaleVersion.partner_id == partner_id,
            )
            .limit(1)
        )
        is not None
    )


def correct_after_close(
    db: Session,
    actor: User,
    sale_id: int,
    before: SaleShare | None,
    after: SaleShare | None,
    *,
    action: str,
    now: datetime,
    ip: str | None = None,
) -> list[PartnerEntry]:
    """The ``CORRECTION`` lines for a change of sale ``sale_id`` from ``before`` to ``after``
    (None: not a partner sale, or voided). For each partner, the difference between what the
    closed periods counted (``before``, when its day is in a closed period) and what they would
    count now (``after``, likewise) goes into the open period, dated today. A day still in an
    open period needs nothing: the open period reads the sale live. Called by ``app.sales`` in
    the sale's own transaction, after the new version is flushed.

    The shared advisory lock is only taken for a partner with a closed period that could cover
    the day (checked first without it); everything is checked again under the lock."""
    shares = [(s, sign) for s, sign in ((before, -1), (after, 1)) if s is not None]
    candidates: set[int] = set()
    for share, _sign in shares:
        closed_end = latest_closed_end(db, actor.agency_id, share.partner_id)
        if closed_end is not None and share.day <= closed_end:
            candidates.add(share.partner_id)
    if not candidates:
        return []
    for partner_id in sorted(candidates):
        lock_partner(db, partner_id, shared=True)
    delta: dict[int, Decimal] = defaultdict(lambda: ZERO)
    label: dict[int, tuple[date, date]] = {}
    for share, sign in shares:
        period = _covered(db, actor.agency_id, share)  # again, under the lock
        if period is None:
            continue
        delta[share.partner_id] += sign * share.pb
        label.setdefault(share.partner_id, period)
    written: list[PartnerEntry] = []
    for partner_id in sorted(delta):
        amount = delta[partner_id]
        if amount == 0:
            continue
        if not _sale_names_partner(db, actor.agency_id, sale_id, partner_id):
            raise RuntimeError(f"sale {sale_id} is not a sale of partner {partner_id}")
        start, end = label[partner_id]
        row = PartnerEntry(
            agency_id=actor.agency_id,
            partner_id=partner_id,
            entry_date=now.date(),
            kind=ENTRY_CORRECTION,
            amount=check_entry_amount(ENTRY_CORRECTION, amount),
            reason=CORRECTION_REASON.format(
                sale=sale_id, action=action, start=start.day, end=date_fr(end)
            ),
            source=check_entry_source(ENTRY_CORRECTION, ENTRY_SOURCE_SYSTEM),
            sale_id=sale_id,
            created_by_user_id=actor.id,
            created_at=now,
        )
        db.add(row)
        db.flush()
        audit.record(
            db,
            CORRECTION_ADDED,
            agency_id=actor.agency_id,
            actor_user_id=actor.id,
            ip=ip,
            details={
                "partner_id": partner_id,
                "entry_id": row.id,
                "sale_id": sale_id,
                "amount": str(amount),
                "closed_start": start.isoformat(),
            },
            now=now,
        )
        written.append(row)
    return written


__all__ = [
    "ACTION_ADDED",
    "ACTION_EDITED",
    "ACTION_MOVED",
    "ACTION_VOIDED",
    "Account",
    "AccountConflict",
    "AccountForbidden",
    "AccountNotFound",
    "AccountRow",
    "EntryView",
    "PartnerAccountError",
    "PeriodView",
    "SaleShare",
    "StaleRequestKey",
    "Written",
    "account",
    "account_rows",
    "add_line",
    "close_period",
    "correct_after_close",
    "decide_line",
    "get_account",
    "is_closed_period_refusal",
    "new_request_key",
    "oldest_unpaid",
    "record_payment",
]
