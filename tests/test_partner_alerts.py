"""Partner accounts in the alerts and the chat (phase F, slice F4), on SQLite: the due-soon and
overdue levels on the day boundaries, who sees them, agency isolation, and the three read-only
chat lookups (``partner_balance``, ``partners_owing``, ``partner_statement``) for every role,
with an ambiguous name, an alias and another agency's partner. Made-up names only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app import partner_aliases, partner_book
from app.chat import ai_prompt, tools
from app.chat import reply as r
from app.chat.router import route_message
from app.chat.tools import ChatContext, run_tool
from app.models import PartnerEntry, User
from app.partner_alerts import partner_alerts
from tests.test_partner_book import OCT_3, World, partner_sale, ticket

DUE = date(2026, 10, 30)  # the 1-15 October period is due 15 days after its end
PARTNER_TOOLS = ("partner_balance", "partners_owing", "partner_statement")


@pytest.fixture
def w(session: Session) -> World:
    """ "Partenaire Test" owes the 1-15 October period (a sale of 21 368 + fee 300, closed and
    unpaid); "Partenaire Deux" owes nothing; the other agency has its own "Partenaire Test"."""
    world = World(session)
    world.sell(partner_sale(world.partner, ticket("3940000001")), OCT_3)
    world.set_days("closed")
    world.close()
    return world


def at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)


def ctx(w: World, user: User, day: date = DUE) -> ChatContext:
    return ChatContext(w.db, user, at(day))


def texts(answer: r.Reply) -> list[str]:
    return [line.text for line in answer.lines]


# --- the levels on the day boundaries -----------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "level", "late"),
    [
        (DUE - timedelta(days=4), None, 0),  # 26 October: not yet
        (DUE - timedelta(days=3), "due_soon", 0),  # 27 October: three days before
        (DUE, "due_soon", 0),  # the due day itself
        (DUE + timedelta(days=1), "overdue", 1),  # the day after
        (DUE + timedelta(days=10), "overdue", 10),
    ],
)
def test_the_alert_level_on_the_day_boundaries(
    w: World, today: date, level: str | None, late: int
) -> None:
    alerts = partner_alerts(w.db, w.manager, today)
    assert alerts is not None
    found = [a for a in alerts.owing if a.partner_id == w.partner.id]
    if level is None:
        assert found == [] and not alerts.any
        return
    (alert,) = found
    assert (alert.level, alert.days_late, alert.due) == (level, late, DUE)
    assert alert.amount == Decimal("21668.00")
    assert alert.name == "Partenaire Test"
    assert (alert in alerts.overdue) == (level == "overdue")


def test_a_paid_period_raises_no_alert(w: World) -> None:
    w.pay("21668", paid_on="2026-10-20")
    alerts = partner_alerts(w.db, w.manager, DUE + timedelta(days=5))
    assert alerts is not None and alerts.owing == ()


def test_overdue_comes_first_then_due_soon(w: World) -> None:
    """« Partenaire Deux » closes 16-31 October late; on 15 November the 1-15 period of
    « Partenaire Test » is overdue and the other one is due soon."""
    w.sell(partner_sale(w.second, ticket("3940000002", "1000")), date(2026, 10, 20))
    w.set_days("closed", date(2026, 10, 16), date(2026, 10, 31))
    partner_book.close_period(
        w.db, w.accountant, w.second.id, date(2026, 10, 16),
        request_key=partner_book.new_request_key(), now=at(date(2026, 11, 3)),
    )  # fmt: skip
    w.db.commit()
    alerts = partner_alerts(w.db, w.manager, date(2026, 11, 13))
    assert alerts is not None
    assert [a.partner_id for a in alerts.owing] == [w.partner.id, w.second.id]
    assert [a.level for a in alerts.owing] == ["overdue", "due_soon"]


def test_staff_get_no_alerts(w: World) -> None:
    assert partner_alerts(w.db, w.staff, DUE + timedelta(days=1)) is None


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_manager_and_accountant_get_them(w: World, who: str) -> None:
    alerts = partner_alerts(w.db, getattr(w, who), DUE + timedelta(days=1))
    assert alerts is not None and len(alerts.overdue) == 1


def test_another_agency_sees_none_of_them(w: World) -> None:
    alerts = partner_alerts(w.db, w.other_manager, DUE + timedelta(days=1))
    assert alerts is not None and not alerts.any


def test_pending_amadeus_refunds_are_listed_until_decided(w: World) -> None:
    entry = PartnerEntry(
        agency_id=w.agency_id,
        partner_id=w.partner.id,
        entry_date=date(2026, 10, 20),
        kind="RFND",
        amount=Decimal("-2505.00"),
        reason="remboursement vu dans le rapport",
        source="amadeus",
        doc_number="3940000001",
        created_by_user_id=w.manager.id,
    )
    w.db.add(entry)
    w.db.commit()
    alerts = partner_alerts(w.db, w.accountant, DUE)
    assert alerts is not None
    assert [(x.entry_id, x.amount, x.doc_number) for x in alerts.refunds] == [
        (entry.id, Decimal("-2505.00"), "3940000001")
    ]
    w.decide(entry, "confirm", w.accountant)
    alerts = partner_alerts(w.db, w.accountant, DUE)
    assert alerts is not None and alerts.refunds == ()


# --- the chat: partner_balance ------------------------------------------------------------------


def test_the_balance_by_name(w: World) -> None:
    answer = run_tool(
        ctx(w, w.manager, DUE + timedelta(days=2)),
        "partner_balance",
        {"partner": "partenaire test"},
    )
    assert answer.outcome == r.OK
    assert answer.data == {
        "found": True,
        "partner_id": w.partner.id,
        "balance": "21668.00",
        "oldest": {
            "amount": "21668.00",
            "start": "2026-10-01",
            "end": "2026-10-15",
            "due": "2026-10-30",
            "level": "overdue",
            "days_late": 2,
        },
    }
    shown = " ".join(texts(answer))
    assert "Partenaire Test" in shown and "en retard de 2 jours" in shown
    assert f"/partner-accounts/{w.partner.id}" in [x.href for x in answer.lines]


def test_the_balance_through_the_router(w: World) -> None:
    answer = route_message(ctx(w, w.accountant), "Quel est le solde de Partenaire Test ?")
    assert answer.tool == "partner_balance" and answer.data["partner_id"] == w.partner.id


def test_an_approved_alias_names_the_partner(w: World) -> None:
    alias = partner_aliases.propose(w.db, w.staff, w.partner.id, "PT VOYAGES", now=at(DUE))
    assert alias is not None
    partner_aliases.approve(w.db, w.manager, alias.id, now=at(DUE))
    w.db.commit()
    answer = route_message(ctx(w, w.manager), "solde de pt voyages")
    assert answer.data["partner_id"] == w.partner.id


def test_an_ambiguous_name_asks_which_partner(w: World) -> None:
    answer = run_tool(ctx(w, w.manager), "partner_balance", {"partner": "Partenaire"})
    assert answer.data == {"found": False, "matches": 2}
    assert texts(answer)[0] == tools.SEVERAL_PARTNERS
    assert sorted(x.href for x in answer.lines if x.href) == sorted(
        f"/partner-accounts/{p.id}" for p in (w.partner, w.second)
    )


def test_another_agencys_partner_is_never_found(w: World) -> None:
    """The other agency has its own "Partenaire Test": its manager finds only that one, and an
    unknown name reads the same as one of another agency."""
    mine = run_tool(ctx(w, w.other_manager), "partner_balance", {"partner": "Partenaire Test"})
    assert mine.data["partner_id"] == w.foreign.id
    nobody = run_tool(ctx(w, w.other_manager), "partner_balance", {"partner": "Partenaire Deux"})
    assert nobody.data == {"found": False, "matches": 0}
    assert texts(nobody)[0] == tools.NO_PARTNER
    assert "Partenaire Deux" not in nobody.plain  # nothing typed is echoed


@pytest.mark.parametrize("name", PARTNER_TOOLS)
def test_staff_get_the_plain_refusal(w: World, name: str) -> None:
    answer = run_tool(ctx(w, w.staff), name, {"partner": "Partenaire Test"})
    assert (answer.outcome, texts(answer)) == (r.DENIED, [tools.DENIED])
    assert tools.TOOLS[name].handler(ctx(w, w.staff), {"partner": "x"}).outcome == r.DENIED


@pytest.mark.parametrize(
    "message", ["solde de Partenaire Test", "qui doit de l'argent ?", "relevé Partenaire Test"]
)
def test_staff_asking_in_words_get_the_same_refusal(w: World, message: str) -> None:
    answer = route_message(ctx(w, w.staff), message)
    assert (answer.outcome, texts(answer)) == (r.DENIED, [tools.DENIED])


@pytest.mark.parametrize("value", ["", "   ", "x" * 81, 12, None])
def test_a_partner_argument_must_be_a_short_text(value: Any) -> None:
    tool = tools.TOOLS["partner_balance"]
    with pytest.raises(tools.ToolArgError):
        tool.check_args({"partner": value})
    assert tool.check_args({"partner": "  Partenaire   Test "}) == {"partner": "Partenaire Test"}


# --- the chat: partners_owing -------------------------------------------------------------------


def test_who_owes_money(w: World) -> None:
    answer = route_message(ctx(w, w.manager, DUE + timedelta(days=3)), "qui doit de l'argent ?")
    assert answer.tool == "partners_owing"
    assert answer.data == {
        "count": 1,
        "overdue": 1,
        "due_soon": 0,
        "refunds": 0,
        "partners": [
            {
                "partner_id": w.partner.id,
                "amount": "21668.00",
                "level": "overdue",
                "due": "2026-10-30",
                "days_late": 3,
            }
        ],
    }
    assert any("Partenaire Test" in t and "en retard de 3 jours" in t for t in texts(answer))


def test_nobody_owes_before_the_due_window(w: World) -> None:
    answer = run_tool(ctx(w, w.accountant, date(2026, 10, 20)), "partners_owing", {})
    assert answer.data["count"] == 0 and "partners" not in answer.data


def test_who_owes_in_another_agency_is_empty(w: World) -> None:
    answer = run_tool(ctx(w, w.other_manager, DUE + timedelta(days=3)), "partners_owing", {})
    assert answer.data["count"] == 0


# --- the chat: partner_statement ----------------------------------------------------------------


def test_the_statement_of_the_latest_closed_period(w: World) -> None:
    answer = route_message(ctx(w, w.manager), "relevé Partenaire Test")
    assert answer.tool == "partner_statement"
    assert answer.data == {
        "found": True,
        "partner_id": w.partner.id,
        "available": True,
        "start": "2026-10-01",
        "end": "2026-10-15",
        "closed": True,
        "opening": "0.00",
        "sales_total": "21668.00",
        "entries_total": "0.00",
        "payments_total": "0.00",
        "closing": "21668.00",
        "pending": 0,
    }
    links = [(x.href, x.button) for x in answer.lines if x.href]
    pid = w.partner.id
    assert links == [
        (f"/partner-accounts/{pid}/periods/2026-10-01", False),
        (f"/partner-accounts/{pid}/periods/2026-10-01/statement.xlsx", True),
    ]


def test_the_statement_of_a_named_half_month(w: World) -> None:
    answer = route_message(ctx(w, w.accountant), "relevé Partenaire Test 16-31 octobre")
    assert answer.data["start"] == "2026-10-16" and answer.data["closed"] is False
    assert any("provisoire" in t for t in texts(answer))
    missing = route_message(ctx(w, w.accountant), "relevé Partenaire Test 1-15 mars")
    assert missing.data["available"] is False


def test_the_statement_with_an_ambiguous_or_foreign_name(w: World) -> None:
    several = run_tool(ctx(w, w.manager), "partner_statement", {"partner": "partenaire"})
    assert several.data == {"found": False, "matches": 2}
    foreign = run_tool(ctx(w, w.other_manager), "partner_statement", {"partner": "Partenaire Deux"})
    assert foreign.data == {"found": False, "matches": 0}


def test_the_statement_sends_no_file(w: World) -> None:
    answer = run_tool(ctx(w, w.manager), "partner_statement", {"partner": "Partenaire Test"})
    assert answer.handoff_text is None
    assert all(len(x.text) < 300 for x in answer.lines)


# --- what a model sees --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("partner_balance", {"partner": "Partenaire Test"}),
        ("partner_balance", {"partner": "Partenaire"}),
        ("partners_owing", {}),
        ("partner_statement", {"partner": "Partenaire Test"}),
        ("partner_statement", {"partner": "Partenaire Test", "day": "2026-03-01"}),
    ],
)
def test_the_model_sees_the_data_unchanged_and_never_a_name(
    w: World, name: str, args: dict[str, Any]
) -> None:
    answer = run_tool(ctx(w, w.manager, DUE + timedelta(days=1)), name, args)
    assert ai_prompt.sanitize(name, answer.data) == dict(answer.data)
    view = ai_prompt.model_view(answer)
    assert "Partenaire" not in view and "lines" not in json.loads(view)


def test_the_tools_are_offered_to_manager_and_accountant_only(w: World) -> None:
    for user, offered in ((w.manager, True), (w.accountant, True), (w.staff, False)):
        names = {d["name"] for d in ai_prompt.tool_definitions(user)}
        assert set(PARTNER_TOOLS) <= names if offered else not set(PARTNER_TOOLS) & names
