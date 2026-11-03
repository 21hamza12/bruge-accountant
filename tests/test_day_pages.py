"""The day pages (B7): the list of days and the app's own TJQ AVANT of one day, with close/reopen.

Synthetic data only. The FakeClock's day is 2026-10-18; the synthetic 20 October report needs the
clock moved to the 21st.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

from app import sales as sales_service
from app.auth import ratelimit
from app.ingest import ingest_report
from app.models import DayEvent, ReportDay, User
from app.tenancy import rebind_tenant
from tests.builders import amadeus_line, build_amadeus_xlsx
from tests.fixtures import synthetic_20oct as fx
from tests.page_support import Env, fr
from tests.test_sale_pages import (
    D1,
    D2,
    D3,
    D4,
    SIGN,
    SIGN2,
    TODAY,
    YESTERDAY,
    accountant,
    enter,
    make_partner,
    post,
    sidebar,
    tabbar,
    table,
    text_of,
    tk,
)
from tests.web_support import (
    PASSWORD_S,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    owner_session,
)

__all__ = ["env"]


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    return e


NNBSP = " "


def amount(value: str) -> str:
    """``"56368.75"`` as the pages print it: ``56 368,75`` (narrow no-break space)."""
    whole, _, cents = value.partition(".")
    return fr(int(whole)) + (f",{cents}" if cents else "")


VOID = {"input", "br", "meta", "link", "img", "hr"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list[Node | str] = field(default_factory=list)

    def text(self) -> str:
        parts = [c if isinstance(c, str) else c.text() for c in self.children]
        return re.sub(r"[ \t\r\n]+", " ", "".join(parts)).strip()

    def find_all(self, tag: str, *, id: str | None = None, cls: str | None = None) -> list[Node]:
        found: list[Node] = []
        for child in self.children:
            if isinstance(child, str):
                continue
            if (
                child.tag == tag
                and (id is None or child.attrs.get("id") == id)
                and (cls is None or cls in child.attrs.get("class", "").split())
            ):
                found.append(child)
            found.extend(child.find_all(tag, id=id, cls=cls))
        return found

    def one(self, tag: str, *, id: str | None = None, cls: str | None = None) -> Node:
        (only,) = self.find_all(tag, id=id, cls=cls)
        return only


class Tree(HTMLParser):
    """A small element tree of a page, to assert on elements instead of on text patterns."""

    def __init__(self) -> None:
        super().__init__()
        self.root = Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def tree(html: str) -> Node:
    parser = Tree()
    parser.feed(html)
    return parser.root


def pairs(dl: Node) -> dict[str, str]:
    return dict(
        zip(
            [dt.text() for dt in dl.find_all("dt")],
            [dd.text() for dd in dl.find_all("dd")],
            strict=True,
        )
    )


def money_text(html: str, label: str) -> str:
    """The figure of ``label`` in the day's totals block (the first ``dl.kv`` of the page)."""
    return pairs(tree(html).find_all("dl", cls="kv")[0])[label]


def groups(html: str) -> list[tuple[str, list[list[str]]]]:
    """``[(group heading, [cells of each line])]`` of the TJQ AVANT table."""
    found: list[tuple[str, list[list[str]]]] = []
    for body in tree(html).find_all("tbody", cls="sale-group"):
        heading = body.one("th").text()
        rows = [[td.text() for td in tr.find_all("td")] for tr in body.find_all("tr")]
        found.append((heading, [row for row in rows if row]))
    return found


def sign_rows(html: str) -> dict[str, list[str]]:
    table_ = tree(html).one("table", id="sign-totals")
    return {
        tr.one("th").text(): [td.text() for td in tr.find_all("td")]
        for tr in table_.one("tbody").find_all("tr")
    }


def list_rows(html: str) -> list[list[str]]:
    """The cells of each row of the days list (the date, its link text, is the first)."""
    body = tree(html).find_all("tbody")[0]
    return [[c.text() for c in tr.children if isinstance(c, Node)] for tr in body.find_all("tr")]


def report_card(html: str) -> str:
    """The text of the "Rapport Amadeus" card."""
    cards = [
        c
        for c in tree(html).find_all("section", cls="card")
        if c.attrs.get("aria-labelledby") == "report-title"
    ]
    (card,) = cards
    return card.text()


def staff2_client(env: Env) -> TestClient:
    uid = add_user(env.db.owner_url, env.seeded.agency_a, "+22212345692", display_name="Staff 2")
    env.link(uid, SIGN2)
    return env.as_user("+22212345692", PASSWORD_S)


def fill_day(env: Env) -> dict[str, int]:
    """A direct sale (sign 0004SV), a 2-ticket layover sale (0006AC) and a partner sale (0004SV)."""
    pid = make_partner(env, "BADI", "500")
    m = env.manager_a()
    direct = enter(m, tk(D1, "21368"), staff_sign=SIGN, day=TODAY, pb="24000")
    layover = enter(
        m,
        tk(D2, "12000.50", airline="199", pax_name="SYNTH02/PAX"),
        tk(D3, "8000.25", airline="147", pax_name="SYNTH02/PAX"),
        staff_sign=SIGN2,
        day=TODAY,
        pb="30000",
    )
    partner = enter(
        m, tk(D4, "15000", pax_name="SYNTH03/PAX"), staff_sign=SIGN, day=TODAY, ag=str(pid)
    )
    return {"direct": direct, "layover": layover, "partner": partner, "pid": pid}


# --- the day page: totals and lines -------------------------------------------------------------


def test_totals_are_exact_and_the_layover_sale_is_grouped_with_pb_on_its_first_line(
    env: Env,
) -> None:
    ids = fill_day(env)
    r = env.manager_a().get(f"/days/{TODAY}")
    assert r.status_code == 200
    html = r.text
    assert money_text(html, "Ventes") == "3"
    assert money_text(html, "Billets") == "4"
    assert money_text(html, "Coût total").startswith(
        amount("56368.75")
    )  # 21368+12000.50+8000.25+15000
    assert money_text(html, "P.B total").startswith(fr(69500))  # 24000 + 30000 + (15000+500)
    assert money_text(html, "Profit").startswith(amount("13131.25"))
    found = groups(html)
    assert [g[0] for g in found] == [
        f"Vente #{ids['direct']} · 1 billet · P.B {fr(24000)}",
        f"Vente #{ids['layover']} · 2 billets · P.B {fr(30000)}",
        f"Vente #{ids['partner']} · 1 billet · P.B {fr(15500)}",
    ]
    layover = found[1][1]
    assert [row[0] for row in layover] == [D2, D3]
    assert [row[2] for row in layover] == [amount("12000.50"), amount("8000.25")]
    assert [row[8] for row in layover] == [fr(30000), "0"]  # P.B on the first ticket only
    assert layover[0][1:2] == ["199"] and layover[1][1] == "147"
    direct = found[0][1][0]
    assert direct == [D1, "199", fr(21368), "SYNTH01/PAX", "ADT", SIGN, "ABC123", "TKTT",
                      fr(24000), "BRUGE", "Client Synth"]  # fmt: skip
    partner = found[2][1][0]
    assert partner[8] == fr(15500) and partner[9] == "BADI"
    day_table = tree(html).one("table", cls="table-day")
    assert [th.text() for th in day_table.one("thead").find_all("th")] == [
        "DOC NUMBER", "A/L", "TOTAL DOC", "PAX NAME", "Type", "AS", "RLOC", "TRNC", "P.B",
        "AG", "CLT",
    ]  # fmt: skip
    group_heads = [th for th in day_table.find_all("th") if th.attrs.get("colspan")]
    assert len(group_heads) == 3
    assert all(th.attrs.get("scope") == "rowgroup" for th in group_heads)
    for table_ in tree(html).find_all("table"):  # every table has a hidden caption
        assert len(table_.find_all("caption", cls="visually-hidden")) == 1


def test_totals_per_sign_and_for_the_day_for_the_manager(env: Env) -> None:
    fill_day(env)
    rows = sign_rows(env.manager_a().get(f"/days/{TODAY}").text)
    assert rows == {
        SIGN: ["2", "2", fr(36368), fr(39500), fr(3132)],
        SIGN2: ["1", "2", amount("20000.75"), fr(30000), amount("9999.25")],
    }


def test_per_sign_totals_ignore_a_voided_sibling_sale(env: Env) -> None:
    fill_day(env)
    m = env.manager_a()
    extra = enter(m, tk("3940000020", "1000"), staff_sign=SIGN, day=TODAY, pb="1500")
    assert sign_rows(m.get(f"/days/{TODAY}").text)[SIGN][0] == "3"
    path = f"/sales/{extra}/void"
    post(m, path, {"reason": "saisie en double", "version_no": "1"}, via=path)
    rows = sign_rows(m.get(f"/days/{TODAY}").text)
    assert rows[SIGN] == ["2", "2", fr(36368), fr(39500), fr(3132)]


def test_the_accountant_sees_everything_too(env: Env) -> None:
    fill_day(env)
    html = accountant(env).get(f"/days/{TODAY}").text
    assert money_text(html, "Billets") == "4" and len(groups(html)) == 3


def test_staff_see_only_their_own_lines_and_totals(env: Env) -> None:
    fill_day(env)
    html = env.staff_a().get(f"/days/{TODAY}").text
    found = groups(html)
    assert len(found) == 2 and all(row[5] == SIGN for _, rows in found for row in rows)
    assert D2 not in html and D3 not in html and SIGN2 not in html
    assert money_text(html, "Ventes") == "2" and money_text(html, "Billets") == "2"
    # no total of cost or of P.B for an agent (decision 26): only the counts
    totals = pairs(tree(html).find_all("dl", cls="kv")[0])
    assert set(totals) == {"Ventes", "Billets"}
    assert fr(36368) not in html and fr(39500) not in html
    assert sign_rows(html) == {SIGN: ["2", "2"]}
    other = staff2_client(env).get(f"/days/{TODAY}").text
    assert D1 not in other and money_text(other, "Billets") == "2"
    assert "Clôturer" not in html and "Rouvrir" not in html


def test_voided_sales_are_excluded(env: Env) -> None:
    ids = fill_day(env)
    m = env.manager_a()
    r = post(
        m,
        f"/sales/{ids['layover']}/void",
        {"reason": "saisie en double", "version_no": "1"},
        via=f"/sales/{ids['layover']}/void",
    )
    assert r.status_code == 303
    html = m.get(f"/days/{TODAY}").text
    assert D2 not in html and D3 not in html
    assert money_text(html, "Ventes") == "2" and money_text(html, "Billets") == "2"
    assert money_text(html, "Coût total").startswith(fr(36368))


def test_a_day_nobody_wrote_to_is_an_empty_page_not_an_error(env: Env) -> None:
    for c in (env.manager_a(), env.staff_a()):
        r = c.get(f"/days/{YESTERDAY}")
        assert r.status_code == 200
        assert "Aucune vente ce jour." in r.text
        assert "Clôturer la journée" not in r.text
        assert tree(r.text).one("p", id="upload-missing").text() == "Rapport Amadeus : non reçu."


def test_another_agencys_day_is_not_visible(env: Env) -> None:
    fill_day(env)
    html = env.manager_b().get(f"/days/{TODAY}").text
    assert "Aucune vente ce jour" in html and D1 not in html


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("2026-13-45", "Date invalide"),
        ("20261018", "Date invalide"),
        ("not-a-date", "Date invalide"),
        ("2026-10-19", "dans le futur"),
        ("2099-01-01", "dans le futur"),
        ("2019-01-01", "trop ancienne"),
    ],
)
def test_a_date_without_a_page_is_a_french_400(env: Env, raw: str, message: str) -> None:
    for c in (env.manager_a(), env.staff_a()):
        r = c.get(f"/days/{raw}")
        assert r.status_code == 400, raw
        assert message in r.text
        assert r.headers["cache-control"] == "no-store"


def test_the_5_year_bound_is_the_one_of_the_days_service(env: Env) -> None:
    c = env.manager_a()
    assert c.get("/days/2021-10-19").status_code == 200
    assert c.get("/days/2020-01-01").status_code == 400


# --- the Amadeus upload status ------------------------------------------------------------------


def upload_20oct(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    with owner_session(env.db.owner_url) as s:
        ingest_report(
            s,
            env.seeded.agency_a,
            fx.build_synthetic_20oct(),
            "20OCT.xlsx",
            uploaded_by=env.seeded.manager_a,
        )


def test_manager_and_accountant_see_the_upload_details_and_its_nine_cancelled_lines(
    env: Env,
) -> None:
    upload_20oct(env)
    for c in (env.manager_a(), accountant(env)):
        r = c.get("/days/2026-10-20")
        assert r.status_code == 200
        details = pairs(tree(r.text).one("dl", id="upload-details"))
        assert details["Statut"] == "Reçu" and details["Fichier"] == "20OCT.xlsx"
        assert details["Envoyé par"] == "Manager A"
        assert details["Lignes"] == "43" and details["Annulées"] == "9"
        assert "Rapport Amadeus : reçu" in text_of(r.text)
        assert "Aucune vente ce jour." in text_of(r.text)


def test_staff_only_learn_that_a_report_was_received(env: Env) -> None:
    upload_20oct(env)
    r = env.staff_a().get("/days/2026-10-20")
    assert r.status_code == 200
    page_ = tree(r.text)
    assert page_.one("p", id="upload-received").text() == "Reçu."
    assert not page_.find_all("dl", id="upload-details")
    assert report_card(r.text) == "Rapport Amadeus Reçu."
    assert "Rapport Amadeus : reçu" in text_of(r.text)
    for leaked in ("20OCT.xlsx", "Manager A", "Annulées", "Lignes", "Envoyé", "43"):
        assert leaked not in text_of(r.text), leaked


@pytest.mark.usefixtures("gate_open")
def test_staff_do_not_see_the_history_of_the_day_only_the_closing_date(env: Env) -> None:
    fill_day(env)
    acc = accountant(env)
    m = env.manager_a()
    post(acc, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    reopen = f"/days/{TODAY}/reopen"
    post(m, reopen, {"reason": "motif secret du gérant"}, via=reopen)
    post(acc, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    staff = env.staff_a().get(f"/days/{TODAY}")
    assert not tree(staff.text).find_all("ul", cls="history")
    for hidden in ("Historique", "motif secret", "Comptable A", "Manager A"):
        assert hidden not in staff.text, hidden
    assert "Clôturée le 18 octobre 2026" in text_of(staff.text)
    boss = m.get(f"/days/{TODAY}").text
    assert len(tree(boss).one("ul", cls="history").find_all("li")) == 4
    assert "motif secret du gérant" in boss


def test_no_upload_for_the_date_means_not_received(env: Env) -> None:
    fill_day(env)
    text = text_of(env.manager_a().get(f"/days/{TODAY}").text)
    assert "Rapport Amadeus : non reçu" in text and "Rapport Amadeus : reçu" not in text


def test_an_identical_re_upload_keeps_the_lines_and_shows_the_newest_file_name(env: Env) -> None:
    upload_20oct(env)
    with owner_session(env.db.owner_url) as s:
        ingest_report(s, env.seeded.agency_a, fx.build_synthetic_20oct(), "second.xlsx")
    details = pairs(
        tree(env.manager_a().get("/days/2026-10-20").text).one("dl", id="upload-details")
    )
    assert details["Fichier"] == "second.xlsx"
    assert details["Lignes"] == "43" and details["Annulées"] == "9"


def test_an_upload_with_other_bytes_for_the_same_date_replaces_the_one_shown(env: Env) -> None:
    upload_20oct(env)
    other = build_amadeus_xlsx(
        [
            amadeus_line(seq="000001", doc="1990000001", trnc="TKTT"),
            amadeus_line(seq="000002", doc="1990000002", trnc="canx "),
        ],
        range_raw="20OCT",
        current="21-OCT-2026",
    )
    with owner_session(env.db.owner_url) as s:
        ingest_report(s, env.seeded.agency_a, other, "other.xlsx")
    details = pairs(
        tree(env.manager_a().get("/days/2026-10-20").text).one("dl", id="upload-details")
    )
    assert details["Fichier"] == "other.xlsx"
    assert details["Lignes"] == "2" and details["Annulées"] == "1"  # case and spacing ignored


# --- the list of days ---------------------------------------------------------------------------


def test_the_list_shows_each_day_with_status_figures_and_report(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    fill_day(env)
    m = env.manager_a()
    enter(m, tk("3940000010", "5000"), staff_sign=SIGN, day=YESTERDAY, pb="6000")
    rows = list_rows(m.get("/days").text)
    assert [row[0] for row in rows] == ["18 octobre 2026", "17 octobre 2026"]
    assert rows[0][1:] == [
        "Ouverte", "3", "4", amount("56368.75"), fr(69500), amount("13131.25"), "Non reçu"
    ]  # fmt: skip
    assert rows[1][1:] == ["Ouverte", "1", "1", fr(5000), fr(6000), fr(1000), "Non reçu"]
    links = [
        a.attrs["href"]
        for a in tree(m.get("/days").text).find_all("a")
        if "/days/20" in a.attrs.get("href", "")
    ]
    assert links == [f"/days/{TODAY}", f"/days/{YESTERDAY}"]


def test_a_date_with_a_report_flips_the_last_column_and_a_report_only_date_is_listed(
    env: Env,
) -> None:
    env.clock.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    m = env.manager_a()
    enter(m, tk("3940000011", "7000"), staff_sign=SIGN, day="2026-10-19", pb="8000")
    with owner_session(env.db.owner_url) as s:
        ingest_report(s, env.seeded.agency_a, fx.build_synthetic_20oct(), "20OCT.xlsx")
    rows = list_rows(m.get("/days").text)
    assert [row[0] for row in rows] == ["20 octobre 2026", "19 octobre 2026"]
    assert rows[0][1:] == ["Pas de ventes", "0", "0", "0", "0", "0", "Reçu"]
    assert rows[1][-1] == "Non reçu"
    # a sale on the 19th, and a report for the 19th too
    enter(m, tk("3940000012", "1000"), staff_sign=SIGN, day="2026-10-20", pb="1200")
    again = {row[0]: row for row in list_rows(m.get("/days").text)}
    assert again["20 octobre 2026"][1] == "Ouverte" and again["20 octobre 2026"][-1] == "Reçu"


def test_the_list_gives_staff_only_their_own_figures_and_days(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    fill_day(env)
    m = env.manager_a()
    enter(m, tk("3940000030", "900"), staff_sign=SIGN2, day=YESTERDAY, pb="1000")  # not theirs
    with owner_session(env.db.owner_url) as s:
        ingest_report(s, env.seeded.agency_a, fx.build_synthetic_20oct(), "20OCT.xlsx")
    html = env.staff_a().get("/days").text
    rows = list_rows(html)
    assert [row[0] for row in rows] == ["18 octobre 2026"]  # no own sale on the 17th or the 20th
    assert rows[0][1:] == ["Ouverte", "2", "2", "Non reçu"]  # no cost or P.B total (decision 26)
    assert fr(36368) not in html and fr(39500) not in html
    assert "20 octobre" not in html and "17 octobre" not in html


def test_the_list_of_another_agency_is_empty_and_never_leaks(env: Env) -> None:
    fill_day(env)
    html = env.manager_b().get("/days").text
    assert "Aucune journée pour le moment." in html and "/days/2026" not in html


def test_a_staff_member_without_a_linked_sign_sees_nothing_and_gets_no_error(env: Env) -> None:
    fill_day(env)
    add_user(env.db.owner_url, env.seeded.agency_a, "+22212345693", display_name="Sans signe")
    c = env.as_user("+22212345693", PASSWORD_S)
    assert "Aucune journée pour le moment." in c.get("/days").text
    page_ = c.get(f"/days/{TODAY}")
    assert page_.status_code == 200 and "Aucune vente ce jour." in page_.text
    assert D1 not in page_.text and not tree(page_.text).find_all("tbody", cls="sale-group")
    assert money_text(page_.text, "Billets") == "0"


def test_sales_for_days_matches_sales_for_day_day_by_day(env: Env) -> None:
    fill_day(env)
    m = env.manager_a()
    enter(m, tk("3940000040", "500"), staff_sign=SIGN2, day=YESTERDAY, pb="900")
    wanted = [date(2026, 10, 18), date(2026, 10, 17), date(2026, 10, 1)]
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        for user_id in (env.seeded.manager_a, env.seeded.staff_a):
            user = s.get(User, user_id)
            assert user is not None
            together = sales_service.sales_for_days_for(s, user, wanted)
            apart = [v for d in wanted for v in sales_service.sales_for_day_for(s, user, d)]
            assert sorted(v.id for v in together) == sorted(v.id for v in apart)
            assert sorted((v.id, v.pb, v.cost_total) for v in together) == sorted(
                (v.id, v.pb, v.cost_total) for v in apart
            )
        manager = s.get(User, env.seeded.manager_a)
        assert manager is not None
        assert sales_service.sales_for_days_for(s, manager, []) == []
        too_many = [date(2026, 1, 1) + timedelta(days=i) for i in range(367)]
        with pytest.raises(ValueError, match="366"):
            sales_service.sales_for_days_for(s, manager, too_many)


# --- close and reopen ---------------------------------------------------------------------------


def test_staff_cannot_close_or_reopen_and_it_is_audited(env: Env) -> None:
    fill_day(env)
    c = env.staff_a()
    assert c.get(f"/days/{TODAY}/close").status_code == 403
    r = post(c, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    assert r.status_code == 403
    assert (
        post(c, f"/days/{TODAY}/reopen", {"reason": "essai"}, via=f"/days/{TODAY}").status_code
        == 403
    )
    assert c.get(f"/days/{TODAY}/reopen").status_code == 403
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert len(denied) >= 3
    assert {r.status for r in table(env, ReportDay)} == {"open"}


@pytest.mark.usefixtures("gate_open")
def test_the_accountant_closes_then_the_manager_reopens_with_a_reason(env: Env) -> None:
    fill_day(env)
    acc = accountant(env)
    page = acc.get(f"/days/{TODAY}")
    assert "Clôturer la journée" in page.text and "Rouvrir" not in page.text
    confirm = acc.get(f"/days/{TODAY}/close")
    assert confirm.status_code == 200 and "Oui, clôturer" in confirm.text
    r = post(acc, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}/close")
    assert r.status_code == 303 and r.headers["location"] == f"/days/{TODAY}"
    closed = acc.get(f"/days/{TODAY}")
    assert "Clôturée" in closed.text and "Rouvrir" not in closed.text
    assert "Clôturer la journée" not in closed.text
    # closing twice is refused in French
    again = post(acc, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    assert again.status_code == 409 and "clôturée" in again.text
    # the accountant cannot reopen
    assert acc.get(f"/days/{TODAY}/reopen").status_code == 403
    m = env.manager_a()
    assert "Rouvrir la journée" in m.get(f"/days/{TODAY}").text
    assert m.get(f"/days/{TODAY}/reopen").status_code == 200
    r = post(
        m, f"/days/{TODAY}/reopen", {"reason": "erreur de saisie"}, via=f"/days/{TODAY}/reopen"
    )
    assert r.status_code == 303
    html = m.get(f"/days/{TODAY}").text
    text = text_of(html)
    assert "Ouverte" in text and "Clôturer la journée" in text
    kinds = [e.kind for e in table(env, DayEvent)]
    assert kinds == ["open", "close", "reopen"]
    history = html.split('id="events-title"')[1]
    assert "Clôturée" in history and "Rouverte" in history and "Ouverte" in history
    assert "Comptable A" in history and "Manager A" in history
    assert "erreur de saisie" in history


@pytest.mark.usefixtures("gate_open")
def test_reopening_without_a_real_reason_is_a_french_error_and_changes_nothing(env: Env) -> None:
    fill_day(env)
    acc = accountant(env)
    post(acc, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    m = env.manager_a()
    for reason in ("", "   ", "ok"):
        r = post(m, f"/days/{TODAY}/reopen", {"reason": reason}, via=f"/days/{TODAY}/reopen")
        assert r.status_code == 400, reason
        assert "motif" in r.text.lower() or "caractères" in r.text
    assert post(m, f"/days/{TODAY}/reopen", {}, via=f"/days/{TODAY}/reopen").status_code == 400
    assert [r.status for r in table(env, ReportDay)] == ["closed"]


def test_reopening_a_day_that_is_not_closed_is_refused(env: Env) -> None:
    fill_day(env)
    m = env.manager_a()
    r = post(m, f"/days/{TODAY}/reopen", {"reason": "pas besoin"}, via=f"/days/{TODAY}")
    assert r.status_code == 409 and "pas clôturée" in r.text
    assert m.get(f"/days/{TODAY}/reopen").status_code == 409


def test_a_closed_day_refuses_new_sales_until_reopened(env: Env) -> None:
    fill_day(env)
    post(env.manager_a(), f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    s = env.staff_a()
    r = post(s, "/sales", {"doc_number": [D1]}, via="/sales/new")
    assert r.status_code in (400, 409)


def test_closing_or_reopening_an_unknown_day_is_a_404(env: Env) -> None:
    m = env.manager_a()
    assert m.get(f"/days/{YESTERDAY}/close").status_code == 404
    assert post(m, f"/days/{YESTERDAY}/close", {}, via="/days").status_code == 404
    assert (
        post(m, f"/days/{YESTERDAY}/reopen", {"reason": "essai ok"}, via="/days").status_code == 404
    )
    assert m.get("/days/2026-10-19/close").status_code == 400  # future


def test_closing_another_agencys_day_is_a_404(env: Env) -> None:
    fill_day(env)
    b = env.manager_b()
    assert post(b, f"/days/{TODAY}/close", {}, via="/days").status_code == 404
    assert [r.status for r in table(env, ReportDay)] == ["open"]


@pytest.mark.parametrize("suffix", ["close", "reopen"])
def test_every_post_needs_the_csrf_token(env: Env, suffix: str) -> None:
    fill_day(env)
    m = env.manager_a()
    url = f"/days/{TODAY}/{suffix}"
    assert m.post(url, data={"reason": "essai sans jeton"}).status_code == 403
    assert m.post(url, data={"csrf_token": "x" * 40, "reason": "essai"}).status_code == 403
    token = csrf_for(m, "/days")
    foreign = m.post(
        url,
        data={"csrf_token": token, "reason": "essai"},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert [r.status for r in table(env, ReportDay)] == ["open"]


def test_every_post_route_of_the_day_pages_has_csrf_on_its_router() -> None:
    from fastapi.routing import APIRoute

    from app.web.csrf import csrf_protect
    from app.web.routes import days

    posts = {
        r.path
        for r in days.router.routes
        if isinstance(r, APIRoute) and "POST" in (r.methods or ())
    }
    assert posts == {"/days/{day}/close", "/days/{day}/reopen", "/days/{day}/partner-list"}
    assert any(d.dependency is csrf_protect for d in days.router.dependencies)


# --- escaping, headers, navigation --------------------------------------------------------------


NASTY = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'


@pytest.mark.usefixtures("gate_open")
def test_names_and_reasons_are_escaped(env: Env) -> None:
    m = env.manager_a()
    enter(m, tk(D1, pax_name="<b>X</b>"), staff_sign=SIGN, day=TODAY, clt=NASTY)
    post(m, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    post(m, f"/days/{TODAY}/reopen", {"reason": NASTY}, via=f"/days/{TODAY}/reopen")
    for path in (f"/days/{TODAY}", "/days"):
        html = m.get(path).text
        assert "<script>alert" not in html and "<img src=x" not in html, path
        assert "<b>X</b>" not in html
    assert "&lt;script&gt;" in m.get(f"/days/{TODAY}").text
    post(m, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    bad = post(m, f"/days/{TODAY}/reopen", {"reason": NASTY[:3]}, via=f"/days/{TODAY}/reopen")
    assert "<script>" not in bad.text


@pytest.mark.parametrize(
    "path", ["/days", "/days/{d}", "/days/{d}/close", "/days/2026-10-19", "/days/bad"]
)
def test_pages_are_never_cached_and_have_no_inline_style_or_script(env: Env, path: str) -> None:
    fill_day(env)
    c = env.manager_a()
    r = c.get(path.format(d=TODAY))
    assert r.status_code in (200, 400)
    assert r.headers["cache-control"] == "no-store"
    html = r.text
    assert not re.search(r"\sstyle\s*=", html, re.I) and "<style" not in html.lower()
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert '<html lang="fr"' in html and "content-security-policy" in r.headers


@pytest.mark.usefixtures("gate_open")
def test_the_reopen_page_and_post_answers_are_not_cached(env: Env) -> None:
    fill_day(env)
    m = env.manager_a()
    post(m, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    assert m.get(f"/days/{TODAY}/reopen").headers["cache-control"] == "no-store"
    bad = post(m, f"/days/{TODAY}/reopen", {"reason": "x"}, via=f"/days/{TODAY}/reopen")
    assert bad.headers["cache-control"] == "no-store"
    again = post(m, f"/days/{TODAY}/close", {}, via=f"/days/{TODAY}")
    assert again.headers["cache-control"] == "no-store"


def test_pages_need_a_login(env: Env) -> None:
    c = env.factory()
    for path in ("/days", f"/days/{TODAY}"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code in (303, 401), path


@pytest.mark.parametrize("who", ["staff", "manager", "accountant"])
def test_the_sidebar_has_journees_and_the_bottom_bar_keeps_five_tabs(env: Env, who: str) -> None:
    fill_day(env)
    c = {"staff": env.staff_a, "manager": env.manager_a}.get(who) or (lambda: accountant(env))
    client = c()
    for path in ("/", "/days", f"/days/{TODAY}", "/sales"):
        html = client.get(path).text
        assert 'href="/days"' in sidebar(html) and "Journées" in text_of(sidebar(html)), path
        assert "Journées" not in text_of(tabbar(html)), path
        assert tabbar(html).count('class="tab"') <= 5, (who, path)
    assert re.search(r'href="/days" aria-current="page"', client.get("/days").text)
    assert re.search(r'href="/days" aria-current="page"', client.get(f"/days/{TODAY}").text)


def test_the_day_page_is_linked_from_the_dashboard_and_from_the_sales_list(env: Env) -> None:
    fill_day(env)
    env.load()  # a report on 1 Oct gives the dashboards a day to show
    for c in (env.manager_a(), env.staff_a()):
        home = c.get("/").text
        assert re.search(r'href="/days/\d{4}-\d{2}-\d{2}"', home.split('class="content-col"')[1])
        sales_list = c.get(f"/sales?date={TODAY}").text
        assert f'href="/days/{TODAY}"' in sales_list


# --- rate limit ---------------------------------------------------------------------------------


@pytest.mark.usefixtures("gate_open")
def test_closing_and_reopening_share_a_per_user_limit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "DAY_ACTION_USER", ratelimit.Limit("day_action_user", 2, 3600))
    fill_day(env)
    m = env.manager_a()
    close, reopen = f"/days/{TODAY}/close", f"/days/{TODAY}/reopen"
    assert post(m, close, {}, via=f"/days/{TODAY}").status_code == 303
    assert post(m, reopen, {"reason": "erreur de saisie"}, via=reopen).status_code == 303
    r = post(m, close, {}, via=f"/days/{TODAY}")
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert [d.status for d in table(env, ReportDay)] == ["open"]
    other = accountant(env)  # another user has a budget of their own
    assert post(other, close, {}, via=f"/days/{TODAY}").status_code == 303


def test_the_default_limit_is_60_an_hour() -> None:
    assert (ratelimit.DAY_ACTION_USER.max_hits, ratelimit.DAY_ACTION_USER.window_seconds) == (
        60,
        3600,
    )
