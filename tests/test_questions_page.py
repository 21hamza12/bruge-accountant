"""The questions page: summary, tabs, agent groups, expandable bookings, search, visibility rules
and the fixture's numbers (synthetic report: 0004SV 34 tickets / 25 bookings / 746 423, 0009ER
14 tickets / 8 bookings / 372 434)."""

from __future__ import annotations

import re

import pytest

from tests.builders import build_workbook, ticket_row
from tests.page_support import DAY, Env, fr
from tests.web_support import WebDb, add_user


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def squash(html: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def sections(text: str) -> dict[str, str]:
    """The page cut into one block per staff sign (the group bar and its bookings), by sign."""
    out: dict[str, str] = {}
    for block in text.split('<section class="sign"')[1:]:
        match = re.search(r'<code class="sign-code">([^<]*)</code>', block)
        assert match
        out[match.group(1)] = block.split("</section>")[0]
    return out


def number(block: str, css: str) -> str:
    match = re.search(rf'class="{css}">([^<]*)<', block)
    assert match, css
    return match.group(1)


def bookings(block: str) -> list[str]:
    """Each booking (a ``<details>``) of a block, as its HTML."""
    return [b.split("</details>")[0] for b in block.split('<details class="booking"')[1:]]


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
    assert number(sv, "total") == fr(746423)
    assert number(sv, "confirmed-count") == "0"
    assert (number(er, "ticket-count"), number(er, "group-count")) == ("14", "8")
    assert number(er, "total") == fr(372434)
    assert len(bookings(sv)) == 25
    assert len(bookings(er)) == 8
    assert response.headers["cache-control"] == "no-store"
    assert '<h1 class="page-title">Questions de prix</h1>' in response.text


def test_the_summary_tiles(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    tiles = [squash(t) for t in re.findall(r'<div class="card tile">(.*?)</div>', html, re.S)]
    assert tiles == [
        "À tarifer 33",
        "Confirmées 0",
        "Billets 48",
        f"Coût total {fr(1118857)} MRU",
    ]


def test_each_booking_row_shows_reference_passenger_airlines_tickets_cost_and_status(
    env: Env,
) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    row = next(b for b in bookings(html) if "QA0013" in b.split("</summary>")[0])
    summary = row.split("<summary>")[1].split("</summary>")[0]
    assert re.search(r'<span class="b-ref">QA0013</span>', summary)
    assert "PAX24/GIVEN24" in summary and "+2" in summary  # lead passenger and the others
    assert "Qatar Airways" in summary and "Tunisair" in summary  # names, not 157 / 199
    assert "3 billets" in squash(summary)
    assert re.search(r'<span class="pill pill-amber">En attente</span>', summary)
    assert re.search(r'class="b-cost">[^<]*\d', summary)


def test_expanding_a_booking_shows_each_passenger_with_type_airline_and_cost(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    row = next(b for b in bookings(html) if "QA0013" in b.split("</summary>")[0])
    detail = row.split('<div class="booking-detail">')[1]
    lines = re.findall(r'<div class="pax-line">(.*?)</div>', detail, re.S)
    assert len(lines) == 3
    assert all("PAX24/GIVEN24" in squash(line) for line in lines)
    assert all("Adulte" in squash(line) for line in lines)
    airlines = sorted(re.search(r"(Qatar Airways|Tunisair)", squash(x)).group(1) for x in lines)  # type: ignore[union-attr]
    assert airlines == ["Qatar Airways", "Qatar Airways", "Tunisair"]
    assert all(re.search(r"\d", squash(line)) for line in lines)
    assert all("999" in line for line in lines)  # the ticket numbers (mono)
    er = next(b for b in bookings(html) if "QA0030" in b.split("</summary>")[0])
    assert "Enfant" in er
    assert any("Bébé" in b for b in bookings(html))


def test_airline_codes_are_never_shown_bare(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    assert "Royal Air Maroc" in html and "Turkish Airlines" in html and "Cie 495" in html
    assert not re.search(r"<code>(147|157|199|235|495)</code>", html)


def test_money_uses_the_french_grouping() -> None:
    from decimal import Decimal

    from app.web.templating import render_string

    assert render_string("{{ v|money }}", v=Decimal("1234.5")) == "1234.50"
    assert render_string("{{ v|money_fr }}", v=Decimal("746423.00")) == fr(746423)


# --- tabs, date and default ---------------------------------------------------------------------


def test_the_filter_tabs_are_plain_links_with_counts(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    tabs = html.split('<nav class="tabs"')[1].split("</nav>")[0]
    links = re.findall(r'<a class="tab-link"([^>]*)>(.*?)</a>', tabs, re.S)
    assert [squash(label) for _, label in links] == ["À tarifer · 33", "Confirmées · 0", "Toutes"]
    assert 'aria-current="page"' in links[0][0]
    assert 'aria-current="page"' not in links[1][0]
    assert f'href="/questions?date={DAY}&amp;status=confirmed"' in tabs
    assert f'href="/questions?date={DAY}&amp;status=all"' in tabs


def test_the_confirmed_tab_is_empty_for_now_and_the_all_tab_lists_everything(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        confirmed = c.get(f"/questions?date={DAY}&status=confirmed")
        everything = c.get(f"/questions?date={DAY}&status=all")
    assert confirmed.status_code == 200 and everything.status_code == 200
    assert sections(confirmed.text) == {}
    assert "Aucune question de prix" in confirmed.text
    assert sum(len(bookings(b)) for b in sections(everything.text).values()) == 33
    assert re.search(r'<a class="tab-link" aria-current="page"[^>]*>\s*Toutes', everything.text)


@pytest.mark.parametrize(
    "params", [{"status": "nope"}, {"limit": "0"}, {"limit": "x"}, {"limit": "100000"}]
)
def test_invalid_status_or_limit_is_the_400_page(env: Env, params: dict[str, str]) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get("/questions", params={"date": DAY, **params})
    assert response.status_code == 400
    assert "Référence" in response.text


def test_the_default_date_is_the_latest_report_date_with_tickets(env: Env) -> None:
    env.load()
    env.load(data=build_workbook([ticket_row(doc="3940000999", rloc="LATE00")], range_raw="05OCT"))
    with env.manager_a() as c:
        default = c.get("/")
        questions = c.get("/questions")
        older = c.get(f"/questions?date={DAY}")
    assert default.status_code == 200
    assert 'value="2026-10-05"' in questions.text
    assert set(sections(questions.text)) == {"0004SV"}
    assert "LATE00" in questions.text
    assert len(sections(older.text)) == 2
    assert "LATE00" not in older.text
    assert f'href="/questions?date={DAY}"' in questions.text  # the recent dates stay linked


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


# --- show more ----------------------------------------------------------------------------------


def test_the_limit_shows_the_first_rows_of_each_agent_and_links_to_the_rest(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}&limit=3").text
    by_sign = sections(html)
    assert len(bookings(by_sign["0004SV"])) == 3
    assert len(bookings(by_sign["0009ER"])) == 3
    assert "Afficher les 22 autres réservations" in squash(by_sign["0004SV"])
    assert "Afficher les 5 autres réservations" in squash(by_sign["0009ER"])
    more = re.search(r'<a class="more-link" href="([^"]+)"', by_sign["0004SV"])
    assert more
    assert "limit=" in more.group(1) and "#agent-0004SV" in more.group(1)
    assert number(by_sign["0004SV"], "group-count") == "25"  # the header counts all of them


def test_following_the_more_links_ends_with_every_row(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}&limit=3").text
        shown = [len(bookings(sections(html)["0004SV"]))]
        for _ in range(10):
            link = re.search(r'<a class="more-link" href="([^"]+)"', sections(html)["0004SV"])
            if link is None:
                break
            html = c.get(link.group(1).replace("&amp;", "&").split("#")[0]).text
            shown.append(len(bookings(sections(html)["0004SV"])))
    assert shown == sorted(shown) and len(set(shown)) == len(shown)
    assert shown[0] == 3 and shown[-1] == 25
    assert "more-link" not in html


def test_the_default_limit_shows_all_the_fixture_rows(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    assert "more-link" not in html


# --- search -------------------------------------------------------------------------------------


def test_search_by_booking_reference(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}&q=qa0013").text
    found = [b for s in sections(html).values() for b in bookings(s)]
    assert len(found) == 1 and "QA0013" in found[0]
    assert 'value="qa0013"' in html  # the box keeps what was typed


def test_search_by_passenger_and_by_ticket_number(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        by_name = c.get(f"/questions?date={DAY}&q=GIVEN24").text
        by_ticket = c.get(f"/questions?date={DAY}&q=9990000110").text
    assert sum(len(bookings(s)) for s in sections(by_name).values()) >= 1
    assert all("GIVEN24" in b for s in sections(by_name).values() for b in bookings(s))
    found = [b for s in sections(by_ticket).values() for b in bookings(s)]
    assert len(found) == 1 and "QA0009" in found[0]


def test_search_with_no_match_says_so(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}&q=zzzzzz").text
    assert sections(html) == {}
    assert "Aucun résultat" in html


@pytest.mark.parametrize("query", ["%", "_", "' OR 1=1 --", "\\", "<b>", "é"])
def test_search_treats_special_characters_literally(env: Env, query: str) -> None:
    env.load()
    with env.manager_a() as c:
        response = c.get("/questions", params={"date": DAY, "q": query})
    assert response.status_code == 200
    assert sections(response.text) == {}
    assert "<b>" not in response.text


def test_search_text_is_escaped_in_the_box_and_the_message(env: Env) -> None:
    env.load()
    payload = '"><script>alert(1)</script>'
    with env.manager_a() as c:
        response = c.get("/questions", params={"date": DAY, "q": payload})
    assert response.status_code == 200
    assert "<script>alert(1)" not in response.text
    assert "&lt;script&gt;" in response.text
    assert not re.findall(r"<script(?![^>]*src=)", response.text)


def test_a_very_long_search_is_the_400_page(env: Env) -> None:
    with env.manager_a() as c:
        assert c.get("/questions", params={"q": "x" * 500}).status_code == 400


def test_search_keeps_the_date_and_the_tab_in_its_hidden_fields(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}&status=all").text
    form = re.search(r'<form class="search".*?</form>', html, re.S)
    assert form
    assert f'name="date" value="{DAY}"' in form.group(0)
    assert 'name="status" value="all"' in form.group(0)


def test_search_never_reaches_other_peoples_bookings_for_staff(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:
        mine = c.get(f"/questions?date={DAY}&q=QA00")
        # QA0009 belongs to 0004SV: not theirs, so not found
        theirs = c.get(f"/questions?date={DAY}&q=QA0009")
    assert set(sections(mine.text)) == {"0009ER"}
    assert sections(theirs.text) == {}
    assert "0004SV" not in theirs.text


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
    assert len(bookings(by_sign["0009ER"])) == 8
    assert "0004SV" not in response.text
    assert "Non attribué" not in response.text  # only managers see the flag
    assert 'href="/staff"' not in response.text
    tiles = squash(response.text.split('aria-label="Résumé"')[1].split("</section>")[0])
    assert "À tarifer 8" in tiles and "Billets 14" in tiles
    assert fr(372434) in tiles and fr(1118857) not in response.text


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
        post_forms = re.findall(r'<form[^>]*method="post"[^>]*>', text)
        assert all('action="/logout"' in f for f in post_forms)  # only the sidebar sign-out
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


def test_a_booking_is_reachable_by_its_anchor(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}").text
    assert 'id="b-0004SV-QA0009"' in html
    assert 'id="agent-0004SV"' in html


def test_no_inline_styles_on_the_questions_page(env: Env) -> None:
    env.load()
    with env.manager_a() as c:
        html = c.get(f"/questions?date={DAY}&limit=2").text
    assert not re.search(r"\sstyle=", html, re.I)
    assert not re.search(r"<style", html, re.I)
