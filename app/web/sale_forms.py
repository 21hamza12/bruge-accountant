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
from decimal import Decimal

from starlette.datastructures import FormData

from app.formatting import money
from app.models import AG_DIRECT, AG_PARTNER, SaleVersion
from app.money import MoneyError, parse_amount
from app.sales import FieldError, SaleInput, SaleView, TicketInput
from app.sales_rules import MAX_TICKETS_PER_SALE
from app.web.params import parse_db_id

CLIP = 1000
TICKET_FIELDS = ("doc_number", "airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc")
FREE_TICKET_FIELDS = ("doc_number", "airline", "total_doc", "pax_name", "rloc")
DEFAULT_PAX_TYPE = "ADT"
DEFAULT_TRNC = "TKTT"
DIRECT = "direct"

BAD_AG = "Choisissez BRUGE (vente directe) ou un partenaire."
BAD_SOURCE = "Origine de la vente invalide."
ASK_AGENCY = (
    "Confirmez l'agence : répondez Oui, choisissez un autre partenaire ou la vente directe."
)
ASK_OTHER = "Choisissez le partenaire dans la liste."

SOURCE_FORM = "form"
SOURCE_PASTE = "paste"  # the form was prefilled from pasted Amadeus text (only labels history)
SOURCE_SCREENSHOT = "screenshot"  # prefilled from the reading of a screenshot (same questions)
SOURCES = (SOURCE_FORM, SOURCE_PASTE, SOURCE_SCREENSHOT)
ANSWER_YES, ANSWER_OTHER, ANSWER_DIRECT = "yes", "other", "direct"
MAX_NOTICES = 12
HIDDEN_CLIP = 120  # the agency text and the reader warnings carried in hidden fields
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
    # Filled from pasted text (``source == "paste"``): the agency the agent must confirm. The AG of
    # such a form is the answer: ``ag_answer`` yes (``ag_proposed``), other (``ag_other``) or
    # direct. Display and labelling only: the service validates the resulting AG as any other.
    source: str = SOURCE_FORM
    ag_answer: str = ""
    ag_proposed: str = ""  # "direct", a partner id, or "" (nothing to propose)
    ag_other: str = ""
    ag_text: str = ""  # the agency text of the booking (AP agency, else the FP hint)
    ag_match: str = ""  # exact, packed, prefix, fp, close or none
    pb_read: str = ""  # the P.B the reader took from the RM line (to tell it from a typed one)
    notices: list[str] = field(default_factory=list)  # what the reader warned about

    @property
    def from_paste(self) -> bool:
        """Prefilled by the reader (pasted text or a screenshot): the agency question applies."""
        return self.source in (SOURCE_PASTE, SOURCE_SCREENSHOT)

    @property
    def cost_read(self) -> Decimal | None:
        """The Amadeus cost of the tickets as read (sum of TOTAL DOC), for the line at the top of
        a prefilled sale; ``None`` when a ticket has no readable cost."""
        total = Decimal(0)
        for row in self.tickets:
            amount = _amount_or_none(row.total_doc)
            if amount is None:
                return None
            total += amount
        return total

    @property
    def pb_read_amount(self) -> Decimal | None:
        """The P.B the reader took from the RM line, as an amount (``None``: no RM price)."""
        return _amount_or_none(self.pb_read)

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
        if self.from_paste:
            pairs.extend(
                [
                    ("source", self.source),
                    ("ag_answer", self.ag_answer),
                    ("ag_proposed", self.ag_proposed),
                    ("ag_other", self.ag_other),
                    ("ag_text", self.ag_text),
                    ("ag_match", self.ag_match),
                    ("pb_read", self.pb_read),
                ]
            )
            pairs.extend(("pnr_warning", text) for text in self.notices)
        return pairs


@dataclass
class Built:
    """A form turned into service input. ``rows`` maps the service's ticket index to the form row
    it came from; ``problems`` are the mistakes found while reading (before the service runs)."""

    data: SaleInput
    day: date | None
    rows: list[int]
    problems: list[FieldError]


def _amount_or_none(text: str) -> Decimal | None:
    try:
        return parse_amount(text)
    except MoneyError:
        return None


def _text(value: object) -> str:
    return value[:CLIP] if isinstance(value, str) else ""


def read(source: FormData, *, new_sale: bool = True) -> SaleForm:
    """The form as posted: every value text, clipped; missing fields are empty.

    ``source`` and the agency-question fields are **advisory**: they label a new sale's history
    and decide what the agent is asked, never what is accepted (the service validates the AG as
    any other). Only a new sale can come from a paste: with ``new_sale`` false (an edit) they
    are ignored."""
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

    origin = (one("source").strip() or SOURCE_FORM) if new_sale else SOURCE_FORM
    form = SaleForm(
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
        source=origin,
        ag_answer=one("ag_answer").strip(),
        ag_proposed=one("ag_proposed").strip(),
        ag_other=one("ag_other").strip(),
        ag_text=one("ag_text")[:HIDDEN_CLIP] if new_sale else "",
        ag_match=one("ag_match").strip(),
        pb_read=one("pb_read").strip()[:HIDDEN_CLIP] if new_sale else "",
        notices=[_text(v)[:HIDDEN_CLIP] for v in source.getlist("pnr_warning")][:MAX_NOTICES]
        if new_sale
        else [],
    )
    if form.from_paste:
        form.ag = answered_ag(form)
    return form


def answered_ag(form: SaleForm) -> str:
    """The ``ag`` value the agent's answer to the agency question stands for; "" when the
    question is not answered (or answered "other" without a partner)."""
    if form.ag_answer == ANSWER_YES:
        return form.ag_proposed
    if form.ag_answer == ANSWER_OTHER:
        return form.ag_other
    if form.ag_answer == ANSWER_DIRECT:
        return DIRECT
    return ""


def _agency_problem(form: SaleForm) -> FieldError | None:
    """A form from pasted text is not accepted until its agency question is answered."""
    if answered_ag(form):
        return None
    if form.ag_answer == ANSWER_OTHER:
        return FieldError("ag", ASK_OTHER)
    return FieldError("ag", ASK_AGENCY)


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
    partner_id = parse_db_id(text)
    if partner_id is not None:
        return AG_PARTNER, partner_id
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
    return parse_db_id(raw)


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
    if form.source not in SOURCES:
        problems.append(FieldError("source", BAD_SOURCE))
    elif form.from_paste and (problem := _agency_problem(form)) is not None:
        problems.append(problem)
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
    "source": "Origine",
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
