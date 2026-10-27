"""The agency's own details (phase F, slice F3): « Coordonnées de l'agence », printed in the header
of the partner statements (and, later, of the invoices: doc 06 §3).

One row of ``agency_details`` per agency (migration 0018), every field optional free text: the
French and Arabic names, the address, the phones, the e-mail, the NIF, the bank and its account
(RIB), and the Bankily, Masrvi and Sedad numbers. Text is cleaned (``clean_text``: no control or
bidi characters) and its whitespace collapsed; each field has a length limit
(``models.AGENCY_DETAIL_LIMITS``, also a CHECK). The phones and the three wallet numbers are
checked loosely (digits and the usual separators), after ``normalise_number`` (NFKC,
Arabic-Indic digits as 0-9, every dash as ``-``); the e-mail only for its shape. A value that is
not text is refused.

**Who.** A manager saves (``permissions.can_edit_agency_details``), and must be active; the
accountant reads (``can_view_agency_details``). A refused save is a ``permission_denied`` audit
event, committed before the error is raised.

**Double clicks.** The form carries a ``request_key``; the row keeps the key of the last save. The
same form posted again returns the stored row without writing; the same key with other values is
refused (``StaleRequestKey``: the form comes back with a fresh key). When two first saves race
and this one loses the insert, the row is read again: the same key and values are a success,
anything else is ``SAVE_LOST`` (reload).

**Audit.** ``agency_details_saved`` with the names of the fields that changed, never a value. A
save that changes nothing writes nothing.

Nothing here commits, except the audit row of a refusal; the caller commits or rolls back.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import audit, permissions
from app.clock import utc_now
from app.errors import UserError
from app.models import AGENCY_DETAIL_LIMITS, MAX_REQUEST_KEY, AgencyDetails, User
from app.parsing.amadeus import clean_text

SAVED_EVENT: Final = audit.AGENCY_DETAILS_SAVED

LABELS: Final[dict[str, str]] = {
    "name_fr": "Nom (français)",
    "name_ar": "Nom (arabe)",
    "address": "Adresse",
    "phones": "Téléphones",
    "email": "E-mail",
    "nif": "NIF",
    "bank_name": "Banque",
    "bank_account": "Compte bancaire (RIB)",
    "bankily": "Bankily",
    "masrvi": "Masrvi",
    "sedad": "Sedad",
}
FIELDS: Final = tuple(AGENCY_DETAIL_LIMITS)
WALLETS: Final = ("bankily", "masrvi", "sedad")

NOT_MANAGER = "Seul le gérant modifie les coordonnées de l'agence."
INACTIVE_ACTOR = "Votre compte est désactivé."
TOO_LONG = "« {label} » ne peut pas dépasser {limit} caractères."
BAD_PHONES = (
    "Écrivez les numéros de téléphone avec des chiffres, séparés par « / » "
    "(par exemple +222 45 00 00 00 / 22 00 00 00)."
)
BAD_EMAIL = "Saisissez une adresse e-mail valide (par exemple contact@agence.mr)."
BAD_WALLET = "Le numéro {label} ne peut contenir que des chiffres, des espaces, « + » et « - »."
BAD_REQUEST_KEY = "Ce formulaire n'est plus valable : rechargez la page et recommencez."
KEY_REUSED = (
    "Ce formulaire a déjà été envoyé avec d'autres valeurs. Vérifiez la saisie et envoyez-le "
    "de nouveau."
)
NOTHING_CHANGED = "Aucune modification : les coordonnées sont déjà celles-ci."
SAVE_LOST = "Les coordonnées ont changé pendant l'enregistrement : rechargez la page."
BAD_TEXT = "Texte invalide."

_REQUEST_KEY = re.compile(rf"[A-Za-z0-9_-]{{16,{MAX_REQUEST_KEY}}}", re.ASCII)
# Phones: digits and the usual separators; several numbers are separated by / , or ;.
_PHONE_CHARS = re.compile(r"[0-9+(). \-]+", re.ASCII)
_PHONE_SPLIT = re.compile(r"[/,;]", re.ASCII)
_WALLET = re.compile(r"\+?[0-9][0-9 \-]*", re.ASCII)
_EMAIL = re.compile(r"[^@\s]+@[^@\s.]+(\.[^@\s.]+)+")
# Arabic-Indic (U+0660..) and Extended Arabic-Indic (U+06F0..) digits as 0-9; the hyphen, the
# non-breaking hyphen, the figure, en and em dashes, the horizontal bar and the minus sign as -.
_NUMBER_FOLD: Final = str.maketrans(
    {
        **{chr(0x0660 + n): str(n) for n in range(10)},
        **{chr(0x06F0 + n): str(n) for n in range(10)},
        **{chr(c): "-" for c in range(0x2010, 0x2016)},
        "−": "-",
    }
)
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 8, 15
MIN_WALLET_DIGITS, MAX_WALLET_DIGITS = 4, 15


class DetailsError(UserError):
    """A value the manager can correct, or a refusal; ``str(error)`` is French."""


class DetailsForbidden(DetailsError):
    """The actor may not change the details (a 403)."""


class StaleRequestKey(DetailsError):
    """The form's key was used already for other values: show the form with a fresh key."""


class NothingChanged(DetailsError):
    """The values posted are the ones stored: nothing to save."""


@dataclass(frozen=True)
class Saved:
    row: AgencyDetails
    fields: tuple[str, ...]  # what changed, sorted
    created: bool  # False: this form had been saved already (a double click)


def check_request_key(raw: object) -> str:
    if not isinstance(raw, str) or _REQUEST_KEY.fullmatch(raw) is None:
        raise DetailsError(BAD_REQUEST_KEY)
    return raw


def get_details(db: Session, agency_id: int) -> AgencyDetails | None:
    return db.scalars(
        select(AgencyDetails)
        .where(AgencyDetails.agency_id == agency_id)
        .execution_options(populate_existing=True)
    ).one_or_none()


def normalise_number(text: str) -> str:
    """A phone or wallet number as typed, in the form it is checked and stored: NFKC (full-width
    digits, no-break spaces), Arabic-Indic and Extended Arabic-Indic digits as 0-9, and every
    dash as ``-``; whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", text).translate(_NUMBER_FOLD)
    return " ".join(folded.split())


def _digits(text: str) -> int:
    return sum(ch.isdigit() for ch in text)


def _check_phones(text: str) -> None:
    parts = [p.strip() for p in _PHONE_SPLIT.split(text)]
    if not parts or any(not p for p in parts):
        raise DetailsError(BAD_PHONES)
    for part in parts:
        if _PHONE_CHARS.fullmatch(part) is None:
            raise DetailsError(BAD_PHONES)
        if not MIN_PHONE_DIGITS <= _digits(part) <= MAX_PHONE_DIGITS:
            raise DetailsError(BAD_PHONES)


def _check_wallet(name: str, text: str) -> None:
    if _WALLET.fullmatch(text) is None or not (
        MIN_WALLET_DIGITS <= _digits(text) <= MAX_WALLET_DIGITS
    ):
        raise DetailsError(BAD_WALLET.format(label=LABELS[name]))


def clean_values(raw: Mapping[str, object]) -> dict[str, str | None]:
    """Every field of ``raw`` cleaned and checked: None for an empty or missing one.
    ``DetailsError`` (French) for a value that does not fit."""
    out: dict[str, str | None] = {}
    for name in FIELDS:
        value = raw.get(name)
        if value is not None and not isinstance(value, str):
            raise DetailsError(BAD_TEXT)
        text = " ".join(clean_text(value).split()) if value is not None else ""
        if text and (name == "phones" or name in WALLETS):
            text = normalise_number(text)
        if not text:
            out[name] = None
            continue
        if len(text) > AGENCY_DETAIL_LIMITS[name]:
            raise DetailsError(
                TOO_LONG.format(label=LABELS[name], limit=AGENCY_DETAIL_LIMITS[name])
            )
        if name == "phones":
            _check_phones(text)
        elif name == "email" and _EMAIL.fullmatch(text) is None:
            raise DetailsError(BAD_EMAIL)
        elif name in WALLETS:
            _check_wallet(name, text)
        out[name] = text
    return out


def _require_editor(db: Session, actor: User, now: datetime, ip: str | None) -> None:
    if not permissions.can_edit_agency_details(actor):
        audit.refused(
            db, "save_agency_details", agency_id=actor.agency_id, actor_user_id=actor.id,
            ip=ip, now=now,
        )  # fmt: skip
        raise DetailsForbidden(NOT_MANAGER)
    if not actor.is_active:
        audit.refused(
            db, "save_agency_details", agency_id=actor.agency_id, actor_user_id=actor.id,
            ip=ip, now=now,
        )  # fmt: skip
        raise DetailsForbidden(INACTIVE_ACTOR)


def _locked(db: Session, agency_id: int) -> AgencyDetails | None:
    return db.scalars(
        select(AgencyDetails)
        .where(AgencyDetails.agency_id == agency_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def save_details(
    db: Session,
    actor: User,
    raw: Mapping[str, object],
    *,
    request_key: str,
    now: datetime | None = None,
    ip: str | None = None,
) -> Saved:
    """Save the agency's details as typed (every field; an empty one is cleared)."""
    moment = now or utc_now()
    _require_editor(db, actor, moment, ip)
    key = check_request_key(request_key)
    values = clean_values(raw)
    agency_id = actor.agency_id
    row = _locked(db, agency_id)
    if row is not None and row.request_key == key:
        if all(getattr(row, n) == values[n] for n in FIELDS):
            return Saved(row, (), False)
        raise StaleRequestKey(KEY_REUSED)
    changed = tuple(
        sorted(n for n in FIELDS if (getattr(row, n) if row is not None else None) != values[n])
    )
    if not changed:
        raise NothingChanged(NOTHING_CHANGED)
    try:
        with db.begin_nested():
            if row is None:
                row = AgencyDetails(agency_id=agency_id)
                db.add(row)
            for name in FIELDS:
                setattr(row, name, values[name])
            row.request_key = key
            row.updated_by_user_id = actor.id
            row.updated_at = moment
            db.flush()
    except IntegrityError:
        # Two first saves at once, and the other one won. The same form posted twice (a double
        # click that raced) is a success; anything else must be reloaded.
        stored = get_details(db, agency_id)
        if (
            stored is not None
            and stored.request_key == key
            and all(getattr(stored, n) == values[n] for n in FIELDS)
        ):
            return Saved(stored, (), False)
        raise DetailsError(SAVE_LOST) from None
    audit.record(
        db,
        SAVED_EVENT,
        agency_id=agency_id,
        actor_user_id=actor.id,
        ip=ip,
        details={"fields": list(changed)},
        now=moment,
    )
    return Saved(row, changed, True)


# --- the header of a document -------------------------------------------------------------------


def _joined(*parts: tuple[str, str | None]) -> str | None:
    shown = [f"{label} : {value}" for label, value in parts if value]
    return " · ".join(shown) if shown else None


def header_lines(details: AgencyDetails | None, agency_name: str) -> list[str]:
    """The lines at the top of a document, in order; a missing detail is left out, and without
    a French name the agency's own name (``agencies.name``) heads it."""
    if details is None:
        return [agency_name]
    lines = [
        details.name_fr or agency_name,
        details.name_ar,
        details.address,
        _joined(("Tél.", details.phones), ("E-mail", details.email)),
        _joined(("NIF", details.nif)),
        _joined(("Banque", details.bank_name), ("RIB", details.bank_account)),
        _joined(("Bankily", details.bankily), ("Masrvi", details.masrvi), ("Sedad", details.sedad)),
    ]
    return [line for line in lines if line]


__all__ = [
    "FIELDS",
    "LABELS",
    "SAVED_EVENT",
    "DetailsError",
    "DetailsForbidden",
    "NothingChanged",
    "Saved",
    "StaleRequestKey",
    "check_request_key",
    "clean_values",
    "get_details",
    "header_lines",
    "normalise_number",
    "save_details",
]
