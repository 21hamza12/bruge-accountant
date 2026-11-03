"""Agents never see a total of cost or of P.B (owner's decision, 30 Oct 2026, docs/design/02 #26).

In the owner's words: "i dont want the agent to see cout total or PB total if they can see it they
can calculate the profit". Staff (agents) get no « Coût total », no « P.B total » and no summed
amount in any page, fragment or chat answer; they keep the per-sale Coût and P.B columns and the
amounts of a single sale. The accountant and the manager keep every total (the profit stays the
manager's, ``permissions.can_see_profit``). Synthetic data only.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import permissions
from app.chat import tools
from app.chat.reply import Reply
from app.chat.tools import run_tool
from app.models import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_STAFF, User
from tests.chat_support import ChatWorld
from tests.page_support import Env
from tests.test_sale_pages import D1, D2, SIGN, TODAY, accountant, enter, text_of, tk
from tests.web_support import WebDb

pytest_plugins = ["tests.chat_fixtures"]  # the fixtures ``w`` and ``chat_template``

NNBSP = " "  # the narrow no-break space the pages put between thousands
# Two sales of the agent, the same day: per-sale amounts and their sums, as the pages print them.
COST_1, COST_2 = f"21{NNBSP}368,40", f"1{NNBSP}000,10"
PB_1, PB_2 = f"24{NNBSP}000", f"1{NNBSP}500"
COST_SUM, PB_SUM = f"22{NNBSP}368,50", f"25{NNBSP}500"
PROFIT_SUM = f"3{NNBSP}131,50"  # 25500 - 22368.50
TOTAL_WORDS = ("Coût total", "P.B total")
ROLES = ("manager", "accountant", "staff")


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    return e


def clients(env: Env) -> dict[str, TestClient]:
    return {"manager": env.manager_a(), "accountant": accountant(env), "staff": env.staff_a()}


def two_sales(c: TestClient) -> None:
    enter(c, tk(D1, "21368.40"), staff_sign=SIGN, day=TODAY, pb="24000")
    enter(
        c,
        tk(D2, "1000.10", pax_name="SYNTH02/PAX", rloc="DEF456"),
        staff_sign=SIGN,
        day=TODAY,
        pb="1500",
    )


def assert_no_totals(html: str) -> None:
    text = text_of(html)
    for word in TOTAL_WORDS:
        assert word not in text, word
    for summed in (COST_SUM, PB_SUM, "22368", "25500", "22 368", "25 500"):
        assert summed not in text, summed


def assert_totals(html: str) -> None:
    text = text_of(html)
    assert COST_SUM in text and PB_SUM in text


def assert_rows(html: str) -> None:
    """The per-sale Coût and P.B of both sales are there."""
    text = text_of(html)
    for amount in (COST_1, COST_2, PB_1, PB_2):
        assert amount in text, amount


# --- the permission -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"), [(ROLE_MANAGER, True), (ROLE_ACCOUNTANT, True), (ROLE_STAFF, False)]
)
def test_only_manager_and_accountant_may_see_the_totals(role: str, expected: bool) -> None:
    assert permissions.can_see_totals(User(agency_id=1, phone="+1", role=role)) is expected


# --- « Mes ventes » -----------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_the_sales_list_totals_are_hidden_from_staff(env: Env, role: str) -> None:
    c = clients(env)
    two_sales(c["staff"])
    r = c[role].get(f"/sales?date={TODAY}")
    assert r.status_code == 200
    assert_rows(r.text)
    text = text_of(r.text)
    assert "Ventes" in text and "2 billets" in text  # counts are not money: they stay
    if role == "staff":
        assert_no_totals(r.text)
    else:
        assert_totals(r.text)
        for word in TOTAL_WORDS:
            assert word in text
    assert (PROFIT_SUM in text) is (role == "manager")


# --- « Journées » -------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_the_day_list_has_no_cost_or_pb_column_for_staff(env: Env, role: str) -> None:
    c = clients(env)
    two_sales(c["staff"])
    r = c[role].get("/days")
    assert r.status_code == 200
    text = text_of(r.text)
    if role == "staff":
        assert_no_totals(r.text)
        assert '<th scope="col" class="col-end">Coût</th>' not in r.text
        assert '<th scope="col" class="col-end">P.B</th>' not in r.text
        assert "18 octobre 2026" in text  # the day, its counts and its status stay
    else:
        assert_totals(r.text)
    assert (PROFIT_SUM in text) is (role == "manager")


@pytest.mark.parametrize("role", ROLES)
def test_the_day_page_totals_are_hidden_from_staff(env: Env, role: str) -> None:
    c = clients(env)
    two_sales(c["staff"])
    r = c[role].get(f"/days/{TODAY}")
    assert r.status_code == 200
    text = text_of(r.text)
    # the TJQ AVANT lines keep each ticket's cost and each sale's P.B
    assert_rows(r.text)
    assert "Ventes" in text and "Billets" in text
    if role == "staff":
        assert_no_totals(r.text)
    else:
        assert_totals(r.text)
        assert text.count(COST_SUM) == 2  # the day's total and the sign's row
        for word in TOTAL_WORDS:
            assert word in text
    assert (PROFIT_SUM in text) is (role == "manager")


@pytest.mark.parametrize("role", ["manager", "accountant"])
def test_the_close_page_keeps_the_pb_total_for_those_who_close(env: Env, role: str) -> None:
    c = clients(env)
    two_sales(c["staff"])
    r = c[role].get(f"/days/{TODAY}/close")
    assert r.status_code == 200
    assert PB_SUM in text_of(r.text)


def test_staff_cannot_open_the_close_page(env: Env) -> None:
    c = clients(env)
    two_sales(c["staff"])
    r = c["staff"].get(f"/days/{TODAY}/close")
    assert r.status_code == 403
    assert_no_totals(r.text)


def test_staff_cannot_download_the_tjq_apres(env: Env) -> None:
    c = clients(env)
    two_sales(c["staff"])
    for path in (f"/days/{TODAY}/apres.xlsx", f"/days/{TODAY}/apres-preview.xlsx"):
        r = c["staff"].get(path)
        assert r.status_code == 403, path
        assert r.content[:2] != b"PK"


# --- a single sale: staff keep its amounts ------------------------------------------------------


def test_staff_still_see_the_cost_and_pb_of_their_own_sale(env: Env) -> None:
    c = clients(env)
    sale_id = enter(c["staff"], tk(D1, "21368.40"), staff_sign=SIGN, day=TODAY, pb="24000")
    text = text_of(c["staff"].get(f"/sales/{sale_id}").text)
    assert COST_1 in text and PB_1 in text
    assert "Coût total" not in text and "P.B total" not in text
    assert "Profit" not in text


# --- the chat -----------------------------------------------------------------------------------


def _blob(answer: Reply) -> str:
    """Everything a chat answer carries: its lines (shown, and sent to the model) and its data."""
    lines = " ".join(f"{line.text} {line.href or ''}" for line in answer.lines)
    return f"{lines} {json.dumps(answer.data, default=str, ensure_ascii=False)}".lower()


def test_no_chat_answer_gives_staff_a_total(w: ChatWorld) -> None:
    """No lookup open to an agent gives a total of cost or of P.B, neither in what is shown nor
    in what reaches the model (the tool's lines and data)."""
    s = w.scenario
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_sales", {"day": "2026-10-20"}),
        ("list_sales", {}),
        ("ticket_status", {"doc": s.complete}),
        ("ticket_status", {"doc": s.different}),
        ("open_flags", {}),
        ("not_sent_tickets", {}),
        ("day_sheet", {"day": "2026-10-20"}),
        ("void_link", {"doc": s.plain}),
        ("help", {}),
    ]
    for name, args in calls:
        blob = _blob(run_tool(w.ctx(w.staff), name, args))
        for word in ("coût total", "cout total", "p.b total", "total", "revenue", "chiffre"):
            assert word not in blob, (name, word)
    names = {t.name for t in tools.tools_for(w.staff)}
    assert "day_sheet" not in names  # the TJQ APRES link: manager and accountant only
    assert not names & {"partner_balance", "partners_owing", "partner_statement"}
