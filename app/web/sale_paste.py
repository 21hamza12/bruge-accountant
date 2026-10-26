"""From pasted Amadeus text to prefilled sale forms (B6 wiring). Nothing here writes.

``read_paste`` parses the text with ``parse_pnr_text`` and ``proposed_sales`` against the agency's
active partners plus their **approved** aliases, and turns each proposed sale into a ``SaleForm``
(``source="paste"``). The agency of such a form is a question the agent must answer
(``sale_forms.ASK_AGENCY``): the reader only suggests, and an unanswered question blocks the
read-back and the confirm on the server. The pasted text is never stored, logged or audited; only
what the agent confirms in the form is.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app import partner_aliases, partners
from app.formatting import money
from app.parsing.pnr import MAX_TEXT_BYTES, parse_pnr_text
from app.parsing.pnr_sales import ProposedSale, proposed_sales
from app.web.sale_forms import DIRECT, SOURCE_PASTE, SOURCE_SCREENSHOT, SaleForm, TicketForm

TEXT_FIELD = "pnr_text"
EMPTY = "Collez le texte Amadeus de la réservation."
TOO_LONG = f"Texte trop long : {MAX_TEXT_BYTES // 1024} Ko au plus."
NOTHING = "Aucune vente lisible dans ce texte : vérifiez que vous avez copié tout l'écran."
W_NO_ISSUE_DATE = (
    "Date d'émission illisible : la vente sera classée à la journée d'aujourd'hui, "
    "sauf si le gérant ou la comptable choisit une autre journée."
)
W_SOME_NO_ISSUE_DATE = "Billet {doc} : date d'émission illisible (ignorée pour la journée)."


@dataclass(frozen=True)
class Proposal:
    sale: ProposedSale
    form: SaleForm
    rp_sign: str | None


@dataclass(frozen=True)
class PasteResult:
    proposals: list[Proposal]
    reader_warnings: tuple[str, ...]
    error: str = ""


def check_text(raw: object) -> str:
    """The French message for text that must not be parsed (missing, empty, over 16 KB), or ''."""
    if not isinstance(raw, str) or not raw.strip():
        return EMPTY
    if len(raw.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return TOO_LONG
    return ""


def _ag_fields(sale: ProposedSale) -> tuple[str, str]:
    """``(ag_proposed, ag_match)`` of a proposed sale: what the agent is asked about. The answer
    itself is never pre-filled, not even for an exact match: the agent confirms every sale."""
    if sale.ag_kind == "direct":
        proposed = DIRECT
    elif sale.ag_kind == "partner" and sale.partner_id is not None:
        proposed = str(sale.partner_id)
    elif sale.suggested_partner_id is not None:
        return str(sale.suggested_partner_id), "close"
    else:
        return "", sale.agency_match
    return proposed, sale.agency_match


def form_from_sale(
    sale: ProposedSale, reader_warnings: tuple[str, ...] = (), source: str = SOURCE_PASTE
) -> SaleForm:
    proposed, match = _ag_fields(sale)
    day = sale.issue_day
    return SaleForm(
        tickets=[
            TicketForm(
                doc_number=t.doc_number,
                airline=t.airline,
                total_doc=money(t.total_doc) if t.total_doc is not None else "",
                pax_name=t.pax_name,
                pax_type=t.pax_type,
                rloc=t.rloc,
                trnc=t.trnc,
                issue_date=t.issue_date.isoformat() if t.issue_date else "",
            )
            for t in sale.tickets
        ]
        or [TicketForm()],
        pb=money(sale.pb) if sale.pb is not None else "",
        clt=sale.clt or "",
        ap=sale.ap or "",
        staff_sign=sale.staff_sign or "",
        source=source,
        ag_proposed=proposed,
        ag_text=sale.agency_text or "",
        pb_read=money(sale.pb) if sale.pb is not None else "",
        ag_match=match,
        day=day.isoformat() if day else "",
        notices=[*reader_warnings, *sale.warnings, *_date_notices(sale)],
    )


def _date_notices(sale: ProposedSale) -> list[str]:
    """What the agent is told when a ticket's issue date could not be read."""
    if sale.issue_day is None:
        return [W_NO_ISSUE_DATE] if sale.tickets else []
    return [
        W_SOME_NO_ISSUE_DATE.format(doc=t.doc_number) for t in sale.tickets if t.issue_date is None
    ]


def read_paste(
    db: Session, agency_id: int, text: str, source: str = SOURCE_PASTE, nothing: str = NOTHING
) -> PasteResult:
    """Parse ``text`` (already checked with ``check_text``) for this agency. ``source`` labels the
    forms (``paste``, or ``screenshot`` for the transcript of an image)."""
    reading = parse_pnr_text(text, transcript=source == SOURCE_SCREENSHOT)
    names = [(p.id, p.name) for p in partners.list_active(db, agency_id)]
    aliases = partner_aliases.approved_pairs(db, agency_id)
    sales = proposed_sales(reading, names, aliases)
    if not sales:
        return PasteResult([], reading.warnings, nothing)
    return PasteResult(
        [
            Proposal(s, form_from_sale(s, reading.warnings, source), reading.agent_sign)
            for s in sales
        ],
        reading.warnings,
    )


def matching_sign(rp_sign: str | None, signs: list[str]) -> str | None:
    """The one known sign the RP sign (``SV``) stands for (``0004SV``), else None."""
    if not rp_sign:
        return None
    wanted = rp_sign.strip().upper()
    found = [
        s for s in signs if s.upper() == wanted or (len(wanted) >= 2 and s.upper().endswith(wanted))
    ]
    return found[0] if len(found) == 1 else None
