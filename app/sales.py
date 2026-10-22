"""Sales service: store, read and correct sales; the pure rules are in ``app/sales_rules.py``.

A sale is one or more tickets sold with ONE price (P.B), AG, CLT, AP and payment method; a single
ticket is a sale of one ticket. The public names of ``sales_rules`` are re-exported here, so
callers import everything from ``app.sales``.

* **Who may write**: staff (``permissions.can_enter_own_sales``) only for their own linked signs
  (no sign: "Demandez au gérant de lier votre signe"), only on a sale of today, and only while the
  day is not closed. Manager and accountant (``can_edit_any_entry``) may write any sale of their
  agency, on past days too (not after today, not more than 5 years back), and must give a reason
  to edit. A closed day refuses everybody until it is reopened. A role that is neither may do
  nothing. Staff see only their own signs' sales (another sign's sale is "not found").
* **Reasons**: an edit by manager or accountant needs one (5 to 500 characters). A staff member
  editing their own sale on the same day may leave it empty; if they give one it must be valid.
  Voiding and moving a sale need one for everybody.
* **History**: a creation, an edit, a void and a move to another day each append a
  ``sale_versions`` row holding the whole sale (tickets included, money as text) and bump
  ``sales.current_version``; old rows never change.
* **Locks, in this order**: the day ``FOR SHARE`` (two days: in id order), then the sale row
  ``FOR UPDATE`` re-read with ``populate_existing`` (a row already in the session is refreshed,
  never trusted). A version number that still clashes is a clean ``SaleError`` ("rechargez").
* **Tickets**: ``sale_tickets`` holds the current membership; ``UNIQUE (agency_id, doc_number)``
  makes a number belong to at most one sale per agency, even when two requests race.
  ``DuplicateTicket`` carries the existing sale's id only when the actor may see that sale.
  Removing a ticket, or voiding the sale, deletes its row: the number is free again.
* **Fee overrides** (an owner policy: staff may override, but every override is visible): the
  audit events carry ``fee_applied`` and ``fee_override``; ``overrides_for_day`` lists the active
  sales whose current version is an override. B9 will route them to accountant/manager
  validation; nothing else is needed in the schema for that (``sale_versions.fee_override_reason``
  is the marker).
* **Audit** events (ids and amounts only, never a name, phone or reason): ``sale_created``,
  ``sale_edited``, ``sale_voided``, ``sale_moved``; a refused attempt is ``permission_denied``
  with the action name only.

Functions never commit (a refused attempt is the one exception: the audit row is committed before
the error is raised, as the web guard does) and use savepoints: bind the tenant on the session
first (``app.tenancy.bind_tenant``), never inside the savepoint. When a write function raises, the
caller must roll back the session (a day may have been opened and flushed before the failure).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any, NoReturn, ParamSpec, TypeVar, cast

from sqlalchemy import Select, and_, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, days, partners, permissions
from app.accounts import AccountError, get_signs, is_unique_violation, normalize_staff_sign
from app.clock import utc_now
from app.errors import UserError
from app.models import (
    AG_PARTNER,
    SALE_ACTIVE,
    SALE_SOURCES,
    SALE_VOIDED,
    ReportDay,
    Sale,
    SaleTicket,
    SaleVersion,
    StaffSignLink,
    User,
)
from app.reasons import ReasonError, clean_reason, required_reason
from app.sales_rules import (
    MAX_TICKETS_PER_SALE,
    CarriedFee,
    DayTotals,
    FieldError,
    PartnerContext,
    SaleInput,
    SaleView,
    TicketInput,
    TicketLine,
    TicketRow,
    ValidatedSale,
    ValidationContext,
    below_cost_warning,
    day_totals,
    lines_from_json,
    pb_per_line,
    sale_cost,
    sale_profit,
    ticket_json,
    ticket_rows,
    validate,
)

__all__ = [
    "MAX_TICKETS_PER_SALE",
    "CarriedFee",
    "DayTotals",
    "DuplicateTicket",
    "InvalidReason",
    "FieldError",
    "InvalidSale",
    "PartnerContext",
    "SaleError",
    "SaleForbidden",
    "SaleInput",
    "SaleNotFound",
    "SaleVoided",
    "SaleResult",
    "StaleSale",
    "SaleView",
    "TicketInput",
    "TicketLine",
    "TicketRow",
    "ValidatedSale",
    "ValidationContext",
    "active_ticket_lookup",
    "agency_sales_by_id",
    "agency_sales_for_day",
    "build_context",
    "check_sale_consistency",
    "create_sale",
    "day_totals",
    "edit_sale",
    "get_sale_for",
    "move_sale_day",
    "overrides_for_day",
    "pb_per_line",
    "preview_sale",
    "sale_cost",
    "sale_history_for",
    "sale_profit",
    "sales_for_day_for",
    "sales_for_days_for",
    "sign_warnings",
    "ticket_rows",
    "validate",
    "view_warnings",
    "visible_signs",
    "void_sale",
]

ALL = "all"  # manager, accountant: every sign of the agency
OWN = "own"  # staff: their own linked signs

NO_PERMISSION = "Vous n'avez pas le droit de saisir des ventes."
NO_SIGN = "Aucun signe n'est lié à votre compte. Demandez au gérant de lier votre signe."
SIGN_NOT_YOURS = "Ce signe n'est pas lié à votre compte."
SIGN_REQUIRED = "Choisissez le signe de la vente."
BAD_SIGN = "Ce signe n'est pas valide."
ONLY_TODAY = "Vous ne pouvez saisir ou modifier que les ventes d'aujourd'hui."
MOVE_FORBIDDEN = "Seul le gérant ou le comptable peut déplacer une vente vers un autre jour."
NOT_FOUND = "Vente introuvable."
VOIDED = "Cette vente est annulée."
UNCHANGED = "Aucune modification à enregistrer."
SAME_DAY = "Cette vente est déjà sur cette journée."
BAD_SOURCE = "Source de la vente invalide."
STALE = "La vente a été modifiée entre-temps : rechargez la page et recommencez."
DUPLICATE_SEEN = "Le billet {doc} est déjà enregistré dans une autre vente."
DUPLICATE_HIDDEN = "Le billet {doc} est déjà enregistré par un autre agent."

P = ParamSpec("P")
R = TypeVar("R")

# A seam for tests of races: called after the "already sold?" check and before the write, so a test
# can make another request win in between. Always None in production.
after_duplicate_check: Callable[[], None] | None = None


class SaleError(UserError):
    """A mistake the user can correct; ``str(error)`` is a French message."""


class StaleSale(SaleError):
    """The sale changed between the form and the save ("rechargez la page")."""

    def __init__(self) -> None:
        super().__init__(STALE)


class SaleVoided(SaleError):
    """The sale is cancelled: it can no longer be edited or voided."""

    def __init__(self) -> None:
        super().__init__(VOIDED)


class InvalidReason(SaleError):
    """The reason of an edit, a void or a move is missing, too short or too long."""


class SaleNotFound(SaleError):
    """No such sale in the actor's agency (or, for staff, not one of their signs)."""

    def __init__(self) -> None:
        super().__init__(NOT_FOUND)


class SaleForbidden(SaleError):
    """The actor may not do this to this sale."""


class InvalidSale(SaleError):
    """The data is not acceptable; ``errors`` names each field at fault."""

    def __init__(self, errors: Sequence[FieldError]) -> None:
        self.errors = list(errors)
        super().__init__(" ".join(e.message for e in self.errors))


class DuplicateTicket(SaleError):
    """A ticket number is already in another sale of the agency. ``sale_id`` is that sale when
    the actor may see it (the user can be offered "edit the existing one"), else None."""

    def __init__(self, doc_number: str, sale_id: int | None) -> None:
        self.doc_number = doc_number
        self.sale_id = sale_id
        template = DUPLICATE_HIDDEN if sale_id is None else DUPLICATE_SEEN
        super().__init__(template.format(doc=doc_number))


@dataclass(frozen=True)
class SaleResult:
    sale: SaleView
    warnings: tuple[str, ...] = ()


def _audited(action: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Record a ``permission_denied`` audit event when the wrapped service function refuses the
    actor with ``SaleForbidden``. The function must take ``(db, actor, ...)``."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> R:
            try:
                return function(*args, **kwargs)
            except SaleForbidden:
                db, actor = args[0], args[1]
                audit.refused(
                    db,
                    action,
                    agency_id=actor.agency_id,
                    actor_user_id=actor.id,
                    ip=kwargs.get("ip"),
                    now=kwargs.get("now"),
                )
                raise

        return cast(Callable[P, R], wrapper)

    return decorate


# --- who may do what ------------------------------------------------------------------------


def _scope(actor: User) -> str:
    if permissions.can_edit_any_entry(actor):
        return ALL
    if permissions.can_enter_own_sales(actor):
        return OWN
    raise SaleForbidden(NO_PERMISSION)


def _own_signs(db: Session, actor: User) -> list[str]:
    return [link.staff_sign for link in get_signs(db, actor.agency_id, actor.id)]


def _may_see(db: Session, actor: User, staff_sign: str) -> bool:
    """The actor may see (and, within the day rules, write) a sale of this sign."""
    signs = visible_signs(db, actor)
    return signs is None or staff_sign in signs


def visible_signs(db: Session, actor: User) -> list[str] | None:
    """The staff signs whose sales the actor may see: None for every sign (manager, accountant),
    the signs linked to the actor for staff, and none for any other role."""
    if permissions.sees_all_signs(actor):
        return None
    if permissions.can_enter_own_sales(actor):
        return _own_signs(db, actor)
    return []


def _check_source(source: str) -> None:
    if source not in SALE_SOURCES:
        raise SaleError(BAD_SOURCE)


def _check_staff_day(scope: str, day: date, today: date) -> None:
    if scope == OWN and day != today:
        raise SaleForbidden(ONLY_TODAY)


def _reason_for_edit(scope: str, raw: str | None) -> str | None:
    try:
        return required_reason(raw) if scope == ALL else clean_reason(raw)
    except ReasonError as exc:
        raise InvalidReason(str(exc)) from None


def _resolve_sign(
    db: Session, actor: User, scope: str, requested: str | None, existing: str | None
) -> str:
    """The staff sign a sale is written under, checked against the actor's rights."""
    sign = None
    if requested is not None and requested.strip():
        try:
            sign = normalize_staff_sign(requested)
        except AccountError:
            raise InvalidSale([FieldError("staff_sign", BAD_SIGN)]) from None
    if scope == ALL:
        chosen = sign or existing
        if chosen is None:
            raise InvalidSale([FieldError("staff_sign", SIGN_REQUIRED)])
        return chosen
    own = _own_signs(db, actor)
    if not own:
        raise SaleForbidden(NO_SIGN)
    if sign is not None:
        if sign not in own:
            raise SaleForbidden(SIGN_NOT_YOURS)
        return sign
    if existing is not None:
        return existing
    if len(own) == 1:
        return own[0]
    raise InvalidSale([FieldError("staff_sign", SIGN_REQUIRED)])


def _sign_warnings(db: Session, actor: User, scope: str, sign: str) -> tuple[str, ...]:
    """A manager or accountant may write under any sign; tell them when nobody has it."""
    if scope != ALL:
        return ()
    linked = db.scalars(
        select(StaffSignLink.id)
        .where(StaffSignLink.agency_id == actor.agency_id, StaffSignLink.staff_sign == sign)
        .limit(1)
    ).first()
    if linked is not None:
        return ()
    return (f"Le signe {sign} n'est lié à aucun compte : personne ne le verra dans ses ventes.",)


# --- reading --------------------------------------------------------------------------------


def _view(head: Sale, version: SaleVersion, day: date) -> SaleView:
    return SaleView(
        id=head.id,
        agency_id=head.agency_id,
        report_day_id=head.report_day_id,
        day=day,
        staff_sign=head.staff_sign,
        status=head.status,
        version_no=version.version_no,
        created_by_user_id=head.created_by_user_id,
        ag_kind=version.ag_kind,
        partner_id=version.partner_id,
        ag_label=version.ag_label,
        partner_fee_id=version.partner_fee_id,
        fee_applied=version.fee_applied,
        fee_override_reason=version.fee_override_reason,
        pb=version.pb,
        cost_total=version.cost_total,
        clt=version.clt,
        ap=version.ap,
        client_refused_phone=version.client_refused_phone,
        payment_method=version.payment_method,
        tickets=lines_from_json(version.tickets),
        source=version.source,
        reason=version.reason,
        actor_user_id=version.actor_user_id,
        created_at=head.created_at,
        updated_at=head.updated_at,
    )


def _current(agency_id: int) -> Select[Sale, SaleVersion, date]:
    # populate_existing: rows already in the session are refreshed, never served stale.
    return (
        select(Sale, SaleVersion, ReportDay.day)
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
        .where(Sale.agency_id == agency_id)
        .execution_options(populate_existing=True)
    )


def _get_sale(db: Session, agency_id: int, sale_id: int) -> SaleView | None:
    row = db.execute(_current(agency_id).where(Sale.id == sale_id)).first()
    return None if row is None else _view(row[0], row[1], row[2])


def _sales_for_day(
    db: Session,
    agency_id: int,
    day: date,
    staff_signs: Collection[str] | None,
    include_voided: bool,
) -> list[SaleView]:
    if staff_signs is not None and len(staff_signs) == 0:
        return []
    query = _current(agency_id).where(ReportDay.day == day)
    if staff_signs is not None:
        query = query.where(Sale.staff_sign.in_(list(staff_signs)))
    if not include_voided:
        query = query.where(Sale.status == SALE_ACTIVE)
    return [
        _view(head, version, day_) for head, version, day_ in db.execute(query.order_by(Sale.id))
    ]


def get_sale_for(db: Session, actor: User, sale_id: int) -> SaleView | None:
    """The sale as it stands now, or None when it does not exist in the actor's agency or is not
    one the actor may see (a staff member's other signs)."""
    found = _get_sale(db, actor.agency_id, sale_id)
    if found is None or not _may_see(db, actor, found.staff_sign):
        return None
    return found


def sale_history_for(db: Session, actor: User, sale_id: int) -> list[SaleVersion]:
    """Every version of a sale the actor may see, oldest first (empty otherwise)."""
    if get_sale_for(db, actor, sale_id) is None:
        return []
    return list(
        db.scalars(
            select(SaleVersion)
            .where(SaleVersion.agency_id == actor.agency_id, SaleVersion.sale_id == sale_id)
            .order_by(SaleVersion.version_no)
            .execution_options(populate_existing=True)
        )
    )


def sales_for_day_for(
    db: Session,
    actor: User,
    day: date,
    *,
    signs: Collection[str] | None = None,
    include_voided: bool = False,
) -> list[SaleView]:
    """The sales of a day that the actor may see, in creation order. ``signs`` narrows them
    further (it can never widen them). Voided sales only with ``include_voided``."""
    allowed = visible_signs(db, actor)
    if allowed is None:
        wanted: Collection[str] | None = signs
    elif signs is None:
        wanted = allowed
    else:
        wanted = [s for s in signs if s in allowed]
    return _sales_for_day(db, actor.agency_id, day, wanted, include_voided)


MAX_DAYS_PER_QUERY = 366


def sales_for_days_for(db: Session, actor: User, wanted_days: Collection[date]) -> list[SaleView]:
    """The active sales of several days (at most 366) that the actor may see, in creation order,
    in one query. The same sales as ``sales_for_day_for`` gives day by day."""
    if len(wanted_days) > MAX_DAYS_PER_QUERY:
        raise ValueError(f"at most {MAX_DAYS_PER_QUERY} days at a time")
    allowed = visible_signs(db, actor)
    if not wanted_days or (allowed is not None and not allowed):
        return []
    query = _current(actor.agency_id).where(
        ReportDay.day.in_(list(wanted_days)), Sale.status == SALE_ACTIVE
    )
    if allowed is not None:
        query = query.where(Sale.staff_sign.in_(allowed))
    return [_view(head, version, day) for head, version, day in db.execute(query.order_by(Sale.id))]


def view_warnings(db: Session, actor: User, view: SaleView) -> tuple[str, ...]:
    """The warnings of a stored sale, as the read-back showed them: a direct sale priced below its
    cost, and (manager and accountant only) a sign that nobody is linked to."""
    found: list[str] = []
    if view.ag_kind != AG_PARTNER:
        below = below_cost_warning(view.pb, view.cost_total)
        if below is not None:
            found.append(below)
    found.extend(sign_warnings(db, actor, view.staff_sign))
    return tuple(found)


def sign_warnings(db: Session, actor: User, sign: str) -> tuple[str, ...]:
    """Tell a manager or accountant when nobody is linked to the sign a sale is written under
    (nobody else is ever told: staff can only write under their own signs)."""
    scope = ALL if permissions.can_edit_any_entry(actor) else OWN
    return _sign_warnings(db, actor, scope, sign)


def overrides_for_day(db: Session, actor: User, day: date) -> list[SaleView]:
    """The active sales of a day, within the actor's scope, whose current version carries a
    one-off fee (the queue B9 routes to accountant/manager validation)."""
    return [s for s in sales_for_day_for(db, actor, day) if s.fee_override_reason is not None]


_LOOKUP_CHUNK = 500


def active_ticket_lookup(
    db: Session, agency_id: int, doc_numbers: Collection[str]
) -> dict[str, tuple[int, int]]:
    """``{doc_number: (sale_id, report_day_id)}`` for the numbers that belong to an active sale of
    the agency. For the comparison (B8), which works for the agency, not for one agent."""
    wanted = list(dict.fromkeys(doc_numbers))
    found: dict[str, tuple[int, int]] = {}
    for start in range(0, len(wanted), _LOOKUP_CHUNK):
        chunk = wanted[start : start + _LOOKUP_CHUNK]
        rows = db.execute(
            select(SaleTicket.doc_number, SaleTicket.sale_id, Sale.report_day_id)
            .join(Sale, and_(Sale.agency_id == SaleTicket.agency_id, Sale.id == SaleTicket.sale_id))
            .where(
                SaleTicket.agency_id == agency_id,
                Sale.status == SALE_ACTIVE,
                SaleTicket.doc_number.in_(chunk),
            )
        )
        for doc, sale_id, day_id in rows:
            found[doc] = (sale_id, day_id)
    return found


def agency_sales_for_day(db: Session, agency_id: int, day: date) -> list[SaleView]:
    """Every active sale of the agency on a day, in creation order. For the comparison (B8), which
    works for the agency, not for one agent: no sign filter."""
    return _sales_for_day(db, agency_id, day, None, False)


def agency_sales_by_id(db: Session, agency_id: int, sale_ids: Collection[int]) -> list[SaleView]:
    """The active sales of the agency with these ids, as they stand now (the comparison, B8)."""
    wanted = list(dict.fromkeys(sale_ids))
    views: list[SaleView] = []
    for start in range(0, len(wanted), _LOOKUP_CHUNK):
        query = _current(agency_id).where(
            Sale.id.in_(wanted[start : start + _LOOKUP_CHUNK]), Sale.status == SALE_ACTIVE
        )
        views.extend(_view(head, version, day) for head, version, day in db.execute(query))
    return views


def check_sale_consistency(db: Session, agency_id: int) -> list[str]:
    """Problems in the stored sales of an agency, as sentences with ids only (empty when sound):
    every head must have the version it points at, on the same day and with the same status; an
    active sale's ``sale_tickets`` must equal the tickets of that version (numbers and order); a
    voided sale holds none. Usable as a periodic check."""
    heads = list(
        db.scalars(
            select(Sale)
            .where(Sale.agency_id == agency_id)
            .order_by(Sale.id)
            .execution_options(populate_existing=True)
        )
    )
    versions = {
        (v.sale_id, v.version_no): v
        for v in db.scalars(
            select(SaleVersion)
            .where(SaleVersion.agency_id == agency_id)
            .execution_options(populate_existing=True)
        )
    }
    latest: dict[int, int] = {}
    for sale_id, number in versions:
        latest[sale_id] = max(latest.get(sale_id, 0), number)
    held: dict[int, list[tuple[int, str]]] = {}
    for sale_id, position, doc in db.execute(
        select(SaleTicket.sale_id, SaleTicket.position, SaleTicket.doc_number).where(
            SaleTicket.agency_id == agency_id
        )
    ):
        held.setdefault(sale_id, []).append((position, doc))
    problems: list[str] = []
    for head in heads:
        label = f"sale {head.id}"
        current = versions.get((head.id, head.current_version))
        if current is None:
            problems.append(f"{label}: version {head.current_version} does not exist")
            continue
        if latest[head.id] > head.current_version:
            problems.append(f"{label}: a version above {head.current_version} exists")
        if current.report_day_id != head.report_day_id:
            problems.append(f"{label}: day differs from version {current.version_no}")
        if (head.status == SALE_VOIDED) != current.voided:
            problems.append(f"{label}: status differs from version {current.version_no}")
        tickets = sorted(held.get(head.id, []))
        if head.status == SALE_VOIDED:
            if tickets:
                problems.append(f"{label}: voided but holds {len(tickets)} ticket(s)")
            continue
        expected = sorted((t.position, t.doc_number) for t in lines_from_json(current.tickets))
        if tickets != expected:
            problems.append(f"{label}: tickets differ from version {current.version_no}")
    return problems


def _current_version(db: Session, head: Sale) -> SaleVersion:
    return db.scalars(
        select(SaleVersion)
        .where(
            SaleVersion.agency_id == head.agency_id,
            SaleVersion.sale_id == head.id,
            SaleVersion.version_no == head.current_version,
        )
        .execution_options(populate_existing=True)
    ).one()


def build_context(
    db: Session,
    actor: User,
    data: SaleInput,
    day: date,
    previous: SaleVersion | None = None,
) -> ValidationContext:
    """Look up the partner of ``data`` and its fee on ``day`` (and the fee ``previous`` was priced
    with, for an edit) in the actor's agency."""
    partner_context = None
    if (
        data.ag_kind == AG_PARTNER
        and isinstance(data.partner_id, int)
        and not isinstance(data.partner_id, bool)
    ):
        partner = partners.get_partner(db, actor.agency_id, data.partner_id)
        if partner is not None:
            fee = partners.fee_on(db, actor.agency_id, partner.id, day)
            same = previous is not None and previous.partner_id == partner.id
            carried = None
            if previous is not None and same and previous.fee_applied is not None:
                carried = CarriedFee(
                    previous.fee_applied, previous.partner_fee_id, previous.fee_override_reason
                )
            partner_context = PartnerContext(
                partner_id=partner.id,
                name=partner.name,
                is_active=partner.is_active or same,
                fee=None if fee is None else fee.fee,
                fee_id=None if fee is None else fee.id,
                carried=carried,
            )
    return ValidationContext(day=day, partner=partner_context)


# --- access to an existing sale -------------------------------------------------------------


def _fresh_sale(db: Session, actor: User, sale_id: int, *, lock: bool) -> Sale:
    query = (
        select(Sale)
        .where(Sale.agency_id == actor.agency_id, Sale.id == sale_id)
        .execution_options(populate_existing=True)
    )
    head = db.scalars(query.with_for_update() if lock else query).one_or_none()
    if head is None or not _may_see(db, actor, head.staff_sign):
        raise SaleNotFound
    return head


def _access(
    db: Session, actor: User, sale_id: int, today: date, *, lock: bool
) -> tuple[Sale, ReportDay]:
    """The one place that decides whether the actor may work on an existing sale: it exists in
    the actor's agency and (for staff) is one of their signs; the day is today for staff; the day
    is not closed; the sale is not voided. With ``lock`` the locks are taken day first, then the
    sale, and everything is re-read under them. Used by edit, void and preview alike."""
    scope = _scope(actor)
    head = _fresh_sale(db, actor, sale_id, lock=False)
    report_day = db.scalars(
        select(ReportDay)
        .where(ReportDay.agency_id == head.agency_id, ReportDay.id == head.report_day_id)
        .execution_options(populate_existing=True)
    ).one()
    _check_staff_day(scope, report_day.day, today)
    if lock:
        days.lock_day(db, actor, report_day, shared=True)
        head = _fresh_sale(db, actor, sale_id, lock=True)
        if head.report_day_id != report_day.id:
            raise StaleSale  # moved to another day while we waited
    days.require_writable(report_day)
    if head.status == SALE_VOIDED:
        raise SaleVoided
    return head, report_day


# --- writing helpers ------------------------------------------------------------------------


def _check_expected(head: Sale, expected_version: int | None) -> None:
    """Refuse a write built on an older version of the sale (checked under the sale's lock)."""
    if expected_version is not None and head.current_version != expected_version:
        raise StaleSale


def _find_duplicate(
    db: Session, actor: User, docs: Sequence[str], *, except_sale_id: int | None = None
) -> DuplicateTicket | None:
    query = select(SaleTicket.doc_number, SaleTicket.sale_id).where(
        SaleTicket.agency_id == actor.agency_id, SaleTicket.doc_number.in_(list(docs))
    )
    if except_sale_id is not None:
        query = query.where(SaleTicket.sale_id != except_sale_id)
    found = {doc: sale_id for doc, sale_id in db.execute(query)}
    for doc in docs:  # report the first in the order entered
        if doc in found:
            seen = get_sale_for(db, actor, found[doc]) is not None
            return DuplicateTicket(doc, found[doc] if seen else None)
    return None


def _check_no_duplicate(
    db: Session, actor: User, docs: Sequence[str], *, except_sale_id: int | None = None
) -> None:
    duplicate = _find_duplicate(db, actor, docs, except_sale_id=except_sale_id)
    if duplicate is not None:
        raise duplicate


def _raise_unique(
    db: Session, actor: User, exc: IntegrityError, docs: Sequence[str], except_id: int | None
) -> NoReturn:
    """A unique violation on the tickets is a duplicate; on the version number, a stale sale;
    anything else is re-raised."""
    if is_unique_violation(exc):
        duplicate = _find_duplicate(db, actor, docs, except_sale_id=except_id)
        if duplicate is not None:
            raise duplicate from None
        raise StaleSale from None
    raise exc


def _new_version(
    head: Sale,
    number: int,
    sale: ValidatedSale,
    *,
    sign: str,
    actor: User,
    source: str,
    reason: str | None,
    voided: bool,
    now: datetime,
    report_day_id: int | None = None,
) -> SaleVersion:
    return SaleVersion(
        agency_id=head.agency_id,
        sale_id=head.id,
        version_no=number,
        report_day_id=head.report_day_id if report_day_id is None else report_day_id,
        staff_sign=sign,
        ag_kind=sale.ag_kind,
        partner_id=sale.partner_id,
        ag_label=sale.ag_label,
        partner_fee_id=sale.partner_fee_id,
        fee_applied=sale.fee_applied,
        fee_override_reason=sale.fee_override_reason,
        pb=sale.pb,
        cost_total=sale.cost_total,
        clt=sale.clt,
        ap=sale.ap,
        client_refused_phone=sale.client_refused_phone,
        payment_method=sale.payment_method,
        tickets=[ticket_json(t) for t in sale.tickets],
        voided=voided,
        source=source,
        actor_user_id=actor.id,
        reason=reason,
        created_at=now,
    )


def _insert_tickets(db: Session, head: Sale, lines: Sequence[TicketLine], now: datetime) -> None:
    # In ticket-number order, so two requests with overlapping tickets lock them in the same order.
    for line in sorted(lines, key=lambda t: t.doc_number):
        db.add(
            SaleTicket(
                agency_id=head.agency_id,
                sale_id=head.id,
                doc_number=line.doc_number,
                position=line.position,
                created_at=now,
            )
        )


def _validated_from(row: SaleVersion) -> ValidatedSale:
    return ValidatedSale(
        ag_kind=row.ag_kind,
        partner_id=row.partner_id,
        ag_label=row.ag_label,
        tickets=lines_from_json(row.tickets),
        pb=row.pb,
        cost_total=row.cost_total,
        fee_applied=row.fee_applied,
        partner_fee_id=row.partner_fee_id,
        fee_override_reason=row.fee_override_reason,
        clt=row.clt,
        ap=row.ap,
        client_refused_phone=row.client_refused_phone,
        payment_method=row.payment_method,
    )


def _same_content(new: ValidatedSale, old: ValidatedSale) -> bool:
    """Same sale, ignoring what is not content: the warnings, and which fee row was used."""
    return replace(new, warnings=(), partner_fee_id=None) == replace(
        old, warnings=(), partner_fee_id=None
    )


def _audit(
    db: Session,
    event: str,
    actor: User,
    head: Sale,
    version_no: int,
    sale: ValidatedSale,
    *,
    now: datetime,
    ip: str | None,
    extra: dict[str, Any] | None = None,
) -> None:
    audit.record(
        db,
        event,
        agency_id=actor.agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={
            "sale_id": head.id,
            "version_no": version_no,
            "report_day_id": head.report_day_id,
            "ag_kind": sale.ag_kind,
            "partner_id": sale.partner_id,
            "ticket_count": len(sale.tickets),
            "pb": str(sale.pb),
            "cost_total": str(sale.cost_total),
            "fee_applied": None if sale.fee_applied is None else str(sale.fee_applied),
            "fee_override": sale.fee_override_reason is not None,
            "staff_sign": head.staff_sign,
            **(extra or {}),
        },
        now=now,
    )


def _reload(db: Session, actor: User, head: Sale) -> SaleView:
    """The sale just written, as a view (``one()``: it is in this transaction)."""
    row = db.execute(_current(actor.agency_id).where(Sale.id == head.id)).one()
    return _view(row[0], row[1], row[2])


# --- preview, create, edit, void, move ------------------------------------------------------


@_audited("preview_sale")
def preview_sale(
    db: Session,
    actor: User,
    data: SaleInput,
    *,
    day: date | None = None,
    sale_id: int | None = None,
    now: datetime | None = None,
) -> ValidatedSale | list[FieldError]:
    """The read-back of a sale (priced by the server, with its warnings) without writing
    anything: for a new sale, or for an edit of ``sale_id`` (which keeps its day and fee). It
    follows the access rules of the write and raises what the write would raise (not found,
    forbidden, day closed, voided, ``DuplicateTicket``)."""
    moment = now or utc_now()
    today = moment.date()
    scope = _scope(actor)
    previous = None
    if sale_id is not None:
        head, report_day = _access(db, actor, sale_id, today, lock=False)
        previous = _current_version(db, head)
        chosen = report_day.day
        _resolve_sign(db, actor, scope, data.staff_sign, head.staff_sign)
    else:
        chosen = day if day is not None else today
        days.check_day(chosen, today)
        _resolve_sign(db, actor, scope, data.staff_sign, None)
        _check_staff_day(scope, chosen, today)
        existing = days.get_day(db, actor.agency_id, chosen)
        if existing is not None:
            days.require_writable(existing)
    result = validate(data, build_context(db, actor, data, chosen, previous))
    if isinstance(result, list):
        return result
    _check_no_duplicate(db, actor, [t.doc_number for t in result.tickets], except_sale_id=sale_id)
    return result


@_audited("create_sale")
def create_sale(
    db: Session,
    actor: User,
    data: SaleInput,
    *,
    day: date | None = None,
    now: datetime | None = None,
    ip: str | None = None,
    source: str = "form",
) -> SaleResult:
    """Validate, price and store a new sale (version 1) in the actor's agency; the caller commits.

    ``day`` is today unless a manager or accountant names another day. Errors: ``SaleForbidden``,
    ``InvalidSale`` (``.errors``), ``DuplicateTicket``, ``days.DayError`` / ``days.DayClosed``.
    After any of them the caller rolls the session back.
    """
    _check_source(source)
    moment = now or utc_now()
    today = moment.date()
    chosen = day if day is not None else today
    days.check_day(chosen, today)
    scope = _scope(actor)
    sign = _resolve_sign(db, actor, scope, data.staff_sign, None)
    _check_staff_day(scope, chosen, today)
    result = validate(data, build_context(db, actor, data, chosen))
    if isinstance(result, list):
        raise InvalidSale(result)
    docs = [t.doc_number for t in result.tickets]
    _check_no_duplicate(db, actor, docs)
    if after_duplicate_check is not None:
        after_duplicate_check()
    report_day = days.get_or_open_day(db, actor, chosen, now=moment, ip=ip)
    days.lock_day(db, actor, report_day, shared=True)
    days.require_writable(report_day)
    head = Sale(
        agency_id=actor.agency_id,
        report_day_id=report_day.id,
        staff_sign=sign,
        created_by_user_id=actor.id,
        current_version=1,
        status=SALE_ACTIVE,
        created_at=moment,
        updated_at=moment,
    )
    try:
        with db.begin_nested():
            db.add(head)
            db.flush()
            db.add(
                _new_version(
                    head,
                    1,
                    result,
                    sign=sign,
                    actor=actor,
                    source=source,
                    reason=None,
                    voided=False,
                    now=moment,
                )  # fmt: skip
            )
            _insert_tickets(db, head, result.tickets, moment)
            db.flush()
    except IntegrityError as exc:
        _raise_unique(db, actor, exc, docs, None)
    _audit(db, audit.SALE_CREATED, actor, head, 1, result, now=moment, ip=ip)
    warnings = result.warnings + _sign_warnings(db, actor, scope, sign)
    return SaleResult(_reload(db, actor, head), warnings)


@_audited("edit_sale")
def edit_sale(
    db: Session,
    actor: User,
    sale_id: int,
    data: SaleInput,
    reason: str | None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
    source: str = "form",
    expected_version: int | None = None,
) -> SaleResult:
    """Replace the content of a sale (tickets, price, client, payment...) with a new version.

    Manager and accountant must give a ``reason``; staff, editing their own sale of today, need
    not. The day stays (``move_sale_day`` changes it). ``SaleError`` when nothing changed, the
    sale is voided, or it changed under us ("rechargez"). ``expected_version`` is the version the
    form was built on: when the sale has moved on, the edit is refused as stale.
    """
    _check_source(source)
    moment = now or utc_now()
    head, report_day = _access(db, actor, sale_id, moment.date(), lock=True)
    _check_expected(head, expected_version)
    scope = _scope(actor)
    clean = _reason_for_edit(scope, reason)
    sign = _resolve_sign(db, actor, scope, data.staff_sign, head.staff_sign)
    previous = _current_version(db, head)
    result = validate(data, build_context(db, actor, data, report_day.day, previous))
    if isinstance(result, list):
        raise InvalidSale(result)
    if sign == previous.staff_sign and _same_content(result, _validated_from(previous)):
        raise SaleError(UNCHANGED)
    docs = [t.doc_number for t in result.tickets]
    _check_no_duplicate(db, actor, docs, except_sale_id=head.id)
    if after_duplicate_check is not None:
        after_duplicate_check()
    number = head.current_version + 1
    try:
        with db.begin_nested():
            db.add(
                _new_version(
                    head,
                    number,
                    result,
                    sign=sign,
                    actor=actor,
                    source=source,
                    reason=clean,
                    voided=False,
                    now=moment,
                )  # fmt: skip
            )
            head.current_version = number
            head.staff_sign = sign
            head.updated_at = moment
            held = list(
                db.scalars(
                    select(SaleTicket.doc_number)
                    .where(SaleTicket.agency_id == head.agency_id, SaleTicket.sale_id == head.id)
                    .order_by(SaleTicket.position)
                )
            )
            if held != docs:
                db.execute(
                    delete(SaleTicket).where(
                        SaleTicket.agency_id == head.agency_id, SaleTicket.sale_id == head.id
                    )
                )
                _insert_tickets(db, head, result.tickets, moment)
            db.flush()
    except IntegrityError as exc:
        _raise_unique(db, actor, exc, docs, head.id)
    _audit(db, audit.SALE_EDITED, actor, head, number, result, now=moment, ip=ip)
    warnings = result.warnings + _sign_warnings(db, actor, scope, sign)
    return SaleResult(_reload(db, actor, head), warnings)


@_audited("void_sale")
def void_sale(
    db: Session,
    actor: User,
    sale_id: int,
    reason: str | None,
    *,
    now: datetime | None = None,
    ip: str | None = None,
    expected_version: int | None = None,
) -> SaleView:
    """Cancel a sale (a new version marked void). The reason is mandatory for everybody. The
    sale leaves the day's totals, its ticket numbers become free, its history stays.
    ``expected_version`` as for ``edit_sale``."""
    moment = now or utc_now()
    head, _ = _access(db, actor, sale_id, moment.date(), lock=True)
    _check_expected(head, expected_version)
    try:
        clean = required_reason(reason)
    except ReasonError as exc:
        raise InvalidReason(str(exc)) from None
    previous = _current_version(db, head)
    number = head.current_version + 1
    kept = _validated_from(previous)
    try:
        with db.begin_nested():
            db.add(
                _new_version(
                    head,
                    number,
                    kept,
                    sign=head.staff_sign,
                    actor=actor,
                    source=previous.source,
                    reason=clean,
                    voided=True,
                    now=moment,
                )  # fmt: skip
            )
            head.current_version = number
            head.status = SALE_VOIDED
            head.updated_at = moment
            db.execute(
                delete(SaleTicket).where(
                    SaleTicket.agency_id == head.agency_id, SaleTicket.sale_id == head.id
                )
            )
            db.flush()
    except IntegrityError as exc:
        _raise_unique(db, actor, exc, [], head.id)
    _audit(db, audit.SALE_VOIDED, actor, head, number, kept, now=moment, ip=ip)
    return _reload(db, actor, head)


@_audited("move_sale_day")
def move_sale_day(
    db: Session,
    actor: User,
    sale_id: int,
    new_day: date,
    reason: str | None,
    *,
    system: bool = False,
    source: str = "comparison",
    now: datetime | None = None,
    ip: str | None = None,
) -> SaleView:
    """Move an active sale to another report day (a ticket found in a later Amadeus report, B8).

    Allowed for manager and accountant, and, with ``system=True``, for the comparison itself
    acting for whoever uploaded the report (never pass ``system`` from a web route). Both days
    must be writable (not closed); they are locked in id order, then the sale. A reason is
    mandatory. It appends a version carrying the new day and changes ``sales.report_day_id``.
    """
    _check_source(source)
    if not system and not permissions.can_edit_any_entry(actor):
        raise SaleForbidden(MOVE_FORBIDDEN)
    moment = now or utc_now()
    days.check_day(new_day, moment.date())
    try:
        clean = required_reason(reason)
    except ReasonError as exc:
        raise InvalidReason(str(exc)) from None
    head = db.scalars(
        select(Sale)
        .where(Sale.agency_id == actor.agency_id, Sale.id == sale_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if head is None:
        raise SaleNotFound
    if head.status == SALE_VOIDED:
        raise SaleVoided
    old_day = db.scalars(
        select(ReportDay)
        .where(ReportDay.agency_id == head.agency_id, ReportDay.id == head.report_day_id)
        .execution_options(populate_existing=True)
    ).one()
    target = days.get_or_open_day(db, actor, new_day, now=moment, ip=ip)
    if target.id == old_day.id:
        raise SaleError(SAME_DAY)
    for report_day in sorted((old_day, target), key=lambda d: d.id):
        days.lock_day(db, actor, report_day, shared=True)
        days.require_writable(report_day)
    locked = db.scalars(
        select(Sale)
        .where(Sale.agency_id == actor.agency_id, Sale.id == sale_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if locked.report_day_id != old_day.id:
        raise StaleSale
    if locked.status == SALE_VOIDED:
        raise SaleVoided
    previous = _current_version(db, locked)
    number = locked.current_version + 1
    kept = _validated_from(previous)
    try:
        with db.begin_nested():
            db.add(
                _new_version(
                    locked,
                    number,
                    kept,
                    sign=locked.staff_sign,
                    actor=actor,
                    source=source,
                    reason=clean,
                    voided=False,
                    now=moment,
                    report_day_id=target.id,
                )  # fmt: skip
            )
            locked.report_day_id = target.id
            locked.current_version = number
            locked.updated_at = moment
            db.flush()
    except IntegrityError as exc:
        _raise_unique(db, actor, exc, [], locked.id)
    _audit(
        db, audit.SALE_MOVED, actor, locked, number, kept, now=moment, ip=ip,
        extra={"from_report_day_id": old_day.id, "to_report_day_id": target.id},
    )  # fmt: skip
    return _reload(db, actor, locked)
