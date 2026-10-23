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
"""

from __future__ import annotations

import functools
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import accounts, days, export_apres, flags, permissions, sales
from app.chat import reply as r
from app.chat.reply import Line, Reply
from app.chat.text import find_doc
from app.formatting import date_fr
from app.models import (
    AG_DIRECT,
    DAY_CLOSED,
    FLAG_NOT_SENT,
    FLAG_OPEN,
    ReportDay,
    User,
)
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
NO_SUCH_TOOL = "Cette demande n'existe pas."
DENIED = "Cette demande n'est pas disponible pour votre rôle."
NO_TICKET = "Aucun billet {doc} trouvé parmi vos ventes."
VOID_WHY = {
    sales.VOID_DAY_CLOSED: "la journée est clôturée",
    sales.VOID_NOT_TODAY: "seule une vente du jour peut être annulée par vous",
}


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
    (a staff sign), ``text`` or ``flag`` (a boolean). ``hint`` is the French noun phrase used
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
            properties[p.name] = {"type": kind, "description": p.hint}
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
            raise ToolArgError("Demande non comprise.")
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
        doc = find_doc(value) if isinstance(value, str) else None
        return doc if doc is not None else _bad(BAD_DOC)
    if p.kind == "sign":
        try:
            return (
                accounts.normalize_staff_sign(value) if isinstance(value, str) else _bad(BAD_SIGN)
            )
        except accounts.AccountError:
            raise ToolArgError(BAD_SIGN) from None
    if p.kind == "flag":
        return value if isinstance(value, bool) else _bad("Demande non comprise.")
    return value if isinstance(value, str) else _bad("Demande non comprise.")


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
    """Run a registered tool by name (the router and, in part 2, the model call this)."""
    found = TOOLS.get(name)
    if found is None:
        return r.refuse(r.INVALID, r.say(NO_SUCH_TOOL))
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
    blocker = sales.void_blocker(
        ctx.user, view.day, report_day.status if report_day else None, ctx.today
    )
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
