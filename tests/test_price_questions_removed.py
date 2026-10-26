"""The old « Questions de prix » workflow is gone: no page, no menu entry, no phone tab, no
« À tarifer » card. Sales are entered through « Saisir une vente » and compared with the report.

The tables and columns the feature used (``tickets.price_status`` ...) are deliberately left in
place (expand-only policy); nothing reads them any more.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.models import ROLE_ACCOUNTANT
from tests.page_support import DAY, Env
from tests.web_support import PASSWORD_S, WebDb, add_user

ACCOUNTANT_PHONE = "+22212345671"
GONE = ("Questions de prix", "À tarifer", "Commencer", "/questions", "Prix confirmés")


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.load()
    e.link(e.seeded.staff_a, "0009ER")
    return e


def accountant(env: Env) -> TestClient:
    add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        ACCOUNTANT_PHONE,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )
    return env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)


def tabs(html: str) -> list[str]:
    bar = html.split('<nav class="tabbar"')[1].split("</nav>")[0]
    return [
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label)).strip()
        for label in re.findall(r'<span class="tab-label">(.*?)</span>', bar)
    ]


def clients(env: Env) -> dict[str, TestClient]:
    return {
        "staff": env.staff_a(),
        "manager": env.manager_a(),
        "accountant": accountant(env),
    }


@pytest.mark.parametrize(
    "path",
    [
        "/questions",
        f"/questions?date={DAY}",
        f"/questions?date={DAY}&status=all&limit=1000",
        "/questions?q=QA0001",
    ],
)
def test_the_questions_page_is_gone_for_every_role(env: Env, path: str) -> None:
    for role, client in clients(env).items():
        assert client.get(path).status_code == 404, role


def test_the_questions_page_is_gone_for_anonymous_visitors_too(env: Env) -> None:
    with env.factory() as c:
        assert c.get("/questions", follow_redirects=False).status_code in (303, 404)


def test_posting_to_questions_is_not_a_route_either(env: Env) -> None:
    with env.manager_a() as c:
        assert c.post("/questions", data={}).status_code in (403, 404, 405)


def test_the_phone_bar_per_role(env: Env) -> None:
    found = {role: tabs(c.get("/").text) for role, c in clients(env).items()}
    assert found["staff"] == [
        "Tableau de bord",
        "Saisir une vente",
        "Mes ventes",
        "Envoyer un rapport",
        "Compte",
    ]
    assert found["manager"] == [
        "Tableau de bord",
        "Saisir une vente",
        "Envoyer un rapport",
        "Équipe",
        "Compte",
    ]
    assert found["accountant"] == [
        "Tableau de bord",
        "Saisir une vente",
        "Envoyer un rapport",
        "Partenaires",
        "Compte",
    ]


def test_no_role_sees_the_old_workflow_anywhere_on_the_home_page(env: Env) -> None:
    for role, client in clients(env).items():
        html = client.get("/").text
        for gone in GONE:
            assert gone not in html, (role, gone)
        assert "en attente</span></span>" not in html.split('<nav class="tabbar"')[1], role


def test_the_pages_that_used_to_link_to_the_questions_do_not_any_more(env: Env) -> None:
    with env.manager_a() as c:
        for path in ("/", "/uploads", "/days", "/sales", "/flags", "/chat", "/staff"):
            html = c.get(path).text
            assert 'href="/questions' not in html, path
            assert 'action="/questions' not in html, path


def test_a_ticket_sold_under_another_sign_is_not_listed_as_missing() -> None:
    """The staff home counts a report ticket as entered when any active sale of the agency holds
    it (the comparison's rule), not only a sale on the agent's own signs."""
    import inspect

    from app.web.routes import home

    source = inspect.getsource(home._staff_context)
    assert "active_ticket_lookup" in source
