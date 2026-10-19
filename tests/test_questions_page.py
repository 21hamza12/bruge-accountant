"""Step 29: the read-only questions page, visibility rules and the fixture's numbers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ingest import ingest_report
from app.models import StaffSignLink
from app.tenancy import rebind_tenant
from tests.builders import build_workbook, ticket_row
from tests.web_support import (
    PASSWORD_A,
    PASSWORD_B,
    PASSWORD_S,
    PHONE_A,
    PHONE_B,
    PHONE_STAFF_A,
    FakeClock,
    Seeded,
    WebDb,
    add_user,
    login,
    make_client,
    owner_session,
    seed,
)

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
DAY = "2026-10-01"


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.factory = make_client(web_db, FakeClock())

    def as_user(self, phone: str, password: str) -> TestClient:
        c = self.factory()
        assert login(c, phone, password).status_code == 303
        return c

    def manager_a(self) -> TestClient:
        return self.as_user(PHONE_A, PASSWORD_A)

    def staff_a(self) -> TestClient:
        return self.as_user(PHONE_STAFF_A, PASSWORD_S)

    def manager_b(self) -> TestClient:
        return self.as_user(PHONE_B, PASSWORD_B)

    def load(self, agency_id: int | None = None, data: bytes | None = None) -> None:
        with owner_session(self.db.owner_url) as s:
            ingest_report(
                s,
                agency_id or self.seeded.agency_a,
                FIXTURE.read_bytes() if data is None else data,
                "1OCT.xlsx",
            )

    def link(self, user_id: int, sign: str, agency_id: int | None = None) -> None:
        agency = agency_id or self.seeded.agency_a
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency)
            s.add(StaffSignLink(agency_id=agency, user_id=user_id, staff_sign=sign))
            s.commit()


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def sections(text: str) -> dict[str, str]:
    """The page cut into one block per staff sign, keyed by the sign."""
    out: dict[str, str] = {}
    for block in text.split('<section class="sign">')[1:]:
        match = re.search(r'<code class="sign-code">([^<]*)</code>', block)
        assert match
        out[match.group(1)] = block.split("</section>")[0]
    return out


def number(block: str, css: str) -> str:
    match = re.search(rf'class="{css}">([^<]*)<', block)
    assert match, css
    return match.group(1)


# --- the fixture's numbers ----------------------------------------------------------------------


def test_a_manager_sees_every_sign_with_the_fixture_numbers(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get(f"/questions?date={DAY}")
    assert response.status_code == 200
    by_sign = sections(response.text)
    assert list(by_sign) == ["0004SV", "0009ER"]
    sv, er = by_sign["0004SV"], by_sign["0009ER"]
    assert (number(sv, "ticket-count"), number(sv, "group-count")) == ("34", "25")
    assert number(sv, "total") == "746423"
    assert (number(er, "ticket-count"), number(er, "group-count")) == ("14", "8")
    assert number(er, "total") == "372434"
    assert sv.count('<article class="question">') == 25
    assert er.count('<article class="question">') == 8
    assert response.headers["cache-control"] == "no-store"


def test_each_question_shows_passengers_airlines_and_costs(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        text = c.get(f"/questions?date={DAY}").text
    block = next(b for b in text.split('<article class="question">')[1:] if "QA0013" in b)
    block = block.split("</article>")[0]
    assert block.count("PAX24/GIVEN24") == 3
    assert "157" in block
    assert "199" in block
    assert number(block, "ticket-count") == "3"
    assert "Adulte" in block
    er = next(b for b in text.split('<article class="question">')[1:] if "QA0030" in b)
    assert "Enfant" in er
    assert "Bébé" in er


def test_money_uses_the_shared_formatting() -> None:
    from decimal import Decimal

    from app.web.templating import render_string

    assert render_string("{{ v|money }}", v=Decimal("1234.5")) == "1234.50"
    assert render_string("{{ v|money }}", v=Decimal("746423.00")) == "746423"


def test_the_default_date_is_the_latest_report_date_with_tickets(env: Env) -> None:
    env.load()
    env.load(data=build_workbook([ticket_row(doc="3940000999", rloc="LATE00")], range_raw="05OCT"))
    with env.manager_a() as c:
        default = c.get("/questions")
        older = c.get(f"/questions?date={DAY}")
    assert default.status_code == 200
    assert "2026-10-05" in default.text
    assert set(sections(default.text)) == {"0004SV"}
    assert "LATE00" in default.text
    assert len(sections(older.text)) == 2
    assert "LATE00" not in older.text


def test_no_data_at_all_is_a_friendly_empty_page(env: Env) -> None:
    with env.manager_a() as c:
        response = c.get("/questions")
    assert response.status_code == 200
    assert "Aucun rapport" in response.text
    assert "/uploads/new" in response.text


def test_a_date_without_tickets_says_so(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get("/questions?date=2026-09-01")
    assert response.status_code == 200
    assert "Aucune question" in response.text
    assert sections(response.text) == {}


@pytest.mark.parametrize(
    "value",
    [
        "not-a-date",
        "2026-13-01",
        "2026-02-30",
        "20261001",
        "2026-10-1",
        "2026-10-01T00:00",
        "'",
        "x" * 500,
    ],
)
def test_an_invalid_date_is_the_400_page(env: Env, value: str) -> None:
    with env.manager_a() as c:
        response = c.get("/questions", params={"date": value})
    assert response.status_code == 400
    assert "Référence" in response.text


def test_an_empty_date_means_the_default(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        assert c.get("/questions?date=").status_code == 200


# --- who sees what ------------------------------------------------------------------------------


def test_a_manager_sees_the_owner_of_each_sign_and_unlinked_signs_are_flagged(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.manager_a() as c:
        by_sign = sections(c.get(f"/questions?date={DAY}").text)
    assert "Staff A" in by_sign["0009ER"]
    assert "Non attribué" not in by_sign["0009ER"]
    assert "Non attribué" in by_sign["0004SV"]
    assert 'href="/staff"' in by_sign["0004SV"]


def test_staff_see_only_the_signs_linked_to_them(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        response = c.get(f"/questions?date={DAY}")
    assert response.status_code == 200
    by_sign = sections(response.text)
    assert list(by_sign) == ["0009ER"]
    assert number(by_sign["0009ER"], "group-count") == "8"
    assert by_sign["0009ER"].count('<article class="question">') == 8
    assert "0004SV" not in response.text
    assert "Non attribué" not in response.text  # only managers see the flag
    assert 'href="/staff"' not in response.text


def test_a_staff_user_with_two_signs_sees_both(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    env.link(env.seeded.staff_a, "0004SV")
    with env.staff_a() as c:
        assert list(sections(c.get(f"/questions?date={DAY}").text)) == ["0004SV", "0009ER"]


def test_staff_without_linked_signs_get_a_friendly_empty_state(env: Env) -> None:
    env.load()
    with env.staff_a() as c:
        response = c.get(f"/questions?date={DAY}")
    assert response.status_code == 200
    assert sections(response.text) == {}
    assert "Aucun code agent" in response.text
    assert "0004SV" not in response.text
    assert "0009ER" not in response.text


def test_a_sign_linked_to_someone_else_is_not_shown_to_staff(env: Env) -> None:
    env.load()
    other = add_user(env.db.owner_url, env.seeded.agency_a, "+22212345620", display_name="Other")
    env.link(other, "0004SV")
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        text = c.get(f"/questions?date={DAY}").text
    assert "0004SV" not in text
    assert "Other" not in text


# --- no leaks between agencies ------------------------------------------------------------------


def test_another_agency_sees_nothing_for_the_same_date(env: Env) -> None:
    env.load()  # agency A only
    with env.manager_b() as c:
        same_date = c.get(f"/questions?date={DAY}")
        default = c.get("/questions")
        forged = c.get(
            "/questions",
            params={"date": DAY, "agency_id": env.seeded.agency_a, "agency": "agence-a"},
        )
    for response in (same_date, forged):
        assert response.status_code == 200
        assert sections(response.text) == {}
        assert "0004SV" not in response.text
        assert "GIVEN" not in response.text
    assert "Aucun rapport" in default.text
    assert "0004SV" not in default.text


def test_each_agency_sees_its_own_data_only(env: Env) -> None:
    env.load()
    env.load(
        agency_id=env.seeded.agency_b,
        data=build_workbook([ticket_row(doc="3940000777", rloc="BBBBBB", staff="0777ZZ")]),
    )
    with env.manager_a() as a, env.manager_b() as b:
        text_a, text_b = a.get(f"/questions?date={DAY}").text, b.get(f"/questions?date={DAY}").text
    assert set(sections(text_a)) == {"0004SV", "0009ER"}
    assert set(sections(text_b)) == {"0777ZZ"}
    assert "BBBBBB" not in text_a
    assert "0004SV" not in text_b


def test_anonymous_is_sent_to_login(env: Env) -> None:
    with env.factory() as c:
        response = c.get("/questions", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_page_is_read_only(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        text = c.get(f"/questions?date={DAY}").text
        assert "<form" not in text.replace('<form method="get"', "")
        assert 'type="submit"' not in text.replace('type="submit">Afficher', "")
        assert c.post("/questions", follow_redirects=False).status_code in (403, 405)


# --- escaping -----------------------------------------------------------------------------------


def test_hostile_names_and_signs_are_escaped(env: Env) -> None:
    script = "<script>alert(1)</script>"
    img = '"><img src=x onerror=alert(1)>'
    rows = [
        ticket_row(doc="3940000001", name=f"{script}/X", rloc="AAAAAA"),
        ticket_row(doc="3940000002", name=f"{img}/Y", rloc="BBBBBB", staff=img[:30]),
    ]
    env.load(data=build_workbook(rows))
    env.link(env.seeded.staff_a, "0004SV")
    with env.manager_a() as c:
        text = c.get(f"/questions?date={DAY}").text
    assert script not in text
    assert "<img" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert not re.findall(r"<script(?![^>]*src=)", text)


def test_the_empty_state_is_not_cached(env: Env) -> None:
    with env.manager_a() as c:
        response = c.get("/questions")
    assert "Aucun rapport" in response.text
    assert response.headers["cache-control"] == "no-store"
