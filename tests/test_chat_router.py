"""app/chat/router.py: from a French message to a tool call, and the seam for the AI of part 2."""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import pytest

from app.chat import reply as r
from app.chat.reply import Reply
from app.chat.router import (
    MAX_SCAN,
    Intent,
    looks_like_amadeus,
    looks_like_fragment,
    parse_message,
    polite_unknown,
    route_message,
)
from app.chat.tools import BAD_DOC, TOOLS, ChatContext
from tests.chat_support import ChatWorld
from tests.flag_support import NOW

pytest_plugins = ["tests.chat_fixtures"]  # the fixtures ``w`` and ``chat_template``
TODAY = date(2026, 10, 22)
DOC = "3940299439"
PNR = (
    "RP/NKC262213/NKC262213  SV/SU  21OCT26/0000Z  ABC123\n"
    "  1.SYNTHZ/ONE\n"
    "  2  AF 123 Y 20OCT 1 CDGJFK HK1\n"
)


def intent(text: str) -> Intent:
    return parse_message(text, TODAY)


@pytest.mark.parametrize(
    "text",
    [
        "mes billets",
        "mes billets d'aujourd'hui",
        "Mes Billets D'AUJOURD'HUI",
        "mes billets d’aujourd’hui",
        "MES BILLETS AUJOURDHUI",
        "donne-moi mes billets s'il te plaît",
        "mes tickets",
        "mes ventes du jour",
        "mon billet du jour",
    ],
)
def test_my_tickets(text: str) -> None:
    found = intent(text)
    assert found.tool == "list_sales" and not found.error
    assert found.args.get("mine") is True
    assert "day" not in found.args or found.args["day"] == TODAY


def test_tickets_of_a_day_and_of_a_sign() -> None:
    assert intent("billets du 19 septembre").args == {"day": date(2026, 9, 19)}
    assert intent("billets du 19/09").args == {"day": date(2026, 9, 19)}
    assert intent("billets d'hier").args == {"day": date(2026, 10, 21)}
    assert intent("billets de 0004SV").args == {"sign": "0004SV"}
    assert intent("les billets de 0004sv du 19 sept").args == {
        "sign": "0004SV",
        "day": date(2026, 9, 19),
    }
    assert intent("mes ventes du 19 sept").args == {"day": date(2026, 9, 19), "mine": True}


@pytest.mark.parametrize(
    "text",
    [
        "qu'est-ce qui bloque",
        "Qu'est-ce qui bloque aujourd'hui ?",
        "qu’est-ce qui bloque aujourd’hui",
        "ça bloque où ?",
        "bloquants",
        "flags",
        "mes flags",
        "les écarts",
        "il y a des anomalies ?",
    ],
)
def test_what_blocks(text: str) -> None:
    assert intent(text) == Intent("open_flags")


@pytest.mark.parametrize(
    "text",
    [
        "billets non envoyés",
        "billets non envoyes",
        "Les billets NON ENVOYÉS d'hier",
        "quels billets ne sont pas envoyés ?",
        "manquants",
        "les billets manquants",
        "il manque des billets : lesquels manquants ?",
    ],
)
def test_not_sent(text: str) -> None:
    assert intent(text) == Intent("not_sent_tickets")


@pytest.mark.parametrize(
    "text",
    [
        f"billet {DOC}",
        f"Billet n° {DOC} ?",
        DOC,
        f"statut du ticket {DOC}",
        f"billet 495-{DOC}",
        f"billet 495{DOC}",
        f"où en est le billet {DOC} stp",
    ],
)
def test_a_ticket_number(text: str) -> None:
    assert intent(text) == Intent("ticket_status", {"doc": DOC})


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("feuille du 19 septembre", date(2026, 9, 19)),
        ("feuille d'hier", date(2026, 10, 21)),
        ("la feuille 19/09", date(2026, 9, 19)),
        ("feuille du 19 sept", date(2026, 9, 19)),
        ("Feuille du 2026-09-19", date(2026, 9, 19)),
        ("export apres du 19.09", date(2026, 9, 19)),
        ("TJQ APRES du 19 septembre", date(2026, 9, 19)),
        ("export du 19 sept", date(2026, 9, 19)),
        ("feuille", None),
        ("feuille d'aujourd'hui", TODAY),
    ],
)
def test_the_sheet_of_a_day(text: str, day: date | None) -> None:
    found = intent(text)
    assert found.tool == "day_sheet" and not found.error
    assert found.args == ({"day": day} if day else {})


@pytest.mark.parametrize(
    "text",
    [
        "aide",
        "Aide",
        "AIDEZ-MOI",
        "help",
        "menu",
        "j'ai besoin d'aide",
        "?",
        "que sais-tu faire",
        "que sais tu faire ?",
    ],
)
def test_help(text: str) -> None:
    assert intent(text) == Intent("help")


@pytest.mark.parametrize(
    "text",
    [
        "menu du jour ventes",
        "quelle commande dois-je taper pour les billets",
        "options de paiement du client",
        "commande",
        "options",
    ],
)
def test_help_is_for_a_short_message_and_not_a_lone_commande_or_options(text: str) -> None:
    assert intent(text).tool != "help"


@pytest.mark.parametrize(
    ("text", "tool"),
    [
        (f"pourquoi le billet {DOC} est dans les écarts", "ticket_status"),
        (f"aide moi pour le billet {DOC}", "ticket_status"),
        (f"je cherche le billet {DOC} annulé ?", "ticket_status"),
        (f"ticket {DOC} vendu après-midi", "ticket_status"),
        (f"le billet {DOC} est-il non envoyé ?", "ticket_status"),
        (f"billet {DOC} pour la feuille", "ticket_status"),
        ("combien de billets non envoyés après 18h", "not_sent_tickets"),
        ("menu du jour ventes", "list_sales"),
        ("billets annulés hier", "list_sales"),
    ],
)
def test_a_ticket_number_decides_before_the_words_around_it(text: str, tool: str) -> None:
    found = intent(text)
    assert found.tool == tool and not found.error
    if tool == "ticket_status":
        assert found.args == {"doc": DOC}


@pytest.mark.parametrize("text", ["feuille", "tjq", "export du 19/09", "excel d'hier"])
def test_the_sheet_needs_its_own_words(text: str) -> None:
    assert intent(text).tool == "day_sheet"


@pytest.mark.parametrize("text", ["ventes après 18h", "billets après midi", "après"])
def test_a_lone_apres_is_not_the_sheet(text: str) -> None:
    assert intent(text).tool != "day_sheet"


@pytest.mark.parametrize(
    "text", ["394029943", "billet 12345678", "billet 39402994390", "123456789012345 stp"]
)
def test_a_long_number_that_is_not_a_ticket_is_refused_not_a_list(text: str) -> None:
    found = intent(text)
    assert found.tool == "ticket_status" and found.error == BAD_DOC


def test_a_long_number_that_is_an_amount_is_not_a_ticket() -> None:
    assert intent("BADI a payé 600000 cash").tool is None
    assert intent("montant 1250000").tool is None


@pytest.mark.parametrize("text", ["billets 12345", "billets du 19/09/2026", "billets 2026-09-19"])
def test_short_numbers_and_dates_are_not_bad_tickets(text: str) -> None:
    assert not intent(text).error


@pytest.mark.parametrize(
    "text",
    [
        "billets du 2026/13/19",
        "billets de lundi",
        "feuille de vendredi",
        "mes billets 19/09/2026/5",
        "billets de demain",
    ],
)
def test_a_date_that_cannot_be_read_is_never_today(text: str) -> None:
    found = intent(text)
    assert found.error.startswith("Je n'ai pas compris la date.")
    assert "19/09" in found.error and "hier" in found.error
    assert found.tool in ("list_sales", "day_sheet")


def test_a_year_first_date_is_read() -> None:
    assert intent("billets du 2026/09/19").args == {"day": date(2026, 9, 19)}


def test_an_amount_with_k_is_not_a_day() -> None:
    assert intent("ventes 4.5k") == Intent("list_sales")


@pytest.mark.parametrize(
    "text",
    [
        f"annule {DOC}",
        f"Annuler le billet {DOC}",
        f"supprime {DOC} stp",
        f"ANNULE 495-{DOC}",
        f"je veux supprimer le billet {DOC}",
    ],
)
def test_cancel_names_the_ticket(text: str) -> None:
    assert intent(text) == Intent("void_link", {"doc": DOC})


@pytest.mark.parametrize("text", [f"annulation {DOC}", f"billet {DOC} annulé ?"])
def test_a_noun_or_a_participle_is_a_question_not_a_cancel(text: str) -> None:
    found = intent(text)
    assert found.tool in ("ticket_status", "void_link")
    if "annul" in text:
        assert found == Intent("ticket_status", {"doc": DOC})


def test_cancel_without_a_ticket_still_asks_for_one() -> None:
    assert intent("annule") == Intent("void_link", {})
    assert intent("je veux annuler une vente") == Intent("void_link", {})


def test_the_more_specific_request_wins() -> None:
    assert intent(f"annule le billet {DOC}").tool == "void_link"  # not a status
    assert intent("billets non envoyés").tool == "not_sent_tickets"  # not a list of sales
    assert intent("billets bloqués").tool == "open_flags"
    assert intent(f"aide billet {DOC}").tool == "ticket_status"


@pytest.mark.parametrize(
    "text",
    ["", "   ", "bonjour", "merci beaucoup", "quel temps fait-il ?", "BADI a payé 60 000"],
)
def test_nothing_matched(text: str) -> None:
    assert intent(text) == Intent(None)


@pytest.mark.parametrize("text", ["feuille du 31/02", "billets du 32/01", "feuille du 30 février"])
def test_a_date_that_does_not_exist_is_an_error_not_a_guess(text: str) -> None:
    found = intent(text)
    assert found.error.startswith("Je n'ai pas compris la date.")
    assert found.tool in ("day_sheet", "list_sales")


def test_a_pasted_screen_is_handed_to_the_paste_flow() -> None:
    assert intent(PNR) == Intent("paste_sale", {"text": PNR})
    # even if the pasted text also contains words that mean something else
    assert intent(PNR + "\n  3 RM annule billets").tool == "paste_sale"


def test_how_a_pasted_screen_is_recognised() -> None:
    assert looks_like_amadeus(PNR)
    assert looks_like_amadeus("RP/NKC262213/NKC262213\n")
    assert looks_like_amadeus("  1.DOE/JOHN\n  2  FA PAX 235-3940281465/ETKL/MRU1000\n")
    assert looks_like_amadeus(" 8 FA PAX 235-3940281465/ETKL\n 9 FP CASH\n")
    assert not looks_like_amadeus("1. mes billets")
    assert not looks_like_amadeus("RP/ dans un texte, pas en début de ligne")
    assert not looks_like_amadeus("billet 3940299439")
    assert not looks_like_amadeus("")


def test_parsing_never_raises_on_odd_text() -> None:
    for text in ("\x00\x00", "ÿ" * 3000, "💥 billets 💥", "‮3940299439", "a" * 100_000, "9" * 5000):
        assert isinstance(intent(text), Intent)


# --- route_message and its seam -------------------------------------------------------------------


def test_a_message_is_run_through_the_registry_with_the_users_rights(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.manager), "aide")
    assert (answer.outcome, answer.tool) == (r.OK, "help")


def test_a_message_nobody_understands_goes_to_the_unknown_seam(w: ChatWorld) -> None:
    seen: list[tuple[ChatContext, str]] = []

    def ai(ctx: ChatContext, text: str) -> Reply:
        seen.append((ctx, text))
        return r.reply("ai", r.say("réponse de l'IA"))

    ctx = w.ctx(w.staff)
    answer = route_message(ctx, "bonjour", unknown=ai)
    assert answer.tool == "ai" and answer.lines[0].text == "réponse de l'IA"
    assert seen == [(ctx, "bonjour")]
    # a message the plain code understands never reaches the AI
    route_message(ctx, "aide", unknown=ai)
    assert len(seen) == 1


def test_the_default_unknown_answer_is_polite_and_lists_the_help(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.staff), "Zorglub <script>alert(1)</script>")
    assert answer.outcome == r.UNKNOWN and answer.tool is None
    assert answer.lines[0].text == "Je n'ai pas compris votre demande."
    assert answer.lines[1].text == "Voici ce que je sais faire :"
    assert "Zorglub" not in answer.plain and "script" not in answer.plain  # nothing is echoed
    assert answer == polite_unknown(w.ctx(w.staff), "autre texte")  # and it does not depend on it


def test_a_bad_date_does_not_run_the_tool(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.manager), "feuille du 31/02")
    assert answer.outcome == r.INVALID
    assert answer.lines[0].text.startswith("Je n'ai pas compris la date.")


def test_every_tool_the_router_can_name_is_registered() -> None:
    names = {"list_sales", "open_flags", "not_sent_tickets", "ticket_status", "day_sheet"}
    names |= {"paste_sale", "void_link", "help"}
    assert names == set(TOOLS)
    for text in ("mes billets", "flags", "manquants", f"billet {DOC}", "feuille", "aide", "annule"):
        assert intent(text).tool in TOOLS


def test_the_clock_of_the_context_decides_today(w: ChatWorld) -> None:
    answer = route_message(w.ctx(w.manager, NOW), "billets d'hier")
    assert answer.data["day"] == "2026-10-21"
    args: dict[str, Any] = dict(answer.data)
    assert args["count"] == 0


@pytest.mark.parametrize("filler", ["\n", " \n", "\r\n", "\t\n", " " * 50 + "\n"])
def test_a_text_of_blank_lines_is_read_in_linear_time(filler: str) -> None:
    r"""``^\s*RP/`` re-scanned every later blank line from each line start: quadratic."""
    started = time.perf_counter()
    text = filler * (100_000 // len(filler)) + "x"
    assert not looks_like_amadeus(text)
    assert not looks_like_fragment(text)
    assert parse_message(text, TODAY) == Intent(None)
    assert time.perf_counter() - started < 0.5


def test_only_the_head_of_a_huge_text_is_scanned() -> None:
    assert not looks_like_amadeus("x" * MAX_SCAN + "\nRP/ABC/DEF\n")
    assert looks_like_amadeus("RP/ABC/DEF\n" + "x" * 5 * MAX_SCAN)


def test_a_header_may_be_indented_but_not_after_blank_lines_only() -> None:
    assert looks_like_amadeus("   RP/NKC262213/NKC262213")
    assert looks_like_amadeus("\n\n  \tRP/NKC262213")
