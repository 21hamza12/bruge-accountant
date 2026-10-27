"""The pure rules of a sale: data shapes, validation and pricing, and the helpers on a sale.

Nothing here reads or writes the database (``validate`` receives what it needs in a
``ValidationContext``), so every rule is testable on its own. ``app/sales.py`` is the service
that looks things up, stores them, and re-exports the public names of this module.

Rules (docs/design/05-daily-comparison.md section 2; decisions 4, 13, 21, 22):

* **P.B of a direct sale** is typed by the agent (``>= 0``). Below the cost of its tickets it is
  accepted, with a warning for the read-back (``ValidatedSale.warnings``).
* **P.B of a partner sale** is computed here and nowhere else: the sum of the TOTAL DOC of its
  tickets plus the fee times the NUMBER OF TICKETS. The fee is the partner's ``fee_on`` the sale's
  day, or a one-off override (per ticket) with a mandatory reason. A price typed for a partner
  sale is ignored. Editing a partner sale keeps the fee (or override and its reason) it was priced
  with, so a change of cost recomputes the price with the same fee; ``reset_fee`` asks for the
  standard fee of the day instead. A partner without a fee is refused, naming the partner.
* **Limits**: the cost of a sale and its P.B (and an override fee times the number of tickets)
  must stay under ``money.MAX_AMOUNT``, the size of the NUMERIC(14,2) columns.
* **Issue date**: a ticket may carry the day it was issued (``TicketInput.issue_date``, stored in
  the ticket JSON); the newest one is the day the sale is filed under (``latest_issue_date``, used
  by ``app.sales.filing_day``).
* **Per-line P.B for the exports** (``pb_per_line``): the full price on the first ticket (position
  1, the order entered) and 0 on the others.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.auth.phone import InvalidPhoneError, normalize_phone
from app.formatting import money_fr
from app.models import (
    AG_DIRECT,
    AG_LABEL_DIRECT,
    AG_PARTNER,
    PAX_TYPES,
    PAYMENT_METHODS,
    SALE_VOIDED,
    TICKET_TYPES,
)
from app.money import MAX_AMOUNT, MESSAGE_FORMAT, MoneyError, to_amount
from app.parsing.amadeus import clean_text
from app.reasons import ReasonError, required_reason

MAX_TICKETS_PER_SALE = 20
MAX_NAME_LENGTH = 120
MAX_PAX_NAME_LENGTH = 100
ZERO = Decimal("0.00")

_DIGITS = re.compile(r"[0-9]+", re.ASCII)
TICKET_DIGITS = 10  # the number as Amadeus exports it (3940299438)
PREFIXED_DIGITS = 3 + TICKET_DIGITS  # airline code + number (495-3940299438)
_AIRLINE = re.compile(r"[A-Z0-9]{2,3}", re.ASCII)
_RLOC = re.compile(r"[A-Z0-9]{4,16}", re.ASCII)

BAD_TEXT = "Texte invalide."
CLT_REQUIRED = "Le nom du client est obligatoire pour une vente directe."
NAME_TOO_LONG = f"Ce champ ne peut pas dépasser {MAX_NAME_LENGTH} caractères."
PB_REQUIRED = "Saisissez le prix P.B."
TOO_LARGE = "Montant trop grand."
NO_TICKETS = "Ajoutez au moins un billet."
BAD_TICKET = "Billet invalide."
TOO_MANY_TICKETS = f"Une vente ne peut pas avoir plus de {MAX_TICKETS_PER_SALE} billets."
DUPLICATE_IN_SALE = "Ce numéro de billet est déjà dans cette vente."
BAD_DOC_NUMBER = "Numéro du billet : 10 chiffres (ou compagnie + 10 chiffres)."
BAD_DOC_PREFIX = "Le préfixe ne correspond pas à la compagnie (A/L)."
BAD_AIRLINE = "Le code de la compagnie doit avoir 2 ou 3 caractères (lettres ou chiffres)."
BAD_PAX_NAME = f"Le nom du passager est obligatoire ({MAX_PAX_NAME_LENGTH} caractères au plus)."
BAD_PAX_TYPE = "Le type de passager doit être ADT, CHD ou INF."
BAD_RLOC = "La référence de réservation (RLOC) doit avoir de 4 à 16 lettres ou chiffres."
BAD_TRNC = "Le type de billet doit être " + ", ".join(TICKET_TYPES) + "."
BAD_AG_KIND = "Type de vente invalide."
BAD_PAYMENT = "Choisissez un mode de paiement."
PARTNER_UNKNOWN = "Choisissez un partenaire."
PARTNER_INACTIVE = "Ce partenaire est désactivé."
DIRECT_WITH_PARTNER = "Une vente directe n'a pas de partenaire."
DIRECT_WITH_OVERRIDE = "Un tarif particulier ne concerne qu'une vente partenaire."
OVERRIDE_REASON_REQUIRED = "Un tarif particulier exige un motif (5 caractères au moins)."
REFUSED_WITH_PHONE = "Le client a refusé de donner son numéro : laissez le numéro vide."
BAD_FLAG = "Valeur invalide."
BAD_ISSUE_DATE = "La date d'émission du billet est invalide."
FUTURE_ISSUE_DATE = "La date d'émission du billet est dans le futur."


def _no_fee(name: str) -> str:
    return f"Aucun tarif n'est défini pour le partenaire {name} à cette date."


def _below_cost(pb: Decimal, cost: Decimal) -> str:
    return (
        f"Le prix P.B ({money_fr(pb)} MRU) est inférieur au coût des billets "
        f"({money_fr(cost)} MRU)."
    )


def below_cost_warning(pb: Decimal, cost: Decimal) -> str | None:
    """The read-back warning for a price under the cost, or None."""
    return _below_cost(pb, cost) if pb < cost else None


# --- input and validated data ---------------------------------------------------------------


@dataclass(frozen=True)
class FieldError:
    """A mistake in one field: ``field`` is ``pb``, ``clt``, ``tickets[1].doc_number``..."""

    field: str
    message: str


@dataclass(frozen=True)
class TicketInput:
    doc_number: str
    airline: str
    total_doc: Decimal | int | str
    pax_name: str
    pax_type: str
    rloc: str
    trnc: str
    issue_date: date | None = None  # the day the ticket was issued (Amadeus FA line)


@dataclass(frozen=True)
class SaleInput:
    """What the agent typed. ``tickets`` keep the order entered (position 1 is the first)."""

    tickets: Sequence[TicketInput]
    ag_kind: str = AG_DIRECT
    partner_id: int | None = None
    pb: Decimal | int | str | None = None  # direct sales only; ignored for a partner sale
    clt: str | None = None
    ap: str | None = None
    client_refused_phone: bool = False
    payment_method: str = ""
    staff_sign: str | None = None  # manager and accountant choose; staff default to their own
    fee_override: Decimal | int | str | None = None  # per ticket, partner sales only
    fee_override_reason: str | None = None
    reset_fee: bool = False  # on an edit: the standard fee of the day, not the one carried


@dataclass(frozen=True)
class CarriedFee:
    """The fee a partner sale was priced with (the previous version), kept on an edit."""

    fee: Decimal
    fee_id: int | None
    override_reason: str | None


@dataclass(frozen=True)
class PartnerContext:
    partner_id: int
    name: str
    is_active: bool
    fee: Decimal | None  # the partner's fee on the sale's day, None when there is none
    fee_id: int | None
    carried: CarriedFee | None = None


@dataclass(frozen=True)
class ValidationContext:
    """What ``validate`` needs from the database, looked up by the caller."""

    day: date
    partner: PartnerContext | None = None


@dataclass(frozen=True)
class TicketLine:
    doc_number: str
    airline: str
    total_doc: Decimal
    pax_name: str
    pax_type: str
    rloc: str
    trnc: str
    position: int
    issue_date: date | None = None


@dataclass(frozen=True)
class ValidatedSale:
    """A sale checked and priced, ready to store; ``warnings`` are for the read-back."""

    ag_kind: str
    partner_id: int | None
    ag_label: str
    tickets: tuple[TicketLine, ...]
    pb: Decimal
    cost_total: Decimal
    fee_applied: Decimal | None
    partner_fee_id: int | None
    fee_override_reason: str | None
    clt: str | None
    ap: str | None
    client_refused_phone: bool
    payment_method: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SaleView:
    """A sale as it stands now: the head row with its current version."""

    id: int
    agency_id: int
    report_day_id: int
    day: date
    staff_sign: str
    status: str
    version_no: int
    created_by_user_id: int
    ag_kind: str
    partner_id: int | None
    ag_label: str
    partner_fee_id: int | None
    fee_applied: Decimal | None
    fee_override_reason: str | None
    override_digest: str | None
    pb: Decimal
    cost_total: Decimal
    clt: str | None
    ap: str | None
    client_refused_phone: bool
    payment_method: str
    tickets: tuple[TicketLine, ...]
    source: str
    reason: str | None
    actor_user_id: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DayTotals:
    sales: int
    tickets: int
    revenue: Decimal  # sum of P.B
    cost: Decimal  # sum of TOTAL DOC
    profit: Decimal  # revenue - cost


@dataclass(frozen=True)
class TicketRow:
    """One ticket of an active sale, flattened for the comparison with Amadeus (B8) and the
    exports (B10). ``pb_line`` is the per-line P.B (full price on the first ticket, 0 after)."""

    doc_number: str
    airline: str
    total_doc: Decimal
    pax_name: str
    pax_type: str
    rloc: str
    trnc: str
    staff_sign: str
    sale_id: int
    report_day_id: int
    position: int
    sale_pb: Decimal
    pb_line: Decimal


# --- validate -------------------------------------------------------------------------------


def _short_text(raw: object, limit: int) -> tuple[str | None, str | None]:
    """``(text, error)``: the cleaned text, empty -> None; an error for a non-text or too long."""
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, BAD_TEXT
    text = " ".join(clean_text(raw).split())
    if len(text) > limit:
        return None, NAME_TOO_LONG
    return (text or None), None


def _amount(raw: object) -> tuple[Decimal | None, str | None]:
    """``(amount, error)`` for an amount typed or passed in: ``to_amount`` for a Decimal, int or
    text; anything else (a float, a bool, None) is refused."""
    if isinstance(raw, bool) or not isinstance(raw, Decimal | int | str):
        return None, MESSAGE_FORMAT
    try:
        return to_amount(raw), None
    except MoneyError as exc:
        return None, str(exc)


def _code(raw: object) -> str | None:
    """A code typed by a person (airline, passenger type, RLOC, ticket type) cleaned and in
    capitals; None when it is not text."""
    if not isinstance(raw, str):
        return None
    return clean_text(raw).upper()


def _has_override(data: SaleInput) -> bool:
    """A one-off fee was given (a blank text counts as nothing)."""
    if data.fee_override is None:
        return False
    return not (isinstance(data.fee_override, str) and not data.fee_override.strip())


def ticket_number(raw: object, airline: str | None) -> tuple[str | None, str | None]:
    """``(number, error)``: the 10 digits a ticket is stored and compared with, from what a person
    typed. Spaces, "-" and "/" are ignored; the characters must be ASCII digits. Ten digits are
    kept as they are; thirteen are the airline code plus the number: the first three must be the
    ticket's A/L and the last ten are kept (``495-3940281465`` becomes ``3940281465``). Anything
    else is refused. This is the one place that normalises a ticket number."""
    if not isinstance(raw, str):
        return None, BAD_DOC_NUMBER
    compact = "".join(raw.replace("-", "").replace("/", "").split())
    if _DIGITS.fullmatch(compact) is None:
        return None, BAD_DOC_NUMBER
    if len(compact) == TICKET_DIGITS:
        return compact, None
    if len(compact) == PREFIXED_DIGITS:
        if airline is not None and compact[:3] == airline:
            return compact[3:], None
        return None, BAD_DOC_PREFIX
    return None, BAD_DOC_NUMBER


def _validate_ticket(raw: TicketInput, index: int, errs: list[FieldError]) -> TicketLine | None:
    prefix = f"tickets[{index}]."
    start = len(errs)

    def bad(name: str, message: str) -> None:
        errs.append(FieldError(prefix + name, message))

    airline = _code(raw.airline)
    if airline is None or _AIRLINE.fullmatch(airline) is None:
        bad("airline", BAD_AIRLINE)
    doc, doc_error = ticket_number(raw.doc_number, airline)
    if doc is None:
        bad("doc_number", doc_error or BAD_DOC_NUMBER)
    cost, cost_error = _amount(raw.total_doc)
    if cost is None:
        bad("total_doc", cost_error or BAD_TEXT)
    name, _ = _short_text(raw.pax_name, MAX_PAX_NAME_LENGTH)
    if name is None:
        bad("pax_name", BAD_PAX_NAME)
    pax_type = _code(raw.pax_type)
    if pax_type not in PAX_TYPES:
        bad("pax_type", BAD_PAX_TYPE)
    rloc = _code(raw.rloc)
    if rloc is None or _RLOC.fullmatch(rloc) is None:
        bad("rloc", BAD_RLOC)
    trnc = _code(raw.trnc)
    if trnc not in TICKET_TYPES:
        bad("trnc", BAD_TRNC)
    issue_date = raw.issue_date
    if issue_date is not None and (
        isinstance(issue_date, datetime) or not isinstance(issue_date, date)
    ):
        bad("issue_date", BAD_ISSUE_DATE)
    if len(errs) > start or not (doc and airline and name and pax_type and rloc and trnc):
        return None
    if cost is None:
        return None
    return TicketLine(doc, airline, cost, name, pax_type, rloc, trnc, index + 1, issue_date)


def latest_issue_date(tickets: object) -> date | None:
    """The day a sale is filed under: the issue date of its newest ticket (for an exchange, the new
    ticket), or None when no ticket has one. Anything that is not a plain date is ignored here
    (``validate`` refuses it)."""
    if not isinstance(tickets, Sequence) or isinstance(tickets, str | bytes):
        return None
    found = [
        d
        for t in tickets
        if isinstance(d := getattr(t, "issue_date", None), date) and not isinstance(d, datetime)
    ]
    return max(found) if found else None


def _validate_tickets(raw: object, errs: list[FieldError]) -> tuple[TicketLine, ...] | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes) or len(raw) == 0:
        errs.append(FieldError("tickets", NO_TICKETS))
        return None
    if len(raw) > MAX_TICKETS_PER_SALE:
        errs.append(FieldError("tickets", TOO_MANY_TICKETS))
        return None
    start = len(errs)
    lines: list[TicketLine] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, TicketInput):
            errs.append(FieldError(f"tickets[{index}]", BAD_TICKET))
            continue
        line = _validate_ticket(item, index, errs)
        if line is None:
            continue
        if line.doc_number in seen:
            errs.append(FieldError(f"tickets[{index}].doc_number", DUPLICATE_IN_SALE))
            continue
        seen.add(line.doc_number)
        lines.append(line)
    return tuple(lines) if len(errs) == start else None


@dataclass(frozen=True)
class _Fee:
    per_ticket: Decimal
    fee_id: int | None
    override_reason: str | None


def _override_reason(raw: str | None) -> tuple[str | None, str | None]:
    """``(reason, error)`` for the reason of a one-off fee."""
    try:
        return required_reason(raw), None
    except ReasonError as exc:
        missing = not isinstance(raw, str) or not raw.strip()
        return None, OVERRIDE_REASON_REQUIRED if missing else str(exc)


def _resolve_fee(
    data: SaleInput, context: ValidationContext, errs: list[FieldError]
) -> tuple[PartnerContext, _Fee] | None:
    """The partner and the per-ticket fee to charge, or None (errors added)."""
    partner = context.partner
    if (
        not isinstance(data.partner_id, int)
        or isinstance(data.partner_id, bool)
        or partner is None
        or partner.partner_id != data.partner_id
    ):
        errs.append(FieldError("partner_id", PARTNER_UNKNOWN))
        return None
    if not partner.is_active:
        errs.append(FieldError("partner_id", PARTNER_INACTIVE))
        return None
    if _has_override(data):
        # Owner policy: staff may override the fee too, with a mandatory reason. Every override is
        # visible (audit ``fee_override``, ``overrides_for_day``); B9 routes them to validation.
        amount, message = _amount(data.fee_override)
        reason, reason_error = _override_reason(data.fee_override_reason)
        if amount is None:
            errs.append(FieldError("fee_override", message or BAD_TEXT))
        if reason is None:
            errs.append(FieldError("fee_override_reason", reason_error or OVERRIDE_REASON_REQUIRED))
        if amount is None or reason is None:
            return None
        return partner, _Fee(amount, None, reason)
    if partner.carried is not None and not data.reset_fee:
        carried = partner.carried
        return partner, _Fee(carried.fee, carried.fee_id, carried.override_reason)
    if partner.fee is None:
        errs.append(FieldError("partner_id", _no_fee(partner.name)))
        return None
    return partner, _Fee(partner.fee, partner.fee_id, None)


def _validate_contact(data: SaleInput, errs: list[FieldError]) -> tuple[str | None, str | None]:
    """``(clt, ap)`` cleaned; mistakes added to ``errs``."""
    clt, clt_error = _short_text(data.clt, MAX_NAME_LENGTH)
    if clt_error:
        errs.append(FieldError("clt", clt_error))
    elif clt is None and data.ag_kind == AG_DIRECT:
        errs.append(FieldError("clt", CLT_REQUIRED))
    ap: str | None = None
    if not isinstance(data.client_refused_phone, bool):
        errs.append(FieldError("client_refused_phone", BAD_FLAG))
    elif data.ap is not None and not isinstance(data.ap, str):
        errs.append(FieldError("ap", BAD_TEXT))
    elif data.ap is not None and data.ap.strip():
        if data.client_refused_phone:
            errs.append(FieldError("ap", REFUSED_WITH_PHONE))
        else:
            try:
                ap = normalize_phone(data.ap)
            except InvalidPhoneError as exc:
                errs.append(FieldError("ap", str(exc)))
    return clt, ap


def validate(data: SaleInput, context: ValidationContext) -> ValidatedSale | list[FieldError]:
    """Check and price a sale. Pure: ``context`` carries what the database knows (the partner and
    its fee on the sale's day). Returns the validated sale or every mistake found."""
    errs: list[FieldError] = []
    tickets = _validate_tickets(data.tickets, errs)
    if data.ag_kind not in (AG_DIRECT, AG_PARTNER):
        errs.append(FieldError("ag_kind", BAD_AG_KIND))
    if data.payment_method not in PAYMENT_METHODS:
        errs.append(FieldError("payment_method", BAD_PAYMENT))
    clt, ap = _validate_contact(data, errs)

    pb = ZERO
    priced: tuple[PartnerContext, _Fee] | None = None
    if data.ag_kind == AG_DIRECT:
        if data.partner_id is not None:
            errs.append(FieldError("partner_id", DIRECT_WITH_PARTNER))
        if _has_override(data):
            errs.append(FieldError("fee_override", DIRECT_WITH_OVERRIDE))
        typed, message = _amount(data.pb)
        if data.pb is None:
            errs.append(FieldError("pb", PB_REQUIRED))
        elif typed is None:
            errs.append(FieldError("pb", message or PB_REQUIRED))
        else:
            pb = typed
    elif data.ag_kind == AG_PARTNER:
        priced = _resolve_fee(data, context, errs)

    cost_total = ZERO
    if tickets is not None:
        cost_total = sum((t.total_doc for t in tickets), ZERO)
        if cost_total >= MAX_AMOUNT:
            errs.append(FieldError("tickets", TOO_LARGE))
        if priced is not None:
            fees = priced[1].per_ticket * len(tickets)
            pb = cost_total + fees
            if fees >= MAX_AMOUNT:
                field = "fee_override" if priced[1].override_reason is not None else "pb"
                errs.append(FieldError(field, TOO_LARGE))
            elif pb >= MAX_AMOUNT:
                errs.append(FieldError("pb", TOO_LARGE))
    if errs or tickets is None:
        return errs

    if priced is not None:
        partner, fee = priced
        return ValidatedSale(
            AG_PARTNER, partner.partner_id, partner.name, tickets, pb, cost_total,
            fee.per_ticket, fee.fee_id, fee.override_reason,
            clt, ap, data.client_refused_phone, data.payment_method,
        )  # fmt: skip
    warnings = (_below_cost(pb, cost_total),) if pb < cost_total else ()
    return ValidatedSale(
        AG_DIRECT, None, AG_LABEL_DIRECT, tickets, pb, cost_total, None, None, None,
        clt, ap, data.client_refused_phone, data.payment_method, warnings,
    )  # fmt: skip


# --- pure helpers on a sale -----------------------------------------------------------------


class _HasTicketLines(Protocol):
    @property
    def pb(self) -> Decimal: ...
    @property
    def tickets(self) -> Sequence[TicketLine]: ...


def sale_cost(sale: _HasTicketLines) -> Decimal:
    """The cost of a sale: the sum of the TOTAL DOC of its tickets."""
    return sum((t.total_doc for t in sale.tickets), ZERO)


def sale_profit(sale: _HasTicketLines) -> Decimal:
    """P.B minus the cost (negative when sold below cost). For a partner sale: fee x tickets."""
    return sale.pb - sale_cost(sale)


def pb_per_line(sale: _HasTicketLines) -> list[tuple[str, Decimal]]:
    """``(doc_number, P.B)`` per ticket for an export (decision 21): the full P.B on the first
    ticket (lowest position, i.e. the order entered) and 0 on the others, so the lines add up
    to the sale's P.B."""
    ordered = sorted(sale.tickets, key=lambda t: t.position)
    return [(t.doc_number, sale.pb if n == 0 else ZERO) for n, t in enumerate(ordered)]


def day_totals(views: Iterable[SaleView]) -> DayTotals:
    """Totals of the active sales among ``views``; voided sales never count."""
    count = tickets = 0
    revenue = cost = ZERO
    for view in views:
        if view.status == SALE_VOIDED:
            continue
        count += 1
        tickets += len(view.tickets)
        revenue += view.pb
        cost += sale_cost(view)
    return DayTotals(count, tickets, revenue, cost, revenue - cost)


def ticket_rows(views: Iterable[SaleView]) -> list[TicketRow]:
    """The tickets of the active sales among ``views``, in the order of the sales then of the
    entry, each with its per-line P.B. Voided sales are skipped."""
    rows: list[TicketRow] = []
    for view in views:
        if view.status == SALE_VOIDED:
            continue
        lines = dict(pb_per_line(view))
        for t in sorted(view.tickets, key=lambda item: item.position):
            rows.append(
                TicketRow(
                    doc_number=t.doc_number,
                    airline=t.airline,
                    total_doc=t.total_doc,
                    pax_name=t.pax_name,
                    pax_type=t.pax_type,
                    rloc=t.rloc,
                    trnc=t.trnc,
                    staff_sign=view.staff_sign,
                    sale_id=view.id,
                    report_day_id=view.report_day_id,
                    position=t.position,
                    sale_pb=view.pb,
                    pb_line=lines[t.doc_number],
                )
            )
    return rows


# --- the stored form of the tickets ---------------------------------------------------------


def override_digest(
    partner_id: int | None,
    fee_applied: Decimal | None,
    reason: str | None,
    tickets: Iterable[TicketLine],
) -> str | None:
    """What a validator approves on a one-off partner fee: the partner, the fee, its reason and
    the tickets with their costs (16 hex digits). None when the sale has no override. A change of
    any of them is another digest, so an approval never covers it."""
    if reason is None or fee_applied is None:
        return None
    fee = format(fee_applied.normalize(), "f")
    docs = "|".join(f"{t.doc_number}:{format(t.total_doc.normalize(), 'f')}" for t in tickets)
    text = f"{partner_id}\x1f{fee}\x1f{reason}\x1f{docs}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def ticket_json(line: TicketLine) -> dict[str, Any]:
    """A ticket as stored in ``sale_versions.tickets`` (money as text)."""
    stored: dict[str, Any] = {
        "doc_number": line.doc_number,
        "airline": line.airline,
        "total_doc": str(line.total_doc),
        "pax_name": line.pax_name,
        "pax_type": line.pax_type,
        "rloc": line.rloc,
        "trnc": line.trnc,
        "position": line.position,
    }
    if (
        line.issue_date is not None
    ):  # absent for a ticket without a date, as before the date existed
        stored["issue_date"] = line.issue_date.isoformat()
    return stored


def _stored_date(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def lines_from_json(raw: list[dict[str, Any]]) -> tuple[TicketLine, ...]:
    """The tickets of a stored version, in entry order."""
    return tuple(
        TicketLine(
            doc_number=str(t["doc_number"]),
            airline=str(t["airline"]),
            total_doc=Decimal(str(t["total_doc"])),
            pax_name=str(t["pax_name"]),
            pax_type=str(t["pax_type"]),
            rloc=str(t["rloc"]),
            trnc=str(t["trnc"]),
            position=int(t["position"]),
            issue_date=_stored_date(t.get("issue_date")),
        )
        for t in sorted(raw, key=lambda item: int(item["position"]))
    )
