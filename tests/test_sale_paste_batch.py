"""A booking of several passengers is entered one sale after the other WITHOUT pasting it again.

After a sale that came from a multi-sale paste (or screenshot) is confirmed, the agent is back on
the list of that paste's proposals: the ones already in a sale are « déjà saisie » (decided on the
server from the ticket numbers), the others keep their « Saisir cette vente » button.

The proposals travel in a signed hidden field of the pages (never a URL, never the database or the
logs). Synthetic PNR texts only (tests/fixtures/pnr), made-up passengers.
"""

from __future__ import annotations

import base64
import logging
import re
from datetime import UTC, date, datetime
from html import unescape
from html.parser import HTMLParser

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.ai.fake import FakeTranscribeClient
from app.models import ReportDay, Sale, SaleTicket, SaleVersion
from tests.acceptance_support import fields_of, override
from tests.ai_support import png
from tests.page_support import Env
from tests.test_sale_pages import SIGN, enter, staff2, table, text_of, tk
from tests.test_sale_paste import no_inline_code, paste, pnr, values
from tests.web_support import WebDb, csrf_for, owner_session

BATCH = "paste_batch"
DOCS = ("3940299481", "3940299482", "3940299483")
NAMES = ("SYNTHA/ONE", "SYNTHB/TWO", "SYNTHC/THREE")
ISSUED = date(2026, 10, 21)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    e.clock.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # the issue day (decision 25)
    return e


class _Forms(HTMLParser):
    """Every form of a page posting to ``action``: its fields, name by name, in page order."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.current: dict[str, list[str]] | None = None
        self.forms: list[dict[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.current = {} if a.get("action") == self.action else None
        elif tag == "input" and self.current is not None and a.get("name"):
            self.current.setdefault(a["name"], []).append(unescape(a.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.current is not None:
            self.forms.append(self.current)
            self.current = None


def forms_to(html: str, action: str) -> list[dict[str, list[str]]]:
    parser = _Forms(action)
    parser.feed(html)
    return parser.forms


def offered(html: str) -> list[dict[str, list[str]]]:
    """The « Saisir cette vente » forms of the proposals page, in page order."""
    return forms_to(html, "/sales/new")


def offered_docs(html: str) -> list[str]:
    return [form["doc_number"][0] for form in offered(html)]


def save(c: TestClient, proposal: dict[str, list[str]]) -> httpx.Response:
    """Open one proposal, answer the agency question, read back, confirm (as a browser would)."""
    opened = c.post("/sales/new", data=proposal, follow_redirects=False)
    assert opened.status_code == 200, text_of(opened.text)[:1500]
    data = override(
        fields_of(opened.text, "/sales/preview"),
        ag_answer="direct",
        payment_method="cash",
    )
    readback = c.post("/sales/preview", data=data, follow_redirects=False)
    assert readback.status_code == 200, text_of(readback.text)[:1500]
    confirm = fields_of(readback.text, "/sales")
    result: httpx.Response = c.post("/sales", data=confirm, follow_redirects=False)
    return result


def sale_of(env: Env, doc: str) -> int:
    for ticket in table(env, SaleTicket):
        if ticket.doc_number == doc:
            return int(ticket.sale_id)
    raise AssertionError(f"no sale holds {doc}")


def assert_back_on_the_list(r: httpx.Response, done: int, left: int) -> str:
    assert r.status_code == 200, text_of(r.text)[:1500]
    assert r.headers["cache-control"] == "no-store"
    text = text_of(r.text)
    assert "Vente enregistrée" in text
    assert text.count("déjà saisie") == done
    assert text.count("Saisir cette vente") == left
    assert "Terminé" in text and "Nouveau collage" in text
    assert no_inline_code(r.text)
    return text


def walk_three(env: Env, c: TestClient, first: httpx.Response) -> None:
    """Save the three proposals one after the other, checking the list each time."""
    assert first.status_code == 200
    assert offered_docs(first.text) == list(DOCS)
    assert all(form.get(BATCH) for form in offered(first.text))

    r1 = save(c, offered(first.text)[0])
    text = assert_back_on_the_list(r1, done=1, left=2)
    s1 = sale_of(env, DOCS[0])
    assert f'href="/sales/{s1}"' in r1.text
    assert offered_docs(r1.text) == list(DOCS[1:])
    assert all(name in text for name in NAMES)

    r2 = save(c, offered(r1.text)[0])
    assert_back_on_the_list(r2, done=2, left=1)
    assert offered_docs(r2.text) == [DOCS[2]]

    r3 = save(c, offered(r2.text)[0])
    text = assert_back_on_the_list(r3, done=3, left=0)
    assert "Toutes les ventes de ce texte sont saisies" in text
    assert offered(r3.text) == []

    assert sorted(t.doc_number for t in table(env, SaleTicket)) == list(DOCS)
    assert len(table(env, Sale)) == 3
    # the signed issue date kept working for every proposal: each sale is filed under the 21st
    assert {d.day for d in table(env, ReportDay)} == {ISSUED}


# --- the cycle ------------------------------------------------------------------------------------


def test_a_three_sale_paste_is_entered_without_pasting_again(env: Env) -> None:
    c = env.staff_a()
    walk_three(env, c, paste(c, pnr("three_adults_rm")))


def test_a_three_sale_screenshot_is_entered_without_calling_the_ai_again(env: Env) -> None:
    fake = FakeTranscribeClient([pnr("three_adults_rm")])
    env.factory.app.state.ai_client = fake  # type: ignore[attr-defined]
    c = env.staff_a()
    first = c.post(
        "/sales/screenshot",
        data={"csrf_token": csrf_for(c, "/sales/new")},
        files=[("images", ("page0.png", png(), "image/png"))],
        follow_redirects=False,
    )
    assert len(fake.calls) == 1
    assert offered(first.text)[0]["source"] == ["screenshot"]
    walk_three(env, c, first)
    assert len(fake.calls) == 1  # the proposals came back from the page, not from the AI
    sources = {v.source for v in table(env, SaleVersion)}
    assert sources == {"screenshot"}


def test_the_form_of_a_proposal_leads_back_to_the_list(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    opened = c.post("/sales/new", data=offered(first.text)[1], follow_redirects=False)
    assert opened.status_code == 200
    assert values(opened.text, "pax_name") == [NAMES[1]]
    assert values(opened.text, "staff_sign") == [SIGN]  # their own sign, as before
    assert no_inline_code(opened.text)
    (back,) = forms_to(opened.text, "/sales/paste/list")
    listed = c.post("/sales/paste/list", data=back, follow_redirects=False)
    assert listed.status_code == 200
    assert offered_docs(listed.text) == list(DOCS)
    assert "Vente enregistrée" not in text_of(listed.text)
    # « Modifier » on the read-back keeps the list too
    data = override(fields_of(opened.text, "/sales/preview"), ag_answer="direct")
    data = override(data, payment_method="cash")
    readback = c.post("/sales/preview", data=data, follow_redirects=False)
    assert fields_of(readback.text, "/sales/new")[BATCH] == back[BATCH]
    assert fields_of(readback.text, "/sales")[BATCH] == back[BATCH]


def test_one_passenger_has_no_list_and_redirects_as_before(env: Env) -> None:
    from tests.test_sale_paste import one_passenger

    c = env.staff_a()
    r = paste(c, one_passenger())
    assert BATCH not in r.text
    data = override(fields_of(r.text, "/sales/preview"), ag_answer="direct", payment_method="cash")
    readback = c.post("/sales/preview", data=data, follow_redirects=False)
    done = c.post("/sales", data=fields_of(readback.text, "/sales"), follow_redirects=False)
    assert done.status_code == 303 and re.fullmatch(r"/sales/\d+", done.headers["location"])


# --- « déjà saisie » is the server's, from the ticket numbers ------------------------------------


def test_a_ticket_already_in_a_sale_is_marked_even_on_the_first_reading(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(DOCS[1], cost="5022", airline="495", pax_name=NAMES[1]))
    r = paste(c, pnr("three_adults_rm"))
    text = text_of(r.text)
    assert text.count("déjà saisie") == 1 and text.count("Saisir cette vente") == 2
    assert offered_docs(r.text) == [DOCS[0], DOCS[2]]
    assert f'href="/sales/{sale_id}"' in r.text


def test_a_sale_of_another_agent_is_marked_without_a_link(env: Env) -> None:
    other = staff2(env)
    sale_id = enter(
        other, tk(DOCS[0], cost="5022", airline="495", pax_name=NAMES[0]), staff_sign="0006AC"
    )
    r = paste(env.staff_a(), pnr("three_adults_rm"))
    text = text_of(r.text)
    assert "déjà saisie par un autre agent" in text
    assert f"/sales/{sale_id}" not in r.text  # staff only ever see their own signs
    assert offered_docs(r.text) == list(DOCS[1:])
    # the manager sees every sign: the link is there for them
    manager = paste(env.manager_a(), pnr("three_adults_rm"))
    assert f'href="/sales/{sale_id}"' in manager.text


def test_a_proposal_already_saved_cannot_be_saved_twice(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    stale = offered(first.text)[0]
    opened = c.post("/sales/new", data=stale, follow_redirects=False)
    data = override(fields_of(opened.text, "/sales/preview"), ag_answer="direct")
    readback = c.post("/sales/preview", data=override(data, payment_method="cash"))
    confirm = fields_of(readback.text, "/sales")
    assert c.post("/sales", data=confirm).status_code == 200
    # an old copy of the page still has the button: the read-back refuses it
    again = c.post("/sales/new", data=stale, follow_redirects=False)
    data = override(fields_of(again.text, "/sales/preview"), ag_answer="direct")
    refused = c.post("/sales/preview", data=override(data, payment_method="cash"))
    assert refused.status_code == 409 and "déjà enregistré" in text_of(refused.text)
    # and so does the confirm, once the double-submit window is over
    env.clock.now = datetime(2026, 10, 21, 12, 30, tzinfo=UTC)
    late = c.post("/sales", data=confirm, follow_redirects=False)
    assert late.status_code == 409 and "déjà enregistré" in text_of(late.text)
    assert len(table(env, Sale)) == 1


def test_a_double_submit_of_the_confirm_shows_the_list_again(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    opened = c.post("/sales/new", data=offered(first.text)[0], follow_redirects=False)
    data = override(fields_of(opened.text, "/sales/preview"), ag_answer="direct")
    readback = c.post("/sales/preview", data=override(data, payment_method="cash"))
    confirm = fields_of(readback.text, "/sales")
    assert c.post("/sales", data=confirm).status_code == 200
    twice = c.post("/sales", data=confirm, follow_redirects=False)
    assert_back_on_the_list(twice, done=1, left=2)
    assert len(table(env, Sale)) == 1


# --- tampering ---------------------------------------------------------------------------------


def _tamper(token: str) -> str:
    body, _, mac = token.partition(".")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    forged = raw.replace(b"SYNTHB/TWO", b"SYNTHX/EVIL")
    assert forged != raw
    return base64.urlsafe_b64encode(forged).decode().rstrip("=") + "." + mac


def test_a_tampered_list_is_dropped_and_the_sale_is_still_validated(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    proposal = offered(first.text)[0]
    forged = _tamper(proposal[BATCH][0])
    # the list page refuses it
    listed = c.post("/sales/paste/list", data=override(proposal, paste_batch=forged))
    assert listed.status_code == 400
    assert "SYNTHX/EVIL" not in listed.text
    # the sale itself is the agent's own, validated as always; without a valid list it redirects
    r = save(c, override(proposal, paste_batch=forged))
    assert r.status_code == 303 and re.fullmatch(r"/sales/\d+", r.headers["location"])
    assert "SYNTHX/EVIL" not in str([v.tickets for v in table(env, SaleVersion)])


def test_a_garbled_or_oversized_list_is_ignored(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    proposal = offered(first.text)[0]
    for bad in ("", "garbage", "a.b", "x" * 300_000, proposal[BATCH][0] + "0"):
        listed = c.post("/sales/paste/list", data=override(proposal, paste_batch=bad))
        assert listed.status_code == 400


def test_the_list_of_one_agent_is_useless_to_another(env: Env) -> None:
    first = paste(env.staff_a(), pnr("three_adults_rm"))
    proposal = offered(first.text)[0]
    other = staff2(env)
    token = csrf_for(other, "/sales/new")
    listed = other.post(
        "/sales/paste/list",
        data=override(proposal, csrf_token=token),
        follow_redirects=False,
    )
    assert listed.status_code == 400
    assert NAMES[1] not in listed.text


def test_a_forged_issue_date_in_a_proposal_is_dropped_for_staff(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    proposal = override(offered(first.text)[0], issue_date="2026-10-01")
    r = save(c, proposal)
    assert r.status_code == 200
    # the signature did not match the date: the sale goes to today, never to the 1st
    assert {d.day for d in table(env, ReportDay)} == {ISSUED}  # today, the 21st


def test_every_post_needs_the_csrf_token(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    proposal = {k: v for k, v in offered(first.text)[0].items() if k != "csrf_token"}
    assert c.post("/sales/paste/list", data=proposal).status_code == 403


# --- privacy ------------------------------------------------------------------------------------


def _all_text_in_the_database(env: Env) -> str:
    """Every value of every row of every table, as one string."""
    found: list[str] = []
    with owner_session(env.db.owner_url) as s:
        for table_name in inspect(s.get_bind()).get_table_names():
            query = text(f'SELECT * FROM "{table_name}"')  # noqa: S608 - names from the schema
            for row in s.execute(query):
                found.extend(str(value) for value in row)
    return "\n".join(found)


def test_nothing_of_the_paste_reaches_a_url_a_log_or_the_database(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    c = env.staff_a()
    locations: list[str] = []

    def keep(response: httpx.Response) -> None:
        if "location" in response.headers:
            locations.append(response.headers["location"])
        locations.append(str(response.request.url))

    c.event_hooks["response"] = [keep]
    with caplog.at_level(logging.DEBUG):
        first = paste(c, pnr("three_adults_rm"))
        token = offered(first.text)[0][BATCH][0]
        r1 = save(c, offered(first.text)[0])
        r2 = save(c, offered(r1.text)[0])
    assert r2.status_code == 200
    # the message and every extra field of every record (the JSON log lines are built from them)
    logged = caplog.text + " ".join(str(vars(record)) for record in caplog.records)
    for secret in (*NAMES, *DOCS, "SYNTHCLIENT", "ZX6AGG", "RP/NKC262213", token[:40]):
        assert secret not in logged, secret
        assert all(secret not in url for url in locations), secret
    assert all(BATCH not in url and "?" not in url for url in locations)
    stored = _all_text_in_the_database(env)
    assert NAMES[2] not in stored and DOCS[2] not in stored  # the passenger not entered yet
    assert token[:40] not in stored and "RP/NKC262213" not in stored and "FA PAX" not in stored


def test_the_list_pages_have_no_inline_script_or_style(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, pnr("three_adults_rm"))
    assert no_inline_code(first.text)
    assert 'href="/sales"' in first.text and 'href="/sales/new"' in first.text
