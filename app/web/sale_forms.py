"""The sale form as the browser posts it, and its two-way mapping with the service.

Everything posted is plain text and **never trusted**: ``to_input`` builds the ``SaleInput`` the
service validates and prices again, so the read-back page can carry the values in hidden fields
without any of them (a price, a sign, a partner) deciding anything by itself. The form is read
field by field from the request (not through FastAPI's ``Form`` parameters), so a missing, odd or
oversized value becomes a French message next to the field and never a raw 422 page.

* A ticket row is seven parallel lists (``doc_number``, ``airline``, ``total_doc``, ``pax_name``,
  ``pax_type``, ``rloc``, ``trnc``): one entry per row, in the order shown. A row whose five free
  fields are all empty is dropped (the way to remove a row without JavaScript); the errors of the
  others point at the row where they were typed.
* Every value is clipped to ``CLIP`` characters before use, more than any limit of the service, so
  clipping can only turn "too long" into "still too long".
* The row count is capped at ``MAX_TICKETS_PER_SALE`` + 1: one over the limit is enough for the
  service to say so.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from starlette.datastructures import FormData

from app.formatting import money
from app.models import AG_DIRECT, AG_PARTNER, SaleVersion
from app.sales import FieldError, SaleInput, SaleView, TicketInput
from app.sales_rules import MAX_TICKETS_PER_SALE
from app.web.params import MAX_DB_ID

CLIP = 1000
TICKET_FIELDS = ("doc_number", "airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc")
FREE_TICKET_FIELDS = ("doc_number", "airline", "total_doc", "pax_name", "rloc")
DEFAULT_PAX_TYPE = "ADT"
DEFAULT_TRNC = "TKTT"
DIRECT = "direct"

BAD_AG = "Choisissez BRUGE (vente directe) ou un partenaire."
BAD_DAY = "La date doit être au format AAAA-MM-JJ."
BAD_VERSION = "Version de la vente invalide : rechargez la page."

PAYMENT_LABELS: tuple[tuple[str, str], ...] = (
    ("cash", "Espèces"),
    ("bankily", "Bankily"),
    ("masrvi", "Masrvi"),
    ("sedad", "Sedad"),
    ("virement", "Virement"),
    ("cheque", "Chèque"),
    ("credit", "Crédit"),
)
PAYMENT_NAME = dict(PAYMENT_LABELS)

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_DIGITS = re.compile(r"[0-9]{1,19}", re.ASCII)
_TRUE = frozenset({"1", "on", "true", "yes"})


@dataclass
class TicketForm:
    doc_number: str = ""
    airline: str = ""
    total_doc: str = ""
    pax_name: str = ""
    pax_type: str = DEFAULT_PAX_TYPE
    rloc: str = ""
    trnc: str = DEFAULT_TRNC

    @property
    def is_blank(self) -> bool:
        return not any(getattr(self, name).strip() for name in FREE_TICKET_FIELDS)


@dataclass
class SaleForm:
    tickets: list[TicketForm] = field(default_factory=lambda: [TicketForm()])
    ag: str = DIRECT  # "direct" or a partner id
    pb: str = ""
    fee_override: str = ""
    fee_override_reason: str = ""
    clt: str = ""
    ap: str = ""
    client_refused_phone: bool = False
    payment_method: str = ""
    staff_sign: str = ""
    day: str = ""
    reason: str = ""
    version_no: str = ""
    add_row: bool = False  # the "Ajouter un billet" button was pressed

    @property
    def hidden_pairs(self) -> list[tuple[str, str]]:
        """Every value of the form as ``(name, value)`` pairs, to carry in hidden fields."""
        pairs: list[tuple[str, str]] = []
        for row in self.tickets:
            pairs.extend((name, getattr(row, name)) for name in TICKET_FIELDS)
        pairs.extend(
            [
                ("ag", self.ag),
                ("pb", self.pb),
                ("fee_override", self.fee_override),
                ("fee_override_reason", self.fee_override_reason),
                ("clt", self.clt),
                ("ap", self.ap),
                ("payment_method", self.payment_method),
                ("staff_sign", self.staff_sign),
                ("day", self.day),
                ("reason", self.reason),
                ("version_no", self.version_no),
            ]
        )
        if self.client_refused_phone:
            pairs.append(("client_refused_phone", "1"))
        return pairs


@dataclass
class Built:
    """A form turned into service input. ``rows`` maps the service's ticket index to the form row
    it came from; ``problems`` are the mistakes found while reading (before the service runs)."""

    data: SaleInput
    day: date | None
    rows: list[int]
    problems: list[FieldError]


def _text(value: object) -> str:
    return value[:CLIP] if isinstance(value, str) else ""


def read(source: FormData) -> SaleForm:
    """The form as posted: every value text, clipped; missing fields are empty."""
    columns = {name: [_text(v) for v in source.getlist(name)] for name in TICKET_FIELDS}
    count = min(max((len(v) for v in columns.values()), default=0), MAX_TICKETS_PER_SALE + 1)
    rows: list[TicketForm] = []
    for i in range(count):
        row = TicketForm()
        for name in TICKET_FIELDS:
            values = columns[name]
            setattr(row, name, values[i] if i < len(values) else "")
        rows.append(row)

    def one(name: str) -> str:
        return _text(source.get(name))

    return SaleForm(
        tickets=rows or [TicketForm()],
        ag=one("ag") or DIRECT,
        pb=one("pb"),
        fee_override=one("fee_override"),
        fee_override_reason=one("fee_override_reason"),
        clt=one("clt"),
        ap=one("ap"),
        client_refused_phone=one("client_refused_phone").strip().lower() in _TRUE,
        payment_method=one("payment_method"),
        staff_sign=one("staff_sign"),
        day=one("day"),
        reason=one("reason"),
        version_no=one("version_no"),
        add_row=bool(one("add_row")),
    )


def with_extra_row(form: SaleForm) -> SaleForm:
    """The same form with one more empty row (never beyond the cap)."""
    if len(form.tickets) < MAX_TICKETS_PER_SALE:
        form.tickets = [*form.tickets, TicketForm()]
    return form


def parse_ag(raw: str) -> tuple[str, int | None]:
    """``(ag_kind, partner_id)`` of the ``ag`` field. A value that is neither "direct" nor a
    plausible partner id gives an ag kind the service refuses (the message is its own)."""
    text = raw.strip()
    if text == DIRECT:
        return AG_DIRECT, None
    if _DIGITS.fullmatch(text) and 1 <= int(text) <= MAX_DB_ID:
        return AG_PARTNER, int(text)
    return "?", None


def parse_day(raw: str) -> tuple[date | None, FieldError | None]:
    """The day a manager or accountant chose; ``(None, None)`` for an empty field (today)."""
    text = raw.strip()
    if not text:
        return None, None
    if _ISO_DATE.fullmatch(text):
        try:
            return date.fromisoformat(text), None
        except ValueError:
            pass
    return None, FieldError("day", BAD_DAY)


def parse_version(raw: str) -> int | None:
    """The version number the form was built on, or None when it is missing or garbled."""
    text = raw.strip()
    if _DIGITS.fullmatch(text) and 1 <= int(text) <= MAX_DB_ID:
        return int(text)
    return None


def to_input(form: SaleForm, *, choose_day: bool) -> Built:
    """The ``SaleInput`` the service will validate. ``choose_day`` is true for manager and
    accountant only; for staff the day field is ignored (they always enter today)."""
    kept = [i for i, row in enumerate(form.tickets) if not row.is_blank]
    tickets = tuple(
        TicketInput(
            doc_number=form.tickets[i].doc_number,
            airline=form.tickets[i].airline,
            total_doc=form.tickets[i].total_doc,
            pax_name=form.tickets[i].pax_name,
            pax_type=form.tickets[i].pax_type,
            rloc=form.tickets[i].rloc,
            trnc=form.tickets[i].trnc,
        )
        for i in kept
    )
    kind, partner_id = parse_ag(form.ag)
    day: date | None = None
    problems: list[FieldError] = []
    if choose_day:
        day, problem = parse_day(form.day)
        if problem is not None:
            problems.append(problem)
    data = SaleInput(
        tickets=tickets,
        ag_kind=kind,
        partner_id=partner_id,
        pb=form.pb.strip() or None,
        clt=form.clt,
        ap=form.ap or None,
        client_refused_phone=form.client_refused_phone,
        payment_method=form.payment_method,
        staff_sign=form.staff_sign.strip() or None,
        fee_override=form.fee_override.strip() or None,
        fee_override_reason=form.fee_override_reason or None,
    )
    return Built(data, day, kept, problems)


_ROW_ERROR = re.compile(r"tickets\[(\d+)\]\.(\w+)")
_FIELD_NAMES = {"ag_kind": "ag", "partner_id": "ag"}


def error_map(errors: Sequence[FieldError], rows: Sequence[int]) -> dict[str, str]:
    """Service errors keyed by the form field they belong to: ``ag``, ``clt``, ``tickets`` (the
    section) and ``t<row>.<field>`` for a ticket (``row`` is the position in the form, not in the
    service's list: blank rows were dropped). Several messages for one field are joined."""
    found: dict[str, str] = {}
    for error in errors:
        match = _ROW_ERROR.fullmatch(error.field)
        if match:
            index = int(match.group(1))
            row = rows[index] if index < len(rows) else index
            key = f"t{row}.{match.group(2)}"
        elif error.field.startswith("tickets["):
            key = "tickets"
        else:
            key = _FIELD_NAMES.get(error.field, error.field)
        found[key] = f"{found[key]} {error.message}" if key in found else error.message
    return found


# --- from a stored sale back to a form ------------------------------------------------------


def from_view(view: SaleView) -> SaleForm:
    """The edit form of a stored sale: its current values, and the version it is built on."""
    override = view.fee_override_reason is not None and view.fee_applied is not None
    return SaleForm(
        tickets=[
            TicketForm(
                doc_number=t.doc_number,
                airline=t.airline,
                total_doc=money(t.total_doc),
                pax_name=t.pax_name,
                pax_type=t.pax_type,
                rloc=t.rloc,
                trnc=t.trnc,
            )
            for t in view.tickets
        ],
        ag=str(view.partner_id) if view.partner_id is not None else DIRECT,
        pb=money(view.pb) if view.ag_kind == AG_DIRECT else "",
        fee_override=money(view.fee_applied) if override and view.fee_applied is not None else "",
        fee_override_reason=view.fee_override_reason or "",
        clt=view.clt or "",
        ap=view.ap or "",
        client_refused_phone=view.client_refused_phone,
        payment_method=view.payment_method,
        staff_sign=view.staff_sign,
        version_no=str(view.version_no),
    )


# --- the history of a sale -------------------------------------------------------------------


def _label(version: SaleVersion) -> str:
    return "Direct" if version.ag_kind == AG_DIRECT else version.ag_label


def changes(previous: SaleVersion | None, current: SaleVersion) -> list[str]:
    """What changed from ``previous`` to ``current``, as short French phrases. Amounts and
    ticket numbers are named; a client's name or phone is only said to have changed."""
    if previous is None:
        return ["Création de la vente"]
    if current.voided:
        return ["Vente annulée"]
    found: list[str] = []
    if current.report_day_id != previous.report_day_id:
        found.append("Journée modifiée")
    if current.staff_sign != previous.staff_sign:
        found.append(f"Signe : {previous.staff_sign} → {current.staff_sign}")
    if (current.ag_kind, current.partner_id) != (previous.ag_kind, previous.partner_id):
        found.append(f"AG : {_label(previous)} → {_label(current)}")
    if current.pb != previous.pb:
        found.append(f"P.B : {money(previous.pb)} → {money(current.pb)}")
    if current.fee_applied != previous.fee_applied:
        found.append("Frais du partenaire modifiés")
    old_tickets = {t["doc_number"]: t for t in previous.tickets}
    new_tickets = {t["doc_number"]: t for t in current.tickets}
    added = [d for d in new_tickets if d not in old_tickets]
    removed = [d for d in old_tickets if d not in new_tickets]
    edited = [
        d for d in new_tickets if d in old_tickets and _differs(old_tickets[d], new_tickets[d])
    ]
    if added:
        found.append("Billet ajouté : " + ", ".join(added))
    if removed:
        found.append("Billet retiré : " + ", ".join(removed))
    if edited:
        found.append("Billet modifié : " + ", ".join(edited))
    if current.cost_total != previous.cost_total:
        found.append(f"Coût : {money(previous.cost_total)} → {money(current.cost_total)}")
    if current.clt != previous.clt:
        found.append("Nom du client modifié")
    if (current.ap, current.client_refused_phone) != (previous.ap, previous.client_refused_phone):
        found.append("Numéro du client modifié")
    if current.payment_method != previous.payment_method:
        found.append(
            f"Paiement : {PAYMENT_NAME.get(previous.payment_method, previous.payment_method)}"
            f" → {PAYMENT_NAME.get(current.payment_method, current.payment_method)}"
        )
    return found or ["Aucun champ modifié"]


def _differs(old: Mapping[str, object], new: Mapping[str, object]) -> bool:
    keys = ("airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc")
    return any(str(old.get(k)) != str(new.get(k)) for k in keys)


# --- the error summary -----------------------------------------------------------------------

FIELD_LABELS = {
    "tickets": "Billets",
    "staff_sign": "Signe",
    "day": "Journée",
    "ag": "AG",
    "pb": "P.B",
    "fee_override": "Frais particuliers",
    "fee_override_reason": "Motif des frais particuliers",
    "clt": "CLT",
    "ap": "AP",
    "client_refused_phone": "Téléphone du client",
    "payment_method": "Mode de paiement",
    "reason": "Motif",
}
TICKET_LABELS = {
    "doc_number": "Numéro du billet",
    "airline": "Compagnie (A/L)",
    "total_doc": "Coût (TOTAL DOC)",
    "pax_name": "Passager (PAX NAME)",
    "pax_type": "Type de passager",
    "rloc": "Réservation (RLOC)",
    "trnc": "Type de billet (TRNC)",
}
_ROW_KEY = re.compile(r"t(\d+)\.(\w+)")


def field_id(key: str) -> str:
    """The id of the input an error key (``clt``, ``t2.total_doc``) belongs to."""
    match = _ROW_KEY.fullmatch(key)
    if match is None:
        return "tickets-title" if key == "tickets" else key
    row, name = int(match.group(1)), match.group(2)
    return f"ticket-{row + 1}" if name == "doc_number" else f"{name}_{row}"


def error_summary(errors: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(link, text)`` for each error, ticket rows first (in row order), then the other fields in
    the order of the form: the list at the top of the page, each pointing at its input."""
    order = list(FIELD_LABELS)
    rows: list[tuple[int, int, str]] = []
    others: list[tuple[int, str]] = []
    for key in errors:
        match = _ROW_KEY.fullmatch(key)
        if match:
            names = list(TICKET_LABELS)
            name = match.group(2)
            rows.append((int(match.group(1)), names.index(name) if name in names else 99, key))
        else:
            others.append((order.index(key) if key in order else 99, key))
    found: list[tuple[str, str]] = []
    for row, _, key in sorted(rows):
        name = key.split(".", 1)[1]
        label = f"Billet {row + 1}, {TICKET_LABELS.get(name, name)}"
        found.append((f"#{field_id(key)}", f"{label} : {errors[key]}"))
    for _, key in sorted(others):
        found.append((f"#{field_id(key)}", f"{FIELD_LABELS.get(key, key)} : {errors[key]}"))
    return found
