"""The registry of chat lookups: fixed, read-only, run with the logged-in person's rights.

Each ``Tool`` has a ``name``, a French ``description``, ``examples``, a permission predicate (from
``app.permissions``: no role is compared here), typed ``params`` and a ``handler`` that returns a
``Reply`` (structured ``data`` plus the lines that show it). A handler:

* is guarded by its own permission check (``tool(...)`` wraps it), on top of the check in
  ``Tool.run``: calling a handler directly cannot skip the permission;
* reuses the services the screens use (``app.sales``, ``app.flags``, ``app.export_apres``,
  ``app.web.sale_paste``), which already filter on the user's agency and, for staff, signs: another
  agent's sale, another agency's anything, is "not found", with the same words as a missing one;
* takes the agency and the user from ``ChatContext`` (the session), never from the message;
* writes nothing, and builds its text from fixed phrases, typed values and the application's own
  records: nothing the person typed is echoed.

``Tool.input_schema`` and ``Tool.check_args`` are what part 2 gives to Claude: the model names a
tool and arguments, and they go through the same checks as a message the router parsed.

Partner accounts (phase F4: ``partner_balance``, ``partners_owing``, ``partner_statement``) are for
the manager and the accountant (``permissions.can_manage_partners``); staff get the same "not
available" reply as for any other money lookup. A partner is named by text (``partner``, at most
``MAX_PARTNER_ARG`` characters), matched in Python against the agency's active partners and their
approved aliases (``pnr_sales.find_partners``, the paste screen's rule): the text never reaches a
query. No match, or several, is answered so (the candidates' names are listed in the lines for
the person to choose); a partner of another agency is never a candidate. Amounts go to ``data``
as plain two-decimal strings; names only to the lines.
"""

from __future__ import annotations

import functools
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import (
    accounts,
    days,
    export_apres,
    flags,
    partner_alerts,
    partner_aliases,
    partner_book,
    partners,
    permissions,
    sales,
)
from app.chat import reply as r
from app.chat.reply import Line, Reply
from app.chat.text import find_doc
from app.formatting import date_fr, money_fr
from app.models import (
    AG_DIRECT,
    DAY_CLOSED,
    FLAG_NOT_SENT,
    FLAG_OPEN,
    Agency,
    Partner,
    ReportDay,
    User,
)
from app.parsing.pnr_sales import find_partners
from app.partner_ledger import ALERT_DUE_SOON, ALERT_OVERDUE, CENT, half_month
from app.sales_rules import SaleView
from app.web import sale_paste

MAX_ITEMS = 20  # rows of one list; the rest is a link to the screen
MAX_FLAG_ROWS = 10

KIND_LABELS = {
    "different": "Différent",
    "not_sent": "Non envoyé",
    "not_in_amadeus": "Absent d'Amadeus",
    "elsewhere": "Ailleurs",
    "bad_number": "Numéro invalide",
}
STATUS_LABELS = {"open": "ouvert", "answered": "répondu"}  # the only two a lookup returns

BAD_DAY = "Je n'ai pas compris la date."
BAD_DAY_HINT = BAD_DAY + " Écrivez par exemple 19/09, 19 septembre ou « hier »."
BAD_DOC = "Je n'ai pas compris le numéro du billet (10 chiffres)."
BAD_SIGN = "Je n'ai pas compris le signe."
BAD_REQUEST = "Demande non comprise."
MAX_DOC_ARG = 32  # characters of a ticket number argument (ten digits, an airline code, spaces)
MAX_SIGN_ARG = 16  # characters of a staff sign argument (``0004SV``)
MAX_PARTNER_ARG = 80  # characters of a partner name argument
BAD_PARTNER = "Je n'ai pas compris le nom du partenaire."
# What a model is told on top of ``Param.hint``: how to write the value.
SCHEMA_HINTS = {
    "day": " (date ISO AAAA-MM-JJ, ex. 2026-10-19)",
    "sign": " (ex. 0004SV)",
    "partner": " (tel que la personne l'a écrit)",
}
NO_SUCH_TOOL = "Cette demande n'existe pas."
DENIED = "Cette demande n'est pas disponible pour votre rôle."
NO_TICKET = "Aucun billet {doc} trouvé parmi vos ventes."
VOID_WHY = {sales.VOID_DAY_CLOSED: "la journée est clôturée"}


class ToolArgError(ValueError):
    """An argument that is missing or not usable; the message is French and fit to show."""


@dataclass(frozen=True)
class ChatContext:
    """Who asks, and when: the session's user and the application clock. The agency is
    ``user.agency_id``; the database session is already bound to it."""

    db: Session
    user: User
    now: datetime

    @property
    def today(self) -> date:
        return self.now.date()


@dataclass(frozen=True)
class Param:
    """``kind``: ``day`` (a date), ``doc`` (a ticket number, normalised to ten digits), ``sign``
    (a staff sign), ``partner`` (a partner's name, spaces collapsed), ``text`` or ``flag`` (a
    boolean). ``hint`` is the French noun phrase used
    both to say what is missing and to describe the argument to a model."""

    name: str
    kind: str
    hint: str
    required: bool = False


Handler = Callable[[ChatContext, Mapping[str, Any]], Reply]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    examples: tuple[str, ...]
    allowed: Callable[[User], bool]
    params: tuple[Param, ...]
    handler: Handler

    def input_schema(self) -> dict[str, Any]:
        """A JSON schema of the arguments (what a model is told)."""
        kinds = {"day": ("string", "date"), "doc": ("string", ""), "sign": ("string", "")}
        properties: dict[str, Any] = {}
        for p in self.params:
            kind, fmt = kinds.get(p.kind, ("boolean" if p.kind == "flag" else "string", ""))
            properties[p.name] = {
                "type": kind,
                "description": p.hint + SCHEMA_HINTS.get(p.kind, ""),
            }
            if fmt:
                properties[p.name]["format"] = fmt
        return {
            "type": "object",
            "properties": properties,
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    def check_args(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """The arguments, typed and normalised. Unknown names and wrong types are refused."""
        known = {p.name: p for p in self.params}
        if set(raw) - set(known):
            raise ToolArgError(BAD_REQUEST)
        out: dict[str, Any] = {}
        for p in self.params:
            value = raw.get(p.name)
            if value is None or value == "":
                if p.required:
                    raise ToolArgError(f"Il manque {p.hint}.")
                continue
            out[p.name] = _coerce(p, value)
        return out

    def run(self, ctx: ChatContext, raw: Mapping[str, Any]) -> Reply:
        """Check the permission, then the arguments, then answer."""
        if not self.allowed(ctx.user):
            return _denied(self.name)
        try:
            args = self.check_args(raw)
        except ToolArgError as exc:
            return r.refuse(r.INVALID, r.say(str(exc)), tool=self.name)
        return self.handler(ctx, args)


def _coerce(p: Param, value: Any) -> Any:
    if p.kind == "day":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        try:
            return date.fromisoformat(value) if isinstance(value, str) else _bad(BAD_DAY)
        except ValueError:
            raise ToolArgError(BAD_DAY) from None
    if p.kind == "doc":
        doc = find_doc(value) if isinstance(value, str) and len(value) <= MAX_DOC_ARG else None
        return doc if doc is not None else _bad(BAD_DOC)
    if p.kind == "sign":
        try:
            if not isinstance(value, str) or len(value) > MAX_SIGN_ARG:
                return _bad(BAD_SIGN)
            return accounts.normalize_staff_sign(value)
        except accounts.AccountError:
            raise ToolArgError(BAD_SIGN) from None
    if p.kind == "flag":
        return value if isinstance(value, bool) else _bad(BAD_REQUEST)
    if p.kind == "partner":
        name = " ".join(value.split()) if isinstance(value, str) else ""
        return name if name and len(name) <= MAX_PARTNER_ARG else _bad(BAD_PARTNER)
    return value if isinstance(value, str) else _bad(BAD_REQUEST)


def _bad(message: str) -> Any:
    raise ToolArgError(message)


TOOLS: dict[str, Tool] = {}


def _denied(name: str) -> Reply:
    return r.refuse(r.DENIED, r.say(DENIED), tool=name)


def tool(
    name: str,
    description: str,
    allowed: Callable[[User], bool],
    *,
    examples: tuple[str, ...] = (),
    params: tuple[Param, ...] = (),
) -> Callable[[Handler], Handler]:
    """Register a handler in ``TOOLS``, guarded by ``allowed`` (checked again on every call)."""

    def register(handler: Handler) -> Handler:
        @functools.wraps(handler)
        def guarded(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
            return handler(ctx, args) if allowed(ctx.user) else _denied(name)

        TOOLS[name] = Tool(name, description, examples, allowed, params, guarded)
        return guarded

    return register


def run_tool(ctx: ChatContext, name: str, raw: Mapping[str, Any]) -> Reply:
    """Run a registered tool by name (the router and, in part 2, the model call this). Arguments
    that are not an object are refused here, whatever a model sent."""
    found = TOOLS.get(name)
    if found is None:
        return r.refuse(r.INVALID, r.say(NO_SUCH_TOOL))
    if not isinstance(raw, Mapping):
        return r.refuse(r.INVALID, r.say(BAD_REQUEST), tool=name)
    return found.run(ctx, raw)


def tools_for(user: User) -> list[Tool]:
    """The tools this user may use, in registry order."""
    return [t for t in TOOLS.values() if t.allowed(user)]


# --- shared lookups ---------------------------------------------------------------------------


def _my_flags(ctx: ChatContext, *, doc: str | None = None) -> list[flags.StoredFlag]:
    """The flags still waiting that this user may see (``app.flags`` filters by sign for staff);
    ``doc`` keeps one ticket's flags, filtered in the query."""
    return flags.flags_for_me(ctx.db, ctx.user, doc=doc)


def _day_dates(ctx: ChatContext, found: list[flags.StoredFlag]) -> dict[int, date]:
    ids = {f.report_day_id for f in found}
    if not ids:
        return {}
    rows = ctx.db.execute(
        select(ReportDay.id, ReportDay.day).where(
            ReportDay.agency_id == ctx.user.agency_id, ReportDay.id.in_(ids)
        )
    )
    return {row.id: row.day for row in rows}


def _visible_sale(ctx: ChatContext, doc: str) -> SaleView | None:
    """The active sale that holds the ticket, if this user may see it (else None, exactly as for
    a ticket that is not there)."""
    located = sales.active_ticket_lookup(ctx.db, ctx.user.agency_id, [doc])
    if doc not in located:
        return None
    return sales.get_sale_for(ctx.db, ctx.user, located[doc][0])


def _docs(view: SaleView) -> str:
    shown = ", ".join(t.doc_number for t in view.tickets[:3])
    more = len(view.tickets) - 3
    return f"{shown} (+{more})" if more > 0 else shown


def _missing(view: SaleView, open_by_doc: Counter[str]) -> list[str]:
    """What is still to do on a sale: the client's phone of a direct sale (neither given nor
    refused), and the comparison flags still open on its tickets."""
    found: list[str] = []
    if view.ag_kind == AG_DIRECT and view.ap is None and not view.client_refused_phone:
        found.append("numéro du client")
    open_count = sum(open_by_doc[t.doc_number] for t in view.tickets)
    if open_count:
        found.append(f"{open_count} écart(s) à traiter")
    return found


def _flag_href(f: flags.StoredFlag, day: date | None) -> str | None:
    """Where a flag is dealt with: the entry form for a ticket that was not sent, else the day's
    list of flags."""
    if f.kind == FLAG_NOT_SENT:
        return f"/flags/{f.id}/enter"
    return f"/flags?date={day.isoformat()}" if day else None


def _flag_line(f: flags.StoredFlag, *, day: date | None) -> Line:
    when = f" · {date_fr(day)}" if day else ""
    text = f"{f.doc_number} · {KIND_LABELS.get(f.kind, f.kind)} · {f.responsible_sign}{when}"
    return Line(text, _flag_href(f, day))


# --- the tools --------------------------------------------------------------------------------


@tool(
    "list_sales",
    "Vos ventes d'un jour et ce qui manque encore sur chacune.",
    permissions.can_use_sales_screens,
    examples=("mes billets d'aujourd'hui", "billets du 19 septembre", "billets de 0004SV"),
    params=(
        Param("day", "day", "le jour (par défaut aujourd'hui)"),
        Param("sign", "sign", "le signe de l'agent"),
        Param("mine", "flag", "seulement les ventes saisies par vous"),
    ),
)
def list_sales(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    user = ctx.user
    day: date = args.get("day") or ctx.today
    sign: str | None = args.get("sign")
    own_only = bool(args.get("mine")) and permissions.sees_all_signs(user)
    visible = sales.visible_signs(ctx.db, user)  # read once, passed to everything below
    if visible == []:
        return r.reply("list_sales", r.say(sales.NO_SIGN), day=day.isoformat(), count=0)
    views = sales.sales_for_day_within(ctx.db, user, day, visible, signs=[sign] if sign else None)
    if own_only:
        views = [v for v in views if v.created_by_user_id == user.id]
    title = "Vos ventes du" if own_only or not permissions.sees_all_signs(user) else "Ventes du"
    if not views:
        return r.reply(
            "list_sales", r.say(f"Aucune vente le {date_fr(day)}."), day=day.isoformat(), count=0
        )
    waiting = flags.flags_within(ctx.db, user, visible)
    open_by_doc = Counter(f.doc_number for f in waiting if f.status == FLAG_OPEN)
    shown = views[:MAX_ITEMS]
    lines = [r.say(f"{title} {date_fr(day)} : {len(views)} vente(s).")]
    items: list[dict[str, Any]] = []
    for view in shown:
        missing = _missing(view, open_by_doc)
        state = "manque : " + ", ".join(missing) if missing else "complète"
        lines.append(r.link(f"{_docs(view)} · {view.ag_label} · {state}", f"/sales/{view.id}"))
        items.append(
            {"sale_id": view.id, "docs": [t.doc_number for t in view.tickets], "missing": missing}
        )
    if len(views) > len(shown):
        more = f"… et {len(views) - len(shown)} autre(s)"
        lines.append(r.link(more, f"/sales?date={day.isoformat()}"))
    return r.reply("list_sales", *lines, day=day.isoformat(), count=len(views), sales=items)


@tool(
    "open_flags",
    "Les écarts bloquants avec le rapport Amadeus (par agent pour le gérant et le comptable).",
    permissions.can_answer_flags,
    examples=("qu'est-ce qui bloque ?", "flags"),
)
def open_flags(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    found = [f for f in _my_flags(ctx) if f.status == FLAG_OPEN and f.blocking]
    if not found:
        return r.reply("open_flags", r.say("Rien ne bloque : aucun écart ouvert."), count=0)
    dates = _day_dates(ctx, found)
    lines = [r.say(f"{len(found)} écart(s) bloquant(s) ouvert(s).")]
    by_sign = Counter(f.responsible_sign for f in found)
    if permissions.sees_all_signs(ctx.user):
        lines += [r.say(f"{sign} : {n}") for sign, n in sorted(by_sign.items())]
    by_day = Counter(f.report_day_id for f in found)
    for day_id in sorted(by_day, key=lambda i: dates.get(i, date.min)):
        day = dates.get(day_id)
        if day is not None:
            lines.append(
                r.link(f"{date_fr(day)} : {by_day[day_id]}", f"/flags?date={day.isoformat()}")
            )
    lines += [_flag_line(f, day=dates.get(f.report_day_id)) for f in found[:MAX_FLAG_ROWS]]
    if len(found) > MAX_FLAG_ROWS:
        lines.append(r.say(f"… et {len(found) - MAX_FLAG_ROWS} autre(s)."))
    return r.reply("open_flags", *lines, count=len(found), by_sign=dict(by_sign))


@tool(
    "not_sent_tickets",
    "Les billets du rapport Amadeus qui ne sont pas encore dans l'application.",
    permissions.can_answer_flags,
    examples=("billets non envoyés", "manquants"),
)
def not_sent_tickets(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    found = [f for f in _my_flags(ctx) if f.status == FLAG_OPEN and f.kind == FLAG_NOT_SENT]
    if not found:
        return r.reply(
            "not_sent_tickets",
            r.say("Aucun billet manquant : tout est dans l'application."),
            count=0,
        )
    dates = _day_dates(ctx, found)
    lines = [r.say(f"{len(found)} billet(s) du rapport Amadeus ne sont pas dans l'application.")]
    lines += [_flag_line(f, day=dates.get(f.report_day_id)) for f in found[:MAX_ITEMS]]
    if len(found) > MAX_ITEMS:
        lines.append(r.say(f"… et {len(found) - MAX_ITEMS} autre(s)."))
    return r.reply(
        "not_sent_tickets",
        *lines,
        count=len(found),
        docs=[f.doc_number for f in found[:MAX_ITEMS]],
    )


@tool(
    "ticket_status",
    "L'état d'un billet : sa vente, ce qui manque, les écarts ouverts.",
    permissions.can_use_sales_screens,
    examples=("billet 3940299439",),
    params=(Param("doc", "doc", "le numéro du billet (10 chiffres)", required=True),),
)
def ticket_status(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    doc: str = args["doc"]
    view = _visible_sale(ctx, doc)
    mine = _my_flags(ctx, doc=doc)
    if view is None and not mine:
        return r.reply("ticket_status", r.say(NO_TICKET.format(doc=doc)), doc=doc, found=False)
    lines: list[Line] = []
    data: dict[str, Any] = {"doc": doc, "found": True, "flags": [f.id for f in mine]}
    if view is not None:
        open_by_doc = Counter(f.doc_number for f in mine if f.status == FLAG_OPEN)
        missing = _missing(view, open_by_doc)
        lines.append(r.say(f"Billet {doc} : vente enregistrée le {date_fr(view.day)}."))
        lines.append(r.say(f"Signe {view.staff_sign} · {view.ag_label}."))
        lines.append(
            r.say("Il manque : " + ", ".join(missing) + "." if missing else "Rien ne manque.")
        )
        data.update(sale_id=view.id, day=view.day.isoformat(), missing=missing)
    else:
        lines.append(r.say(f"Billet {doc} : pas de vente enregistrée dans l'application."))
    dates = _day_dates(ctx, mine)
    for f in mine:
        state = STATUS_LABELS.get(f.status, f.status)
        label = f"Écart {state} : {KIND_LABELS.get(f.kind, f.kind)}"
        lines.append(Line(label, _flag_href(f, dates.get(f.report_day_id))))
    if view is not None:
        lines.append(r.link("Ouvrir la vente", f"/sales/{view.id}"))
    return r.reply("ticket_status", *lines, **data)


@tool(
    "day_sheet",
    "Le lien de la feuille TJQ APRES d'un jour (gérant et comptable).",
    permissions.can_export_day,
    examples=("feuille du 19 septembre", "feuille d'hier"),
    params=(Param("day", "day", "le jour (par défaut aujourd'hui)"),),
)
def day_sheet(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    day: date = args.get("day") or ctx.today
    iso = day.isoformat()
    if day > ctx.today:
        return r.reply(
            "day_sheet", r.say("Cette date est dans le futur."), day=iso, available=False
        )
    agency_id = ctx.user.agency_id
    report_day = days.get_day(ctx.db, agency_id, day)
    if report_day is not None and report_day.status == DAY_CLOSED:
        stored = export_apres.get_export(ctx.db, agency_id, report_day.id, None)
        if stored is None:
            return r.reply(
                "day_sheet",
                r.say(f"La journée du {date_fr(day)} est clôturée, sans feuille enregistrée."),
                day=iso,
                available=False,
            )
        return r.reply(
            "day_sheet",
            r.say(
                f"Feuille TJQ APRES du {date_fr(day)} (journée clôturée, version {stored.version})."
            ),
            r.button("Télécharger la feuille", f"/days/{iso}/apres.xlsx"),
            day=iso,
            available=True,
            draft=False,
        )
    if flags.latest_upload_for_day(ctx.db, agency_id, day) is None:
        return r.reply(
            "day_sheet",
            r.say(f"Aucun rapport Amadeus pour le {date_fr(day)} : pas de feuille."),
            day=iso,
            available=False,
        )
    return r.reply(
        "day_sheet",
        r.say(f"Feuille TJQ APRES du {date_fr(day)} : brouillon (la journée n'est pas clôturée)."),
        r.button("Télécharger le brouillon", f"/days/{iso}/apres-preview.xlsx"),
        r.link("Voir la journée", f"/days/{iso}"),
        day=iso,
        available=True,
        draft=True,
    )


@tool(
    "paste_sale",
    "Collez le texte d'une réservation Amadeus : j'ouvre la saisie préremplie (rien n'est saisi).",
    permissions.can_use_sales_screens,
    params=(Param("text", "text", "le texte Amadeus", required=True),),
)
def paste_sale(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    text: str = args["text"]
    problem = sale_paste.check_text(text)
    if problem:
        return r.refuse(r.INVALID, r.say(problem), tool="paste_sale")
    result = sale_paste.read_paste(ctx.db, ctx.user.agency_id, text)
    if not result.proposals:
        return r.reply("paste_sale", r.say(sale_paste.NOTHING), sales=0)
    count = len(result.proposals)
    return Reply(
        r.OK,
        (
            r.say(f"J'ai lu {count} vente(s) dans ce texte."),
            r.say("Rien n'est enregistré : vous vérifiez et confirmez dans la saisie."),
            r.link("Ouvrir la saisie", "/sales/new"),
        ),
        "paste_sale",
        {"sales": count},
        handoff_text=text,
    )


@tool(
    "void_link",
    "Le lien pour annuler une vente : l'annulation se confirme sur son écran, jamais ici.",
    permissions.can_use_sales_screens,
    examples=("annule 3940299439",),
    params=(Param("doc", "doc", "le numéro du billet (10 chiffres)", required=True),),
)
def void_link(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    doc: str = args["doc"]
    view = _visible_sale(ctx, doc)
    if view is None:
        return r.reply("void_link", r.say(NO_TICKET.format(doc=doc)), doc=doc, found=False)
    report_day = days.get_day(ctx.db, ctx.user.agency_id, view.day)
    blocker = sales.void_blocker(ctx.user, report_day.status if report_day else None)
    why = VOID_WHY.get(blocker or "", "")
    if why:
        return r.reply(
            "void_link",
            r.say(f"Je ne peux pas ouvrir l'annulation du billet {doc} : {why}."),
            doc=doc,
            sale_id=view.id,
            can_void=False,
        )
    return r.reply(
        "void_link",
        r.say(f"Billet {doc} : rien n'est annulé depuis la discussion."),
        r.say("L'écran d'annulation vous demande un motif et une confirmation."),
        r.button("Ouvrir l'annulation", f"/sales/{view.id}/void"),
        doc=doc,
        sale_id=view.id,
        can_void=True,
    )


# --- partner accounts (F4) ---------------------------------------------------------------------

NO_PARTNER = "Aucun partenaire actif ne correspond à ce nom."
SEVERAL_PARTNERS = "Plusieurs partenaires correspondent à ce nom : lequel ?"
OWES_US = "le partenaire doit cette somme à l'agence"
WE_OWE = "l'agence doit cette somme au partenaire"
SETTLED = "compte soldé"
LEVEL_WORDS = {ALERT_OVERDUE: "en retard", ALERT_DUE_SOON: "bientôt dû"}


def _amount(value: Decimal) -> str:
    """An amount for ``data``: a plain two-decimal string (``"-2505.00"``)."""
    return str(value.quantize(CENT))


def _currency(ctx: ChatContext) -> str:
    found = ctx.db.scalar(select(Agency.currency).where(Agency.id == ctx.user.agency_id))
    return found or ""


def _partner_named(ctx: ChatContext, name: str) -> list[Partner]:
    """The active partners of the user's agency that ``name`` designates: their own names and
    approved aliases, matched in Python (``find_partners``). Two queries; the text is never in
    them."""
    agency_id = ctx.user.agency_id
    active = partners.list_active(ctx.db, agency_id)
    known = [(p.id, p.name) for p in active] + partner_aliases.approved_pairs(ctx.db, agency_id)
    by_id = {p.id: p for p in active}
    return [by_id[i] for i in find_partners(name, known).ids if i in by_id]


def _not_one(tool_name: str, found: list[Partner]) -> Reply:
    """No partner, or several: say so; the candidates are listed for the person to choose."""
    if not found:
        return r.reply(
            tool_name,
            r.say(NO_PARTNER),
            r.link("Voir les comptes partenaires", "/partner-accounts"),
            found=False,
            matches=0,
        )
    lines = [r.say(SEVERAL_PARTNERS)]
    lines += [r.link(p.name, f"/partner-accounts/{p.id}") for p in found[:MAX_ITEMS]]
    return r.reply(tool_name, *lines, found=False, matches=len(found))


def _days(n: int) -> str:
    return f"{n} jour" if n == 1 else f"{n} jours"


def _who_owes(balance: Decimal) -> str:
    if balance > 0:
        return OWES_US
    return WE_OWE if balance < 0 else SETTLED


@tool(
    "partner_balance",
    "Le solde d'un partenaire et son plus ancien impayé (gérant et comptable).",
    permissions.can_manage_partners,
    examples=("solde de Partenaire Test",),
    params=(Param("partner", "partner", "le nom du partenaire", required=True),),
)
def partner_balance(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    found = _partner_named(ctx, args["partner"])
    if len(found) != 1:
        return _not_one("partner_balance", found)
    partner = found[0]
    acc = partner_book.account(ctx.db, ctx.user.agency_id, partner, ctx.today)
    oldest = partner_book.oldest_unpaid(acc, ctx.today)
    cur = _currency(ctx)
    balance = acc.balance
    lines = [r.say(f"Solde de {partner.name} : {money_fr(balance)} {cur} ({_who_owes(balance)}).")]
    data: dict[str, Any] = {"found": True, "partner_id": partner.id, "balance": _amount(balance)}
    if oldest is None:
        lines.append(r.say("Aucun impayé sur une période clôturée."))
    else:
        late = max(0, (ctx.today - oldest.due).days)
        state = (
            "en retard de " + _days(late)
            if oldest.level == ALERT_OVERDUE
            else LEVEL_WORDS.get(oldest.level, "pas encore échu")
        )
        lines.append(
            r.say(
                f"Plus ancien impayé : {money_fr(oldest.amount)} {cur}, période du "
                f"{partner_book.period_label(oldest.start, oldest.end)}, échéance le "
                f"{date_fr(oldest.due)} ({state})."
            )
        )
        data["oldest"] = {
            "amount": _amount(oldest.amount),
            "start": oldest.start.isoformat(),
            "end": oldest.end.isoformat(),
            "due": oldest.due.isoformat(),
            "level": oldest.level,
            "days_late": late,
        }
    lines.append(r.link("Ouvrir le compte", f"/partner-accounts/{partner.id}"))
    return r.reply("partner_balance", *lines, **data)


@tool(
    "partners_owing",
    "Les partenaires en retard de paiement ou bientôt dus, les plus en retard d'abord (gérant et "
    "comptable).",
    permissions.can_manage_partners,
    examples=("qui doit de l'argent ?", "impayés"),
)
def partners_owing(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    alerts = partner_alerts.partner_alerts(ctx.db, ctx.user, ctx.today)
    if alerts is None:  # pragma: no cover - the permission refused it already
        return _denied("partners_owing")
    owing = alerts.owing
    refunds = len(alerts.refunds)
    data: dict[str, Any] = {
        "count": len(owing),
        "overdue": len(alerts.overdue),
        "due_soon": len(alerts.due_soon),
        "refunds": refunds,
    }
    lines: list[Line] = []
    if not owing:
        lines.append(r.say("Aucun partenaire en retard ni bientôt dû."))
    else:
        lines.append(
            r.say(
                f"{len(alerts.overdue)} partenaire(s) en retard, "
                f"{len(alerts.due_soon)} bientôt dû(s)."
            )
        )
        cur = _currency(ctx)
        for a in owing[:MAX_ITEMS]:
            when = (
                "en retard de " + _days(a.days_late)
                if a.overdue
                else f"bientôt dû, le {date_fr(a.due)}"
            )
            lines.append(
                r.link(
                    f"{a.name} · {money_fr(a.amount)} {cur} · {when}",
                    f"/partner-accounts/{a.partner_id}",
                )
            )
        if len(owing) > MAX_ITEMS:
            lines.append(r.link(f"… et {len(owing) - MAX_ITEMS} autre(s)", "/partner-accounts"))
        data["partners"] = [
            {
                "partner_id": a.partner_id,
                "amount": _amount(a.amount),
                "level": a.level,
                "due": a.due.isoformat(),
                "days_late": a.days_late,
            }
            for a in owing[:MAX_ITEMS]
        ]
    if refunds:
        lines.append(r.link(f"{refunds} remboursement(s) Amadeus à confirmer.", "/"))
    return r.reply("partners_owing", *lines, **data)


def _period_totals(period: partner_book.PeriodView) -> tuple[Decimal, ...]:
    """Opening, sales, lines, payments and closing of a period: a closed one from its row (what
    was sent), an open one as computed now."""
    row = period.row
    if period.closed and row is not None:
        values = (
            row.opening_balance,
            row.sales_total,
            row.entries_total,
            row.payments_total,
            row.closing_balance,
        )
        return tuple(v if v is not None else Decimal(0) for v in values)
    t = period.totals
    if t is None:  # pragma: no cover - an open period is always computed
        return (period.opening, Decimal(0), Decimal(0), Decimal(0), period.closing)
    return (t.opening, t.sales_total, t.entries_total, t.payments_total, t.closing)


@tool(
    "partner_statement",
    "Le relevé d'un partenaire pour une quinzaine : le résumé, le lien vers la période et son "
    "fichier Excel (gérant et comptable).",
    permissions.can_manage_partners,
    examples=("relevé Partenaire Test 1-15 octobre",),
    params=(
        Param("partner", "partner", "le nom du partenaire", required=True),
        Param("day", "day", "un jour de la quinzaine (par défaut la dernière période clôturée)"),
    ),
)
def partner_statement(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    found = _partner_named(ctx, args["partner"])
    if len(found) != 1:
        return _not_one("partner_statement", found)
    partner = found[0]
    acc = partner_book.account(ctx.db, ctx.user.agency_id, partner, ctx.today)
    day: date | None = args.get("day")
    base: dict[str, Any] = {"found": True, "partner_id": partner.id}
    account_link = r.link("Ouvrir le compte", f"/partner-accounts/{partner.id}")
    if day is not None:
        start, end = half_month(day)
        period = next((p for p in acc.periods if p.start == start), None)
        if period is None:
            label = partner_book.period_label(start, end)
            return r.reply(
                "partner_statement",
                r.say(f"{partner.name} n'a pas de période du {label}."),
                account_link,
                **base,
                available=False,
                start=start.isoformat(),
                end=end.isoformat(),
            )
    else:
        closed = [p for p in acc.periods if p.closed]
        period = closed[-1] if closed else (acc.periods[-1] if acc.periods else None)
        if period is None:
            return r.reply(
                "partner_statement",
                r.say(f"{partner.name} n'a encore aucune période."),
                account_link,
                **base,
                available=False,
            )
    opening, sold, entries, paid, closing = _period_totals(period)
    cur = _currency(ctx)
    state = "clôturée" if period.closed else "ouverte : relevé provisoire"
    iso = period.start.isoformat()
    lines = [
        r.say(f"Relevé de {partner.name}, période du {period.label} ({state})."),
        r.say(
            f"Solde précédent {money_fr(opening)} · ventes {money_fr(sold)} · lignes "
            f"{money_fr(entries)} · paiements {money_fr(paid)} · nouveau solde "
            f"{money_fr(closing)} {cur}."
        ),
    ]
    if period.pending:
        lines.append(r.say(f"{len(period.pending)} ligne(s) en attente de décision."))
    lines.append(r.link("Voir la période", f"/partner-accounts/{partner.id}/periods/{iso}"))
    lines.append(
        r.button(
            "Télécharger le relevé (Excel)",
            f"/partner-accounts/{partner.id}/periods/{iso}/statement.xlsx",
        )
    )
    return r.reply(
        "partner_statement",
        *lines,
        **base,
        available=True,
        start=iso,
        end=period.end.isoformat(),
        closed=period.closed,
        opening=_amount(opening),
        sales_total=_amount(sold),
        entries_total=_amount(entries),
        payments_total=_amount(paid),
        closing=_amount(closing),
        pending=len(period.pending),
    )


@tool(
    "help",
    "La liste de ce que je sais faire.",
    permissions.can_use_chat,
    examples=("aide",),
)
def help_tool(ctx: ChatContext, args: Mapping[str, Any]) -> Reply:
    return r.reply("help", *help_lines(ctx.user), tools=[t.name for t in tools_for(ctx.user)])


def help_lines(user: User) -> tuple[Line, ...]:
    """What the chat can do for this role: one line for each tool the role may use."""
    lines = [r.say("Voici ce que je sais faire :")]
    for t in tools_for(user):
        if t.name == "help":
            continue
        example = f" Exemple : « {t.examples[0]} »" if t.examples else ""
        lines.append(r.say(f"- {t.description}{example}"))
    return tuple(lines)
