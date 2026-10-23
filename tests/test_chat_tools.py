"""The chat registry: every lookup for every role, isolation between agents and agencies, the typed
arguments, and the shape of each answer. Runs on SQLite through the services; synthetic data."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app import days, permissions, sales
from app.chat import reply as r
from app.chat import tools
from app.chat.reply import Reply
from app.chat.router import route_message
from app.chat.tools import TOOLS, ToolArgError, run_tool
from app.models import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_STAFF, DayExport, User
from tests.chat_support import OTHER_DOC, ChatWorld
from tests.flag_support import D19, D20, NOW

pytest_plugins = ["tests.chat_fixtures"]  # the fixtures ``w`` and ``chat_template``
PNR_DIR = Path(__file__).parent / "fixtures" / "pnr"
ALL_TOOLS = ["list_sales", "open_flags", "not_sent_tickets", "ticket_status", "day_sheet"]
ALL_TOOLS += ["paste_sale", "void_link", "help"]
ARGS: dict[str, dict[str, Any]] = {
    "ticket_status": {"doc": "3940299439"},
    "void_link": {"doc": "3940299439"},
    "paste_sale": {"text": "RP/X"},
}
MANAGER_OR_ACCOUNTANT_ONLY = {"day_sheet"}


def texts(answer: Reply) -> list[str]:
    return [line.text for line in answer.lines]


def hrefs(answer: Reply) -> list[str]:
    return [line.href for line in answer.lines if line.href]


def who(w: ChatWorld, role: str) -> User:
    return {"manager": w.manager, "accountant": w.accountant, "staff": w.staff}[role]


# --- the registry -------------------------------------------------------------------------------


def test_the_registry_holds_exactly_the_documented_tools() -> None:
    assert list(TOOLS) == ALL_TOOLS
    for tool in TOOLS.values():
        assert tool.name in ALL_TOOLS and tool.description.endswith((".", ")", "."))
        assert callable(tool.allowed) and callable(tool.handler)


def test_can_use_chat_follows_the_roles() -> None:
    def user(role: str) -> User:
        return User(agency_id=1, phone="+22200000000", role=role)

    for role in (ROLE_MANAGER, ROLE_ACCOUNTANT, ROLE_STAFF):
        assert permissions.can_use_chat(user(role)) is True
    for role in ("root", "", "MANAGER", "admin"):
        assert permissions.can_use_chat(user(role)) is False


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_every_tool_is_described_for_a_model(name: str) -> None:
    tool = TOOLS[name]
    schema = tool.input_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) <= set(schema["properties"])
    for prop in schema["properties"].values():
        assert prop["type"] in ("string", "boolean") and prop["description"]
    assert set(schema["properties"]) == {p.name for p in tool.params}


@pytest.mark.parametrize("name", ALL_TOOLS)
@pytest.mark.parametrize("role", ["manager", "accountant", "staff"])
def test_permission_matrix(w: ChatWorld, name: str, role: str) -> None:
    """A role gets an answer from a tool only when the tool's predicate says so; otherwise the
    plain "not available" reply, whatever the arguments."""
    answer = run_tool(w.ctx(who(w, role)), name, ARGS.get(name, {}))
    if name in MANAGER_OR_ACCOUNTANT_ONLY and role == "staff":
        assert (answer.outcome, texts(answer)) == (r.DENIED, [tools.DENIED])
    else:
        assert answer.outcome != r.DENIED


@pytest.mark.parametrize("name", ALL_TOOLS)
def test_an_unknown_role_may_use_nothing(w: ChatWorld, name: str) -> None:
    ghost = User(id=999, agency_id=w.agency_id, phone="+22212370099", role="root")
    answer = run_tool(w.ctx(ghost), name, ARGS.get(name, {}))
    assert (answer.outcome, texts(answer)) == (r.DENIED, [tools.DENIED])
    assert answer.data == {}


@pytest.mark.parametrize("name", sorted(MANAGER_OR_ACCOUNTANT_ONLY))
def test_a_handler_called_directly_still_checks_the_permission(w: ChatWorld, name: str) -> None:
    answer = TOOLS[name].handler(w.ctx(w.staff), {})
    assert answer.outcome == r.DENIED


def test_an_unknown_tool_name_is_refused(w: ChatWorld) -> None:
    answer = run_tool(w.ctx(w.manager), "drop_tables", {})
    assert (answer.outcome, texts(answer)) == (r.INVALID, [tools.NO_SUCH_TOOL])


# --- typed arguments ----------------------------------------------------------------------------


def test_arguments_are_typed_and_normalised() -> None:
    tool = TOOLS["list_sales"]
    assert tool.check_args({"day": "2026-09-19", "sign": " 0004sv ", "mine": True}) == {
        "day": date(2026, 9, 19),
        "sign": "0004SV",
        "mine": True,
    }
    assert tool.check_args({"day": date(2026, 9, 19)}) == {"day": date(2026, 9, 19)}
    assert tool.check_args({"day": None, "sign": ""}) == {}
    assert TOOLS["ticket_status"].check_args({"doc": "495-3940299439"}) == {"doc": "3940299439"}


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("ticket_status", {}, "Il manque le numéro du billet (10 chiffres)."),
        ("ticket_status", {"doc": ""}, "Il manque le numéro du billet (10 chiffres)."),
        ("ticket_status", {"doc": "123"}, tools.BAD_DOC),
        ("ticket_status", {"doc": 3940299439}, tools.BAD_DOC),
        ("void_link", {"doc": "abc"}, tools.BAD_DOC),
        ("list_sales", {"day": "19/09"}, tools.BAD_DAY),
        ("list_sales", {"day": "2026-02-31"}, tools.BAD_DAY),
        ("list_sales", {"day": 5}, tools.BAD_DAY),
        ("list_sales", {"sign": "a b!"}, tools.BAD_SIGN),
        ("list_sales", {"sign": 7}, tools.BAD_SIGN),
        ("list_sales", {"mine": "yes"}, "Demande non comprise."),
        ("list_sales", {"agency_id": 2}, "Demande non comprise."),  # never an agency from outside
        ("help", {"x": 1}, "Demande non comprise."),
        ("paste_sale", {}, "Il manque le texte Amadeus."),
        ("paste_sale", {"text": 5}, "Demande non comprise."),
    ],
)
def test_a_bad_argument_is_a_french_message(name: str, raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ToolArgError, match=message.replace("(", r"\(").replace(")", r"\)")):
        TOOLS[name].check_args(raw)


def test_run_turns_a_bad_argument_into_an_invalid_reply(w: ChatWorld) -> None:
    answer = run_tool(w.ctx(w.manager), "ticket_status", {"doc": "12"})
    assert (answer.outcome, answer.tool, texts(answer)) == (
        r.INVALID,
        "ticket_status",
        [tools.BAD_DOC],
    )


# --- my tickets (list_sales) --------------------------------------------------------------------


def test_an_agent_sees_only_their_own_tickets_of_a_day(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.staff), "billets du 20/10")
    assert answer.data["count"] == 11
    signs = {line.href for line in answer.lines if line.href}
    assert all(h.startswith("/sales/") for h in signs)
    mine = {i["sale_id"] for i in answer.data["sales"]}
    other = route_message(w.ctx(w.staff2), "billets du 20/10")
    assert mine.isdisjoint({i["sale_id"] for i in other.data["sales"]})
    assert other.data["count"] == 11


def test_an_agent_cannot_ask_for_another_agents_signs(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.staff), "billets de 0006AC du 20/10")
    assert answer.data["count"] == 0 and texts(answer) == ["Aucune vente le 20 octobre 2026."]
    nobody = route_message(w.ctx(w.staff), "billets de 0009ER du 20/10")  # a sign nobody owns
    assert nobody.data["count"] == 0


def test_a_manager_and_an_accountant_see_every_sign(w: ChatWorld) -> None:
    for user in (w.manager, w.accountant):
        everything = route_message(w.ctx(user), "billets du 20/10")
        assert everything.data["count"] == 33 and texts(everything)[0].startswith("Ventes du")
        one_sign = route_message(w.ctx(user), "billets de 0004SV du 20/10")
        assert one_sign.data["count"] == 11


def test_the_list_says_what_is_still_missing_on_each_sale(w: ChatWorld) -> None:
    sc = w.scenario
    answer = route_message(w.ctx(w.manager), "billets de 0004SV du 20/10")
    by_doc = {i["docs"][0]: i["missing"] for i in answer.data["sales"]}
    assert by_doc[sc.complete] == []
    assert by_doc[sc.plain] == ["numéro du client"]
    different = route_message(w.ctx(w.manager), "billets de 0006AC du 20/10")
    flagged = next(i for i in different.data["sales"] if i["docs"][0] == sc.different)
    assert flagged["missing"] == ["numéro du client", "1 écart(s) à traiter"]
    line = next(x for x in answer.lines if sc.complete in x.text)
    assert line.text.endswith("complète") and line.href == f"/sales/{_sale_id(answer, sc.complete)}"


def _sale_id(answer: Reply, doc: str) -> int:
    return int(next(i["sale_id"] for i in answer.data["sales"] if doc in i["docs"]))


def test_my_tickets_of_today_for_an_agent_and_for_a_manager(w: ChatWorld) -> None:
    mine = w.sell_today(w.staff, "3940000101", "0004SV", ap="+22236123456")
    theirs = w.sell_today(w.staff2, "3940000102", "0006AC")
    boss = w.sell_today(w.manager, "3940000103", "0004SV")
    staff_view = route_message(w.ctx(w.staff), "mes billets d'aujourd'hui")
    assert [i["sale_id"] for i in staff_view.data["sales"]] == [mine.id, boss.id]
    assert texts(staff_view)[0] == "Vos ventes du 22 octobre 2026 : 2 vente(s)."
    assert staff_view.data["sales"][0]["missing"] == []
    manager_mine = route_message(w.ctx(w.manager), "mes billets d'aujourd'hui")
    assert [i["sale_id"] for i in manager_mine.data["sales"]] == [boss.id]  # entered by them
    manager_all = route_message(w.ctx(w.manager), "billets d'aujourd'hui")
    assert {i["sale_id"] for i in manager_all.data["sales"]} == {mine.id, theirs.id, boss.id}
    assert texts(manager_all)[0].startswith("Ventes du")


def test_a_voided_sale_leaves_the_list(w: ChatWorld) -> None:
    sale = w.sell_today(w.staff, "3940000104", "0004SV")
    sales.void_sale(w.db, w.manager, sale.id, "Erreur de saisie", now=NOW)
    w.db.commit()
    answer = route_message(w.ctx(w.staff), "mes billets")
    assert answer.data["count"] == 0
    assert texts(answer) == ["Aucune vente le 22 octobre 2026."]


def test_an_agent_without_a_sign_is_told_who_to_ask(w: ChatWorld) -> None:
    lonely = w._user("+22212370009", "staff")
    answer = route_message(w.ctx(lonely), "mes billets")
    assert texts(answer) == [sales.NO_SIGN] and answer.data["count"] == 0


def test_a_long_list_is_cut_with_a_link_to_the_screen(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.manager), "billets du 20/10")
    assert len(answer.lines) == 1 + tools.MAX_ITEMS + 1
    assert answer.lines[-1].text == "… et 13 autre(s)"
    assert answer.lines[-1].href == "/sales?date=2026-10-20"
    assert answer.data["count"] == 33 and len(answer.data["sales"]) == tools.MAX_ITEMS


def test_many_docs_in_a_sale_are_summarised(w: ChatWorld) -> None:
    sale = w.sell_today(w.manager, "3940000105", "0004SV")
    view = sales.get_sale_for(w.db, w.manager, sale.id)
    assert view is not None and tools._docs(view) == "3940000105"


# --- open flags and not-sent tickets ------------------------------------------------------------


def test_the_manager_sees_the_open_flags_per_agent(w: ChatWorld) -> None:
    for user in (w.manager, w.accountant):
        answer = route_message(w.ctx(user), "qu'est-ce qui bloque aujourd'hui ?")
        assert answer.data["count"] == 2 and answer.data["by_sign"] == {"0004SV": 1, "0006AC": 1}
        assert "0004SV : 1" in texts(answer) and "0006AC : 1" in texts(answer)
        assert "/flags?date=2026-10-20" in hrefs(answer)
        assert any(w.scenario.not_sent in t for t in texts(answer))


def test_an_agent_sees_only_the_flags_of_their_own_signs(w: ChatWorld) -> None:
    sc = w.scenario
    sv = route_message(w.ctx(w.staff), "qu'est-ce qui bloque")
    assert sv.data["count"] == 1 and sc.not_sent in sv.plain and sc.different not in sv.plain
    assert "0004SV : 1" not in texts(sv)  # no per-agent breakdown for an agent
    ac = route_message(w.ctx(w.staff2), "qu'est-ce qui bloque")
    assert ac.data["count"] == 1 and sc.different in ac.plain and sc.not_sent not in ac.plain


def test_nothing_blocks_for_a_user_with_no_flag_and_in_another_agency(w: ChatWorld) -> None:
    lonely = w._user("+22212370009", "staff")
    for user in (lonely, w.other_manager):
        answer = route_message(w.ctx(user), "flags")
        assert (
            texts(answer) == ["Rien ne bloque : aucun écart ouvert."] and answer.data["count"] == 0
        )


def test_answered_and_resolved_flags_do_not_block(w: ChatWorld) -> None:
    for flag in w.all_flags():
        w.set_status(flag.id, "answered")
    answer = route_message(w.ctx(w.manager), "flags")
    assert answer.data["count"] == 0


def test_the_flag_list_is_cut_after_a_few_rows(
    w: ChatWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_FLAG_ROWS", 1)
    answer = route_message(w.ctx(w.manager), "flags")
    assert texts(answer)[-1] == "… et 1 autre(s)."


def test_not_sent_tickets_are_the_agents_own(w: ChatWorld) -> None:
    sc = w.scenario
    manager = route_message(w.ctx(w.manager), "billets non envoyés")
    assert manager.data["docs"] == [sc.not_sent] and manager.data["count"] == 1
    flag_id = next(f.id for f in w.all_flags() if f.kind == "not_sent")
    assert hrefs(manager) == [f"/flags/{flag_id}/enter"]
    assert route_message(w.ctx(w.staff), "manquants").data["docs"] == [sc.not_sent]
    empty = route_message(w.ctx(w.staff2), "manquants")
    assert empty.data["count"] == 0 and texts(empty)[0].startswith("Aucun billet manquant")
    assert route_message(w.ctx(w.other_manager), "manquants").data["count"] == 0


def test_the_not_sent_list_is_cut_after_a_few_rows(
    w: ChatWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_ITEMS", 0)
    answer = route_message(w.ctx(w.manager), "manquants")
    assert texts(answer)[-1] == "… et 1 autre(s)." and answer.data["docs"] == []


# --- one ticket ---------------------------------------------------------------------------------


def test_the_status_of_a_ticket_of_my_own(w: ChatWorld) -> None:
    sc = w.scenario
    answer = route_message(w.ctx(w.staff), f"billet {sc.plain}")
    sale_id = answer.data["sale_id"]
    assert texts(answer)[:3] == [
        f"Billet {sc.plain} : vente enregistrée le 20 octobre 2026.",
        "Signe 0004SV · BRUGE.",
        "Il manque : numéro du client.",
    ]
    assert hrefs(answer) == [f"/sales/{sale_id}"]
    done = route_message(w.ctx(w.staff), f"billet {sc.complete}")
    assert "Rien ne manque." in texts(done)


def test_a_ticket_with_an_open_flag_shows_it_with_its_link(w: ChatWorld) -> None:
    sc = w.scenario
    answer = route_message(w.ctx(w.staff2), f"billet {sc.different}")
    assert "Écart ouvert : Différent" in texts(answer)
    assert "/flags?date=2026-10-20" in hrefs(answer)
    assert "Il manque : numéro du client, 1 écart(s) à traiter." in texts(answer)
    missing = route_message(w.ctx(w.staff), f"billet {sc.not_sent}")
    assert (
        texts(missing)[0] == f"Billet {sc.not_sent} : pas de vente enregistrée dans l'application."
    )
    assert missing.data["found"] is True and "sale_id" not in missing.data
    assert "Écart ouvert : Non envoyé" in texts(missing)


def test_a_ticket_number_with_the_airline_code_is_normalised(w: ChatWorld) -> None:
    doc = w.scenario.plain
    answer = route_message(w.ctx(w.staff), f"billet 495-{doc}")
    assert answer.data["doc"] == doc and answer.data["found"] is True


def test_another_agents_ticket_is_exactly_like_a_ticket_that_does_not_exist(w: ChatWorld) -> None:
    sc = w.scenario
    theirs = route_message(w.ctx(w.staff2), f"billet {sc.plain}")  # sold under 0004SV
    nothing = route_message(w.ctx(w.staff2), "billet 3940000000")
    assert theirs.lines[0].text == f"Aucun billet {sc.plain} trouvé parmi vos ventes."
    assert theirs.lines[0].text.replace(sc.plain, "X") == nothing.lines[0].text.replace(
        "3940000000", "X"
    )
    assert theirs.data == {"doc": sc.plain, "found": False}
    assert len(theirs.lines) == 1 and not hrefs(theirs)


def test_another_agencys_ticket_is_not_found(w: ChatWorld) -> None:
    for user in (w.manager, w.accountant, w.staff):
        answer = route_message(w.ctx(user), f"billet {OTHER_DOC}")
        assert answer.data == {"doc": OTHER_DOC, "found": False}
        assert hrefs(answer) == []
    mirror = route_message(w.ctx(w.other_manager), f"billet {w.scenario.plain}")
    assert mirror.data["found"] is False
    own = route_message(w.ctx(w.other_manager), f"billet {OTHER_DOC}")
    assert own.data["found"] is True


def test_the_other_agencys_lists_hold_only_its_own_data(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.other_manager), "billets du 20/10")
    assert answer.data["count"] == 1 and answer.data["sales"][0]["docs"] == [OTHER_DOC]


# --- the sheet of a day -------------------------------------------------------------------------


def test_the_sheet_of_an_open_day_is_the_draft_download(w: ChatWorld) -> None:
    for user in (w.manager, w.accountant):
        answer = route_message(w.ctx(user), "feuille du 20 octobre")
        assert answer.data == {"day": "2026-10-20", "available": True, "draft": True}
        assert "/days/2026-10-20/apres-preview.xlsx" in hrefs(answer)
        assert next(x for x in answer.lines if x.button).text == "Télécharger le brouillon"


def test_the_sheet_of_a_closed_day_is_the_stored_export(w: ChatWorld) -> None:
    w.close(D20)
    day = w.day(D20)
    nothing = route_message(w.ctx(w.manager), "feuille du 20/10")
    assert nothing.data["available"] is False and "sans feuille enregistrée" in nothing.plain
    w.db.add(
        DayExport(
            agency_id=w.agency_id,
            report_day_id=day.id,
            version=1,
            content=b"x",
            sha256="a" * 64,
            created_by_user_id=w.manager.id,
        )
    )
    w.db.commit()
    stored = route_message(w.ctx(w.manager), "feuille du 20/10")
    assert stored.data == {"day": "2026-10-20", "available": True, "draft": False}
    assert hrefs(stored) == ["/days/2026-10-20/apres.xlsx"]
    assert "version 1" in stored.plain


def test_no_sheet_without_a_report_or_for_the_future(w: ChatWorld) -> None:
    none = route_message(w.ctx(w.manager), "feuille du 19 octobre")
    assert none.data == {"day": D19.isoformat(), "available": False}
    assert "Aucun rapport Amadeus pour le 19 octobre 2026" in none.plain and hrefs(none) == []
    future = run_tool(w.ctx(w.manager), "day_sheet", {"day": date(2026, 11, 1)})
    assert texts(future) == ["Cette date est dans le futur."] and hrefs(future) == []
    today = route_message(w.ctx(w.manager), "feuille")  # today (22 Oct) has no report
    assert today.data["available"] is False


def test_the_sheet_of_another_agency_is_not_offered(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.other_manager), "feuille du 20 octobre")
    assert answer.data["available"] is False and hrefs(answer) == []


# --- a pasted screen ----------------------------------------------------------------------------


def test_a_pasted_screen_hands_off_to_the_paste_flow_and_stores_nothing(w: ChatWorld) -> None:
    text = (PNR_DIR / "three_adults_rm.txt").read_text(encoding="utf-8")
    before = _sales_count(w)
    answer = route_message(w.ctx(w.staff), text)
    assert answer.outcome == r.OK and answer.tool == "paste_sale"
    assert answer.data["sales"] >= 1 and answer.handoff_text == text
    assert hrefs(answer) == ["/sales/new"]
    assert "Rien n'est enregistré" in answer.plain
    assert text not in answer.stored() and "SYNTH" not in answer.stored()
    assert _sales_count(w) == before


def _sales_count(w: ChatWorld) -> int:
    from sqlalchemy import func, select

    from app.models import Sale

    return int(w.db.scalar(select(func.count()).select_from(Sale)) or 0)


def test_a_paste_that_reads_nothing_says_so(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.staff), "RP/NKC262213/NKC262213\n")
    assert answer.handoff_text is None and answer.data == {"sales": 0}
    assert texts(answer)[0].startswith("Aucune vente lisible")


def test_a_paste_that_is_too_big_is_refused(w: ChatWorld) -> None:
    answer = run_tool(w.ctx(w.staff), "paste_sale", {"text": "RP/X\n" + "A" * 20_000})
    assert answer.outcome == r.INVALID and "Texte trop long" in answer.plain


# --- cancelling ---------------------------------------------------------------------------------


def test_cancel_only_links_to_the_void_screen(w: ChatWorld) -> None:
    sc = w.scenario
    for user in (w.manager, w.accountant):
        answer = route_message(w.ctx(user), f"annule {sc.plain}")
        sale_id = answer.data["sale_id"]
        assert answer.data["can_void"] is True
        assert hrefs(answer) == [f"/sales/{sale_id}/void"]
        assert "rien n'est annulé depuis la discussion" in answer.plain
    from app.models import SaleVersion

    versions = w.db.query(SaleVersion).filter(SaleVersion.sale_id == sale_id).count()
    assert versions == 1  # nothing was voided


def test_an_agent_may_void_only_their_own_sale_of_today(w: ChatWorld) -> None:
    sc = w.scenario
    old = route_message(w.ctx(w.staff), f"annule {sc.plain}")  # their own, but of 20 October
    assert old.data["can_void"] is False and hrefs(old) == []
    assert "seule une vente du jour peut être annulée par vous" in old.plain
    today = w.sell_today(w.staff, "3940000106", "0004SV")
    new = route_message(w.ctx(w.staff), "annule 3940000106")
    assert new.data["can_void"] is True and hrefs(new) == [f"/sales/{today.id}/void"]
    foreign = route_message(w.ctx(w.staff2), "annule 3940000106")  # another agent's sale
    assert foreign.data == {"doc": "3940000106", "found": False} and hrefs(foreign) == []
    elsewhere = route_message(w.ctx(w.manager), f"annule {OTHER_DOC}")  # another agency's
    assert elsewhere.data == {"doc": OTHER_DOC, "found": False}


def test_a_closed_day_cannot_be_voided_from_the_chat_either(w: ChatWorld) -> None:
    w.close(D20)
    answer = route_message(w.ctx(w.manager), f"annule {w.scenario.plain}")
    assert answer.data["can_void"] is False and "la journée est clôturée" in answer.plain
    assert hrefs(answer) == []


def test_cancel_without_a_number_asks_for_it(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.manager), "annule")
    assert (answer.outcome, texts(answer)) == (
        r.INVALID,
        ["Il manque le numéro du billet (10 chiffres)."],
    )


# --- help ---------------------------------------------------------------------------------------


def test_help_lists_what_the_role_can_do(w: ChatWorld) -> None:
    manager = route_message(w.ctx(w.manager), "aide")
    staff = route_message(w.ctx(w.staff), "aide")
    assert manager.data["tools"] == [
        "list_sales",
        "open_flags",
        "not_sent_tickets",
        "ticket_status",
    ] + [
        "day_sheet",
        "paste_sale",
        "void_link",
        "help",
    ]
    assert "day_sheet" not in staff.data["tools"]
    assert any("feuille" in t for t in texts(manager)) and not any(
        "feuille" in t for t in texts(staff)
    )
    assert texts(staff)[0] == "Voici ce que je sais faire :"


def test_a_reply_never_carries_unsafe_links(w: ChatWorld) -> None:
    for text in (
        "billets du 20/10",
        "flags",
        "manquants",
        f"billet {w.scenario.plain}",
        "feuille du 20/10",
    ):
        for line in route_message(w.ctx(w.manager), text).lines:
            assert line.href is None or r.safe_href(line.href)


def test_a_handler_never_writes(w: ChatWorld) -> None:
    from sqlalchemy import func, select

    from app.models import AuditEvent, ChatMessage, ComparisonFlag, Sale

    def counts() -> tuple[int, ...]:
        return tuple(
            int(w.db.scalar(select(func.count()).select_from(m)) or 0)
            for m in (Sale, ComparisonFlag, AuditEvent, ChatMessage)
        )

    before = counts()
    for text in ("billets du 20/10", "flags", "manquants", f"billet {w.scenario.plain}", "aide"):
        route_message(w.ctx(w.manager), text)
    route_message(w.ctx(w.manager), "feuille du 20/10")
    route_message(w.ctx(w.manager), f"annule {w.scenario.plain}")
    assert counts() == before


# --- shared rules with the sales service --------------------------------------------------------


def test_void_blocker_is_the_one_rule_for_who_may_void_when(w: ChatWorld) -> None:
    today, old = D20, date(2026, 10, 1)
    assert sales.void_blocker(w.manager, old, "open", today) is None
    assert sales.void_blocker(w.manager, old, "closed", today) == sales.VOID_DAY_CLOSED
    assert sales.void_blocker(w.accountant, today, "closed", today) == sales.VOID_DAY_CLOSED
    assert sales.void_blocker(w.staff, today, "open", today) is None
    assert sales.void_blocker(w.staff, old, "open", today) == sales.VOID_NOT_TODAY
    assert sales.void_blocker(w.staff, today, "closed", today) == sales.VOID_DAY_CLOSED
    # staff on an old closed day: the same order as the sales service (its staff-day rule first)
    assert sales.void_blocker(w.staff, old, "closed", today) == sales.VOID_NOT_TODAY
    assert sales.void_blocker(w.staff, today, None, today) is None  # a day with no row is open


def test_void_link_follows_void_blocker_and_the_void_service(w: ChatWorld) -> None:
    sc = w.scenario
    old = route_message(w.ctx(w.staff), f"annule {sc.plain}")  # 20 October, today is the 22nd
    assert old.data["can_void"] is False
    with pytest.raises(sales.SaleForbidden):
        sales.void_sale(w.db, w.staff, old.data["sale_id"], "motif de test", now=NOW)
    w.close(D20)
    closed = route_message(w.ctx(w.manager), f"annule {sc.plain}")
    assert closed.data["can_void"] is False
    with pytest.raises(days.DayClosed):
        sales.void_sale(w.db, w.manager, closed.data["sale_id"], "motif de test", now=NOW)


def test_flags_for_me_can_be_narrowed_to_one_ticket_in_the_query(w: ChatWorld) -> None:
    from app import flags

    sc = w.scenario
    everything = flags.flags_for_me(w.db, w.manager)
    one = flags.flags_for_me(w.db, w.manager, doc=sc.not_sent)
    assert one and {f.doc_number for f in one} == {sc.not_sent}
    assert {f.id for f in one} == {f.id for f in everything if f.doc_number == sc.not_sent}
    assert flags.flags_for_me(w.db, w.manager, doc="3940000000") == []
    assert flags.flags_for_me(w.db, w.staff2, doc=sc.not_sent) == []  # not their sign


# --- phase E part 2: what a model may send ----------------------------------------------------


def test_the_schema_says_how_to_write_a_date_a_ticket_and_a_sign() -> None:
    props = TOOLS["list_sales"].input_schema()["properties"]
    assert props["day"]["format"] == "date" and "AAAA-MM-JJ" in props["day"]["description"]
    assert "2026-10-19" in props["day"]["description"]
    assert "0004SV" in props["sign"]["description"]
    doc = TOOLS["ticket_status"].input_schema()["properties"]["doc"]
    assert "10 chiffres" in doc["description"]


@pytest.mark.parametrize("raw", [None, "list_sales", ["day"], 12, ("a", 1)])
def test_arguments_that_are_not_an_object_are_refused_not_raised(w: ChatWorld, raw: Any) -> None:
    answer = run_tool(w.ctx(w.manager), "list_sales", raw)
    assert (answer.outcome, texts(answer)) == (r.INVALID, ["Demande non comprise."])


def test_an_oversized_ticket_or_sign_is_refused_before_it_is_read(w: ChatWorld) -> None:
    ctx = w.ctx(w.manager)
    huge_doc = "3940299439" + " x" * 5000
    assert run_tool(ctx, "ticket_status", {"doc": huge_doc}).outcome == r.INVALID
    assert run_tool(ctx, "ticket_status", {"doc": "3940299439"}).outcome == r.OK
    assert run_tool(ctx, "list_sales", {"sign": "0004SV" + "A" * 40}).outcome == r.INVALID
    assert run_tool(ctx, "list_sales", {"sign": "0004SV"}).outcome == r.OK


def test_null_arguments_count_as_missing_the_all_required_form(w: ChatWorld) -> None:
    ctx = w.ctx(w.manager)
    answer = run_tool(ctx, "list_sales", {"day": None, "sign": None, "mine": None})
    assert answer.outcome == r.OK
    assert run_tool(ctx, "ticket_status", {"doc": None}).outcome == r.INVALID
