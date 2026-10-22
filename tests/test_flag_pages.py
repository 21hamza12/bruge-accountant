"""GET /flags and POST /flags/run: French labels, who sees what, CSRF, roles, isolation."""

from __future__ import annotations

import re

import pytest

from app.auth import ratelimit
from app.web.routes.flags import FlagRow, flag_row
from tests.fixtures import synthetic_20oct as fx
from tests.flag_support import FlagWeb
from tests.web_support import WebDb, csrf_for

DAY = "2026-10-20"
FIRST = str(fx.LINES[0]["DOC NUMBER"])
TKTT = [str(line["DOC NUMBER"]) for line in fx.LINES if line["TRNC"] == "TKTT"]
SIGN_OF = {str(line["DOC NUMBER"]): str(line["AS"]) for line in fx.LINES}


@pytest.fixture
def world(web_db: WebDb) -> FlagWeb:
    return FlagWeb(web_db)


def with_flags(world: FlagWeb) -> None:
    """One flag of each of three kinds, a partner ticket missing and nine cancellations."""
    changed, missing = TKTT[1], TKTT[2]
    world.sell_report(skip=[missing], extra_cost={changed: 1000})
    with world.manager() as c:
        assert world.upload(c).status_code == 303


def rows_of(html: str) -> list[str]:
    body = html.split('id="flags-title"', 1)[1].split("</table>", 1)[0]
    return re.findall(r"<tr>(.*?)</tr>", body, flags=re.DOTALL)[1:]


def test_the_list_names_each_kind_in_french_with_both_values(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        page = c.get(f"/flags?date={DAY}")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    text = page.text
    assert "Différent" in text and "Non envoyé" in text
    rows = rows_of(text)
    assert len(rows) == 2
    different = next(r for r in rows if "Différent" in r)
    assert TKTT[1] in different and "Coût (TOTAL DOC)" in different
    assert "Ouvert" in different
    assert "validation du gérant ou du comptable" in different
    assert SIGN_OF[TKTT[1]] in different
    not_sent = next(r for r in rows if "Non envoyé" in r)
    assert TKTT[2] in not_sent and SIGN_OF[TKTT[2]] in not_sent


def test_the_information_sections_are_not_flags(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        text = c.get(f"/flags?date={DAY}").text
    assert "Billets annulés ce jour" in text
    assert "Billets partenaires absents du rapport (autre bureau)" in text
    cancelled = text.split('id="cancelled-title"', 1)[1].split("</section>", 1)[0]
    assert cancelled.count('<span class="mono">') == 9 * 2  # number and sign of nine lines
    assert "Aucun billet partenaire absent." in text


def test_a_comparison_summary_is_on_top(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        text = c.get(f"/flags?date={DAY}").text
    assert re.search(r"Billets identiques</dt><dd class=\"num\">32</dd>", text)
    assert re.search(r"Écarts bloquants</dt><dd class=\"num\">2</dd>", text)
    assert "Relancer la comparaison" in text and "Comparée" in text


def test_staff_see_only_their_own_signs(world: FlagWeb) -> None:
    owners = {SIGN_OF[TKTT[1]], SIGN_OF[TKTT[2]]}
    with_flags(world)
    mine = sorted(owners)[0]
    world.link_staff(mine)
    with world.staff() as c:
        text = c.get(f"/flags?date={DAY}").text
    others = {"0004SV", "0006AC", "0009ER"} - {mine}
    table = text.split('id="flags-title"', 1)[1].split("</table>", 1)[0]
    assert mine in table
    assert not any(sign in table for sign in others)
    assert "Relancer la comparaison" not in text
    cancelled = text.split('id="cancelled-title"', 1)[1].split("</section>", 1)[0]
    assert not any(sign in cancelled for sign in others)


def test_a_staff_member_without_a_sign_sees_no_flag(world: FlagWeb) -> None:
    with_flags(world)
    with world.staff() as c:
        text = c.get(f"/flags?date={DAY}").text
    assert "Aucun écart." in text


def test_a_day_without_a_report_says_so(world: FlagWeb) -> None:
    with world.manager() as c:
        page = c.get(f"/flags?date={DAY}")
    assert page.status_code == 200
    assert "Aucun rapport Amadeus n'a été envoyé pour cette journée." in page.text
    assert "Relancer la comparaison" not in page.text


def test_a_bad_date_is_a_400(world: FlagWeb) -> None:
    with world.manager() as c:
        assert c.get("/flags?date=20261020").status_code == 400
        assert c.get("/flags?date=2026-13-40").status_code == 400


def test_the_page_needs_a_login(world: FlagWeb) -> None:
    anonymous = world.factory()
    response = anonymous.get("/flags", follow_redirects=False)
    assert response.status_code in (303, 401)
    assert response.status_code != 200


def test_no_inline_style_or_script(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        text = c.get(f"/flags?date={DAY}").text
    assert " style=" not in text and "onclick" not in text
    assert not re.search(r"<script(?![^>]*\bsrc=)", text)


# --- the sidebar badge -------------------------------------------------------------------------


def test_the_sidebar_has_an_ecarts_entry_with_the_open_blocking_count(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        text = c.get("/").text
    nav = text.split('href="/flags"', 1)[1].split("</a>", 1)[0]
    assert "Écarts" in nav
    assert re.search(r'class="badge">2<', nav)


def test_the_badge_is_per_viewer_and_absent_at_zero(world: FlagWeb) -> None:
    with_flags(world)
    mine = SIGN_OF[TKTT[1]]
    world.link_staff(mine)
    with world.staff() as c:
        nav = c.get("/").text.split('href="/flags"', 1)[1].split("</a>", 1)[0]
    expected = 2 if SIGN_OF[TKTT[1]] == SIGN_OF[TKTT[2]] else 1
    assert re.search(rf'class="badge">{expected}<', nav)
    with world.other_manager() as c:
        nav = c.get("/").text.split('href="/flags"', 1)[1].split("</a>", 1)[0]
    assert "badge" not in nav


# --- running it again --------------------------------------------------------------------------


def test_a_manager_runs_the_comparison_again(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        response = c.post(
            f"/flags/run?date={DAY}",
            data={"csrf_token": csrf_for(c, "/flags")},
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/flags?date={DAY}"
    assert len(world.flags_rows()) == 2  # nothing changed: no new flag


def test_an_accountant_may_run_it_too(world: FlagWeb) -> None:
    from tests.web_support import PASSWORD_S, add_user

    with_flags(world)
    add_user(
        world.db.owner_url,
        world.seeded.agency_a,
        "+22212345699",
        role="accountant",
        password=PASSWORD_S,
    )
    with world.client("+22212345699", PASSWORD_S) as c:
        response = c.post(
            f"/flags/run?date={DAY}",
            data={"csrf_token": csrf_for(c, "/flags")},
            follow_redirects=False,
        )
    assert response.status_code == 303


def test_staff_cannot_run_it(world: FlagWeb) -> None:
    with_flags(world)
    before = len(world.flags_rows())
    with world.staff() as c:
        response = c.post(
            f"/flags/run?date={DAY}",
            data={"csrf_token": csrf_for(c, "/flags")},
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert len(world.flags_rows()) == before


def test_a_run_without_a_csrf_token_is_refused(world: FlagWeb) -> None:
    with_flags(world)
    with world.manager() as c:
        no_token = c.post(f"/flags/run?date={DAY}", follow_redirects=False)
        bad_token = c.post(
            f"/flags/run?date={DAY}", data={"csrf_token": "nope"}, follow_redirects=False
        )
    assert no_token.status_code == 403 and bad_token.status_code == 403


def test_a_run_needs_a_valid_day_with_a_report(world: FlagWeb) -> None:
    with world.manager() as c:
        token = csrf_for(c, "/flags")
        none = c.post("/flags/run", data={"csrf_token": token})
        assert none.status_code == 400
        missing = c.post("/flags/run?date=2026-10-15", data={"csrf_token": token})
        assert missing.status_code == 400
        assert "Aucun rapport Amadeus" in missing.text
        bad = c.post("/flags/run?date=15-10-2026", data={"csrf_token": token})
        assert bad.status_code == 400


def test_a_run_for_a_closed_day_is_a_409(world: FlagWeb) -> None:
    from tests.test_upload_compare import close_day

    with_flags(world)
    close_day(world)
    with world.manager() as c:
        response = c.post(f"/flags/run?date={DAY}", data={"csrf_token": csrf_for(c, "/flags")})
    assert response.status_code == 409
    assert "clôturée" in response.text


def test_the_manual_run_is_rate_limited(world: FlagWeb, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ratelimit, "COMPARISON_RUN_USER", ratelimit.Limit("comparison_run_user", 2, 3600)
    )
    with_flags(world)
    with world.manager() as c:
        token = csrf_for(c, "/flags")
        codes = [
            c.post(
                f"/flags/run?date={DAY}", data={"csrf_token": token}, follow_redirects=False
            ).status_code
            for _ in range(3)
        ]
    assert codes == [303, 303, 429]


# --- isolation ---------------------------------------------------------------------------------


def test_another_agency_sees_nothing_and_cannot_run_it(world: FlagWeb) -> None:
    with_flags(world)
    with world.other_manager() as c:
        page = c.get(f"/flags?date={DAY}")
        assert "Aucun rapport Amadeus" in page.text
        assert FIRST not in page.text and TKTT[1] not in page.text
        run = c.post(f"/flags/run?date={DAY}", data={"csrf_token": csrf_for(c, "/flags")})
        assert run.status_code == 400
    assert world.flags_rows(world.seeded.agency_b) == []
    assert len(world.flags_rows()) == 2


# --- the row formatting ------------------------------------------------------------------------


def test_row_formatting_per_kind() -> None:
    from datetime import UTC, datetime

    from app.models import ComparisonFlag

    def flag(**kw: object) -> ComparisonFlag:
        base: dict[str, object] = {
            "id": 1,
            "doc_number": "3940000001",
            "kind": "different",
            "field": "total_doc",
            "app_value": "21368",
            "amadeus_value": "21000.5",
            "responsible_sign": "0004SV",
            "needs_validation": True,
            "status": "open",
            "created_at": datetime(2026, 10, 22, tzinfo=UTC),
        }
        base.update(kw)
        return ComparisonFlag(**base)

    money = flag_row(flag())
    assert isinstance(money, FlagRow)
    assert money.app_value.replace(" ", " ") == "21 368"
    assert money.amadeus_value.replace(" ", " ").startswith("21 000")
    elsewhere = flag_row(
        flag(kind="elsewhere", field=None, app_value="2026-10-19", amadeus_value="TKTT")
    )
    assert elsewhere.kind == "Ailleurs" and elsewhere.app_value == "19 octobre 2026"
    not_sent = flag_row(flag(kind="not_sent", field=None, app_value=None, amadeus_value="TKTT"))
    assert (not_sent.kind, not_sent.app_value, not_sent.field) == ("Non envoyé", "—", "")
    bad = flag_row(
        flag(kind="bad_number", field="doc_number", app_value="unusable", amadeus_value="duplicate")
    )
    assert (bad.kind, bad.app_value, bad.amadeus_value) == (
        "Numéro invalide",
        "illisible",
        "en double",
    )
    absent = flag_row(flag(kind="not_in_amadeus", field=None, app_value="TKTT", amadeus_value=None))
    assert absent.kind == "Absent d'Amadeus" and absent.amadeus_value == "—"
    odd = flag_row(flag(app_value="abc"))
    assert odd.app_value == "abc"  # not a number: shown as it is
