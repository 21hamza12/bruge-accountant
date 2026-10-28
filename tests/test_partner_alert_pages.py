"""The partner alerts on the home page (phase F, slice F4): shown to the manager and the
accountant, never to staff or another agency, a constant number of statements whatever the
number of partners, and no inline script or style. Made-up names only."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from tests.page_support import Env
from tests.test_partner_account_pages import (
    _more_partners,
    accountant,
    add_accountant,
    counting,
)
from tests.web_support import WebDb

OVERDUE_DAY = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)  # the 1-15 Oct periods are 1 day late
DUE_SOON_DAY = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)  # due on the 30th
TEMPLATE = Path(__file__).resolve().parent.parent / "app/web/templates/_partner_alerts.html"


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def home(c: TestClient) -> str:
    response = c.get("/", follow_redirects=False)
    assert response.status_code == 200, response.text
    return response.text


def count(html: str, element_id: str) -> int:
    found = re.search(rf'id="{element_id}">(\d+)<', html)
    assert found, element_id
    return int(found.group(1))


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_the_home_page_shows_overdue_partners(env: Env, who: str) -> None:
    add_accountant(env)
    _more_partners(env, 2, 0)
    env.clock.now = OVERDUE_DAY
    html = home(env.manager_a() if who == "manager" else accountant(env))
    assert 'id="partner-alerts"' in html
    assert count(html, "partner-overdue-count") == 2
    assert count(html, "partner-due-soon-count") == 0
    assert "Partenaire Test 0" in html and "1 jour de retard" in html
    assert re.search(r'href="/partner-accounts/\d+"', html)


def test_due_soon_is_shown_from_three_days_before(env: Env) -> None:
    _more_partners(env, 1, 0)
    env.clock.now = DUE_SOON_DAY
    html = home(env.manager_a())
    assert count(html, "partner-due-soon-count") == 1
    assert count(html, "partner-overdue-count") == 0
    assert "bientôt dû" in html and "dans 2 jours" in html


def test_nothing_is_shown_before_the_window(env: Env) -> None:
    _more_partners(env, 1, 0)  # the clock is on 18 October: due on the 30th
    assert 'id="partner-alerts"' not in home(env.manager_a())


def test_staff_never_see_the_alerts(env: Env) -> None:
    _more_partners(env, 1, 0)
    env.clock.now = OVERDUE_DAY
    html = home(env.staff_a())
    assert "partner-alerts" not in html and "Partenaire Test" not in html


def test_another_agency_sees_none_of_them(env: Env) -> None:
    _more_partners(env, 1, 0)
    env.clock.now = OVERDUE_DAY
    html = home(env.manager_b())
    assert "partner-alerts" not in html and "Partenaire Test" not in html


def _home_statements(env: Env, client: TestClient) -> list[str]:
    engine: Engine = env.factory.app.state.engine  # type: ignore[attr-defined]
    home(client)  # warm-up
    with counting(engine) as seen:
        home(client)
    return list(seen)


def test_the_home_page_sends_the_same_number_of_statements_for_2_or_12_partners(
    env: Env,
) -> None:
    _more_partners(env, 2, 0)
    env.clock.now = OVERDUE_DAY
    m = env.manager_a()  # signed in on the day shown (a session does not last two weeks)
    few = _home_statements(env, m)
    _more_partners(env, 10, 100)
    many = _home_statements(env, m)
    assert count(home(m), "partner-overdue-count") == 12
    assert len(many) == len(few) <= 20, (len(few), len(many))
    assert not any("snapshot" in statement for statement in many)


def test_the_alerts_template_has_no_inline_script_or_style() -> None:
    source = TEMPLATE.read_text(encoding="utf-8")
    assert "<script" not in source and "<style" not in source
    assert not re.search(r"\sstyle\s*=", source)
    assert not re.search(r"\son[a-z]+\s*=", source)  # no inline event handler


def test_the_rendered_block_has_no_inline_script_or_style(env: Env) -> None:
    _more_partners(env, 1, 0)
    env.clock.now = OVERDUE_DAY
    html = home(env.manager_a())
    block = html.split('id="partner-alerts"')[1].split("</section>")[0]
    assert "<script" not in block and "style=" not in block
