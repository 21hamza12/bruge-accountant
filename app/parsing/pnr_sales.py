"""From a PNR reading to proposed sales: **suggestions** the agent confirms in the read-back.

One proposed sale per passenger, holding all of that passenger's tickets (a layover passenger has
two; an infant has its own sale with its INF ticket). Tickets that cannot be tied to a passenger
go together into one extra sale with an empty ``pax_name``, no P.B and a warning. A passenger
with no ticket makes no sale, and every sale then carries a warning naming that passenger's
number. With several infants on one adult, the k-th INF ticket goes to the k-th infant; if the
counts differ, a warning says so.

Suggested AG (``ag_kind``):

1. The AP agency decides first. ``BRUGE`` (any case, spaces ignored) is a direct sale.
2. Otherwise ``find_partners`` looks for a partner on a normalised key (accents removed, case
   folded, punctuation and repeated spaces collapsed), stopping at the first rule that finds
   anything: ``exact`` (same key), ``packed`` (same key without spaces), ``prefix`` (the shorter
   key without spaces, at least ``MIN_PREFIX`` characters, equals the first whole words of the
   longer one: ``WORLDGATE`` matches ``WORLD GATE TOURISM``, ``REND`` matches ``REND TOURISME ET
   VOYAGES``, ``ABSHERVOY`` matches nothing). A ``prefix`` match is flagged for review.
3. Several partners at the same rule: ambiguous, nothing is suggested ("agence ambiguë"), and the
   FP hint is **not** tried as a way out.
4. An AP agency that matches nothing falls back to the FP hint (direct, or a partner), flagged
   for review together with "agence inconnue : <AP agency>". With no AP agency the FP hint is
   tried the same way. No match at all: ``ag_kind`` and ``partner_id`` are ``None``.

Warnings echo only the AP agency text (cut to 40 characters) and registered partner names, never
the FP hint, which can be a client's name.

P.B is the RM price of the passenger's type unless a partner is recognised (also when the agency
is unknown or ambiguous: the agent may answer "direct"); a partner's price is computed later by
the sales rules. The client is the FP hint unless it is ``CLIENT``, BRUGE or
names an agency (exact or packed match with a partner, a loose prefix match with the
partner already chosen, or the AP agency). The phone is the first
AP phone. FE non-refundable amounts are only noted in a warning, never used as a cost or a
price. ``ProposedTicket.total_doc`` is ``None`` when the cost could not be read: B5 maps it to
an empty form field.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal, NamedTuple

from app.parsing.pnr import PaxType, PnrReading, TicketLine, strip_accents

MIN_PREFIX = 4
MAX_AGENCY_ECHO = 40
TRNC_TICKET = "TKTT"
TRNC_EMD = "EMDS"

AgKind = Literal["direct", "partner"]  # the values of app.models.AG_DIRECT / AG_PARTNER
MatchRule = Literal["exact", "packed", "prefix"]
# How the suggested AG was found: a ``MatchRule`` on the AP agency, ``fp`` (from the payment hint),
# ``none`` (nothing recognised: ``ProposedSale.suggested_partner_id`` may hold a close name).
AgencyMatch = Literal["exact", "packed", "prefix", "fp", "none"]
STOP_WORDS = frozenset({"et", "de", "du", "des", "la", "le", "les", "of", "and", "the", "da", "do"})
MIN_FUZZY_LENGTH = 5  # shorter keys are never guessed
SHORT_KEY_MAX = 11  # up to here one mistake is allowed, from 12 characters two
MIN_INITIALS_WORDS = 3  # initials stand for a name of at least this many words
_DIRECT: AgKind = "direct"
_PARTNER: AgKind = "partner"
_DIRECT_KEY = "bruge"

W_EXCHANGE = "échange : vérifier le prix"
W_EMD = "EMD : type EMDS proposé, à vérifier (EMDA ou EMDS)."
W_ORPHAN = "Billets dont le passager n'est pas identifié : à vérifier."
W_DIRECT_FROM_FP = "vente directe déduite de l'indication de paiement : vérifier"
W_NO_AGENCY = "agence absente : choisissez la vente directe ou le partenaire."
W_FP_AMBIGUOUS = "agence ambiguë : l'indication de paiement correspond à plusieurs partenaires."
W_INFANT_PAIRING = "billets bébé non appariés avec les bébés : vérifier"
W_RM_UNREADABLE = "Ligne RM illisible : saisissez le P.B."
W_RM_MISSING = "Aucune ligne RM : saisissez le P.B."

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ProposedTicket:
    """The fields of ``sales_rules.TicketInput``, prefilled (``total_doc`` None: cost unknown)."""

    doc_number: str
    airline: str
    total_doc: Decimal | None
    pax_name: str
    pax_type: PaxType
    rloc: str
    trnc: str


@dataclass(frozen=True)
class ProposedSale:
    """The fields of ``sales_rules.SaleInput`` that a PNR can suggest."""

    pax_name: str
    tickets: tuple[ProposedTicket, ...]
    ag_kind: AgKind | None  # None when unknown
    partner_id: int | None
    pb: Decimal | None
    clt: str | None
    ap: str | None
    staff_sign: str | None
    warnings: tuple[str, ...]
    agency_text: str | None = None  # the AP agency (else the FP hint) the suggestion came from
    agency_match: AgencyMatch = "none"
    suggested_partner_id: int | None = None  # a close name for an unknown AP agency: never chosen


class PartnerMatch(NamedTuple):
    ids: tuple[int, ...]
    rule: MatchRule | None


def normalise_key(value: str) -> str:
    """Lower case, accents removed, anything but letters and digits is one space."""
    folded = strip_accents(value).casefold()
    return _NON_ALNUM.sub(" ", folded).strip()


def _word_prefix(short: str, long_words: list[str]) -> bool:
    joined = ""
    for word in long_words:
        joined += word
        if joined == short:
            return True
        if len(joined) > len(short):
            return False
    return False


def find_partners(name: str, partners: Sequence[tuple[int, str]]) -> PartnerMatch:
    """The partners ``name`` designates and the rule that found them (module docstring)."""
    key = normalise_key(name) if isinstance(name, str) else ""
    if not key:
        return PartnerMatch((), None)
    packed = key.replace(" ", "")
    keyed = [
        (pid, k)
        for pid, pname in partners
        if isinstance(pname, str) and (k := normalise_key(pname))
    ]
    exact = tuple(dict.fromkeys(pid for pid, k in keyed if k == key))
    if exact:
        return PartnerMatch(exact, "exact")
    same_packed = tuple(dict.fromkeys(pid for pid, k in keyed if k.replace(" ", "") == packed))
    if same_packed:
        return PartnerMatch(same_packed, "packed")
    prefixed = []
    for pid, k in keyed:
        other = k.replace(" ", "")
        if len(packed) <= len(other):
            short, long_words = packed, k.split()
        else:
            short, long_words = other, key.split()
        if len(short) >= MIN_PREFIX and _word_prefix(short, long_words):
            prefixed.append(pid)
    return PartnerMatch(tuple(dict.fromkeys(prefixed)), "prefix" if prefixed else None)


# --- close names (typos, initials): suggestions only -----------------------------------------


def damerau_levenshtein(a: str, b: str) -> int:
    """Edit distance counting an insertion, a deletion, a substitution and the swap of two
    neighbouring letters as one step (the optimal string alignment variant)."""
    rows = [list(range(len(b) + 1))]
    for i in range(1, len(a) + 1):
        row = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            row[j] = min(rows[i - 1][j] + 1, row[j - 1] + 1, rows[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                row[j] = min(row[j], rows[i - 2][j - 2] + 1)
        rows.append(row)
    return rows[-1][-1]


def _allowed_distance(key: str) -> int:
    length = len(key.replace(" ", ""))
    if length < MIN_FUZZY_LENGTH:
        return -1
    return 1 if length <= SHORT_KEY_MAX else 2


def _abbreviates(short: list[str], full: list[str]) -> bool:
    """Whether the words of ``short`` stand for those of ``full``, in order: each is the word
    itself or the initials of two or more consecutive words (``WG TOURISM`` for ``WORLD GATE
    TOURISM``). At least one word must be initials."""

    def walk(i: int, j: int, used_initials: bool) -> bool:
        if i == len(short):
            return j == len(full) and used_initials
        if j == len(full):
            return False
        if short[i] == full[j] and walk(i + 1, j + 1, used_initials):
            return True
        for end in range(j + 2, len(full) + 1):
            if short[i] == "".join(w[0] for w in full[j:end]) and walk(i + 1, end, True):
                return True
        return False

    return walk(0, 0, False)


def suggest_close(name: str, candidates: Sequence[tuple[int, str]]) -> int | None:
    """The one partner whose name (or alias) ``name`` is probably a typo or an abbreviation of,
    else None. Keys of 5 to 11 characters allow one mistake, 12 or more two, shorter ones none;
    initials are tried as well. When two partners are equally close there is no suggestion: the
    agent picks. Several names of one partner are one candidate."""
    key = normalise_key(name) if isinstance(name, str) else ""
    if not key:
        return None
    allowed = _allowed_distance(key)
    words = [w for w in key.split() if w not in STOP_WORDS] or key.split()
    packed = key.replace(" ", "")
    best: dict[int, int] = {}
    for pid, candidate in candidates:
        other = normalise_key(candidate) if isinstance(candidate, str) else ""
        if not other:
            continue
        found: int | None = None
        if allowed >= 0:
            distance = min(
                damerau_levenshtein(key, other),
                damerau_levenshtein(packed, other.replace(" ", "")),
            )
            if distance <= allowed:
                found = distance
        full = [w for w in other.split() if w not in STOP_WORDS] or other.split()
        # initials are guessed only for a name of three words or more and a key of five
        # characters or more (the same floor as a typo): "GT" or "WGT" suggest nothing
        if (
            found is None
            and allowed >= 0
            and len(full) >= MIN_INITIALS_WORDS
            and _abbreviates(words, full)
        ):
            found = 1
        if found is not None and found < best.get(pid, 99):
            best[pid] = found
    if not best:
        return None
    nearest = min(best.values())
    winners = [pid for pid, distance in best.items() if distance == nearest]
    return winners[0] if len(winners) == 1 else None


def _is_direct(name: str) -> bool:
    return normalise_key(name).replace(" ", "") == _DIRECT_KEY


@dataclass(frozen=True)
class _Agency:
    ag_kind: AgKind | None
    partner_id: int | None
    warnings: tuple[str, ...]
    text: str | None = None
    match: AgencyMatch = "none"
    suggested: int | None = None


def _suggest_agency(
    reading: PnrReading, partners: Sequence[tuple[int, str]], known: Sequence[tuple[int, str]]
) -> _Agency:
    """``partners`` are the registered names (for the warnings), ``known`` the names plus the
    approved aliases (for the matching)."""
    names = {pid: n for pid, n in partners if isinstance(n, str)}
    ap, fp = reading.ap_agency, reading.fp_hint

    def deduced(pid: int) -> str:
        return f"partenaire déduit de « {names.get(pid, '?')} » : vérifier"

    unknown: tuple[str, ...] = ()
    close: int | None = None
    if ap:
        if _is_direct(ap):
            return _Agency(_DIRECT, None, (), ap, "exact")
        found = find_partners(ap, known)
        if len(found.ids) == 1 and found.rule is not None:
            note = (deduced(found.ids[0]),) if found.rule == "prefix" else ()
            return _Agency(_PARTNER, found.ids[0], note, ap, found.rule)
        if found.ids:
            return _Agency(None, None, (f"agence ambiguë : {ap[:MAX_AGENCY_ECHO]}",), ap)
        unknown = (f"agence inconnue : {ap[:MAX_AGENCY_ECHO]}",)
        close = suggest_close(ap, known)
    if fp:
        if _is_direct(fp):
            return _Agency(_DIRECT, None, (*unknown, W_DIRECT_FROM_FP), fp, "fp")
        found = find_partners(fp, known)
        if len(found.ids) == 1:
            return _Agency(_PARTNER, found.ids[0], (*unknown, deduced(found.ids[0])), fp, "fp")
        if found.ids and not unknown:
            return _Agency(None, None, (W_FP_AMBIGUOUS,), fp)
    return _Agency(None, None, unknown or (W_NO_AGENCY,), ap or fp, "none", close)


def _suggest_client(
    reading: PnrReading, partners: Sequence[tuple[int, str]], chosen: int | None
) -> str | None:
    hint = reading.fp_hint
    if not hint:
        return None
    if normalise_key(hint) == "client" or _is_direct(hint):
        return None
    found = find_partners(hint, partners)
    if found.rule in ("exact", "packed") or (chosen is not None and chosen in found.ids):
        return None  # a loose prefix hides the hint only when it names the partner already chosen
    if reading.ap_agency and normalise_key(hint) == normalise_key(reading.ap_agency):
        return None
    return hint


def _ticket_row(
    t: TicketLine, pax_name: str, pax_type: PaxType, rloc: str | None, warnings: list[str]
) -> ProposedTicket:
    if t.cost is None:
        warnings.append(f"Billet {t.doc_number} : coût à saisir.")
    if t.emd:
        warnings.append(W_EMD)
    return ProposedTicket(
        doc_number=t.doc_number,
        airline=t.airline,
        total_doc=t.cost,
        pax_name=pax_name,
        pax_type=pax_type,
        rloc=rloc or "",
        trnc=TRNC_EMD if t.emd else TRNC_TICKET,
    )


def proposed_sales(
    reading: PnrReading,
    partners: Sequence[tuple[int, str]],
    aliases: Sequence[tuple[int, str]] = (),
) -> list[ProposedSale]:
    """One proposed sale per passenger that has tickets (see the module docstring). ``aliases``
    are the approved ``(partner id, alias)`` pairs: they match like the partners' own names."""
    known = [*partners, *aliases]
    agency = _suggest_agency(reading, partners, known)
    client = _suggest_client(reading, known, agency.partner_id)
    phone = reading.ap_phones[0] if reading.ap_phones else None
    # The RM price is P.B unless a partner is recognised (its P.B is cost + fee). An agency that
    # is unknown or ambiguous may still be a direct sale once the agent answers, so its RM price
    # is kept too; the server ignores a P.B for a partner sale.
    priced = agency.ag_kind != _PARTNER

    def build(
        name: str,
        pax_type: PaxType,
        tickets: list[TicketLine],
        extra: tuple[str, ...] = (),
        orphan: bool = False,
    ) -> ProposedSale:
        warnings = [*agency.warnings, *extra]
        rows = tuple(_ticket_row(t, name, pax_type, reading.rloc, warnings) for t in tickets)
        pb: Decimal | None = None
        if priced and not orphan:
            if reading.rm_prices is None:
                warnings.append(W_RM_UNREADABLE if reading.rm_raw else W_RM_MISSING)
            else:
                pb = reading.rm_prices.for_type(pax_type)
                if pb is None:
                    warnings.append(f"Prix {pax_type} absent de la ligne RM : à saisir.")
        if reading.exchanges:
            warnings.append(W_EXCHANGE)
        warnings.extend(
            f"montant non remboursable : {a} MRU" for a in reading.nonrefundable_amounts
        )
        return ProposedSale(
            pax_name=name,
            tickets=rows,
            ag_kind=agency.ag_kind,
            partner_id=agency.partner_id,
            pb=pb,
            clt=client,
            ap=phone,
            staff_sign=reading.agent_sign,
            warnings=tuple(dict.fromkeys(warnings)),
            agency_text=agency.text,
            agency_match=agency.match,
            suggested_partner_id=agency.suggested,
        )

    infant_tickets: dict[int | None, list[TicketLine]] = {}
    for t in reading.tickets:
        if t.is_infant:
            infant_tickets.setdefault(t.passenger, []).append(t)
    infant_count: dict[int, int] = {}
    for p in reading.passengers:
        if p.pax_type == "INF":
            infant_count[p.number] = infant_count.get(p.number, 0) + 1

    sales: list[ProposedSale] = []
    missing: list[str] = []
    claimed: set[str] = set()
    infants_seen: dict[int, int] = {}
    for p in reading.passengers:
        extra: tuple[str, ...] = ()
        if p.pax_type == "INF":
            index = infants_seen.get(p.number, 0)
            infants_seen[p.number] = index + 1
            pool = infant_tickets.get(p.number, [])
            mine = pool[index : index + 1]
            if pool and len(pool) != infant_count[p.number]:
                extra = (W_INFANT_PAIRING,)
        else:
            mine = [t for t in reading.tickets if not t.is_infant and t.passenger == p.number]
        if not mine:
            missing.append(f"Aucun billet trouvé pour le passager {p.number} ({p.pax_type}).")
            continue
        claimed.update(t.doc_number for t in mine)
        sales.append(build(p.name, p.pax_type, mine, extra))
    orphans = [t for t in reading.tickets if t.doc_number not in claimed]
    if orphans:
        sales.append(build("", "ADT", orphans, (W_ORPHAN,), orphan=True))
    if missing:
        sales = [replace(s, warnings=tuple(dict.fromkeys((*s.warnings, *missing)))) for s in sales]
    return sales
