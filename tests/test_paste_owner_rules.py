"""Two rules of the owner (29 Oct, docs/design/02-open-decisions.md):

"if the client does not have a name put CLIENT and if the date is date older than the today date
dont read the ticket with the old date from the pasted or from the screenshot"

1. A direct sale without a client name is stored with the name « CLIENT » (form, paste,
   screenshot); a typed name is kept; a partner sale is unchanged (no client needed).
2. Reading a paste or a screenshot ignores the tickets issued before today, for everyone, and says
   which; the hand-typed form is unchanged.

Synthetic PNR texts only (tests/fixtures/pnr), made-up passengers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from app.ai.fake import FakeTranscribeClient
from app.models import ReportDay, Sale, SaleTicket, SaleVersion
from tests.acceptance_support import fields_of, override
from tests.ai_support import png
from tests.page_support import Env
from tests.test_sale_pages import SIGN, accountant, enter, make_partner, table, text_of, tk
from tests.test_sale_paste import one_passenger, paste, pnr, values
from tests.test_sale_paste_batch import offered, offered_docs, save
from tests.web_support import WebDb, csrf_for

DOCS = ("3940299481", "3940299482", "3940299483")
OLD_NOTICE = "billet(s) émis avant aujourd'hui ignoré(s)"


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    e.clock.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)  # the fixtures' issue day is today
    return e


def shoot(env: Env, c: TestClient, text: str) -> tuple[httpx.Response, FakeTranscribeClient]:
    fake = FakeTranscribeClient([text])
    env.factory.app.state.ai_client = fake  # type: ignore[attr-defined]
    response: httpx.Response = c.post(
        "/sales/screenshot",
        data={"csrf_token": csrf_for(c, "/sales/new")},
        files=[("images", ("page0.png", png(), "image/png"))],
        follow_redirects=False,
    )
    return response, fake


def confirm_form(c: TestClient, html: str, **fields: str) -> httpx.Response:
    """Answer and confirm the prefilled form of ``html`` (a direct sale unless told otherwise)."""
    data = override(fields_of(html, "/sales/preview"), payment_method="cash", **fields)
    readback = c.post("/sales/preview", data=data, follow_redirects=False)
    assert readback.status_code == 200, text_of(readback.text)[:1500]
    result: httpx.Response = c.post(
        "/sales", data=fields_of(readback.text, "/sales"), follow_redirects=False
    )
    return result


def stored_clients(env: Env) -> list[str | None]:
    return [v.clt for v in table(env, SaleVersion)]


def without_client(text: str) -> str:
    """The booking with no payer line: nothing to read a client name from."""
    return "\n".join(line for line in text.splitlines() if " FP " not in line)


def dated(text: str, doc: str, day: str) -> str:
    """The booking with the FA line of ticket ``doc`` issued on ``day`` (``20OCT26``)."""
    return text.replace(f"495-{doc}/ETL6/MRU5022.00/21OCT26", f"495-{doc}/ETL6/MRU5022.00/{day}")


# --- 1. the default client name ----------------------------------------------------------------


def test_a_blank_client_on_the_form_is_stored_as_client(env: Env) -> None:
    c = env.staff_a()
    blank = fields_of(c.get("/sales/new").text, "/sales/preview")
    data = override(
        blank,
        staff_sign=SIGN,
        doc_number="3940299401",
        airline="495",
        total_doc="5000",
        pax_name="TEST/PASSAGER",
        rloc="TEST12",
        ag="direct",
        pb="5400",
        clt="",
        payment_method="cash",
        client_refused_phone="1",
    )
    readback = c.post("/sales/preview", data=data)
    assert readback.status_code == 200, text_of(readback.text)[:1500]
    assert "CLIENT" in text_of(readback.text)
    done = c.post("/sales", data=fields_of(readback.text, "/sales"), follow_redirects=False)
    assert done.status_code == 303
    assert stored_clients(env) == ["CLIENT"]


def test_a_typed_client_name_is_kept(env: Env) -> None:
    enter(env.staff_a(), tk("3940299401"), clt="TEST CLIENT NOMME")
    assert stored_clients(env) == ["TEST CLIENT NOMME"]


def test_spaces_only_count_as_no_name(env: Env) -> None:
    enter(env.staff_a(), tk("3940299401"), clt="   ")
    assert stored_clients(env) == ["CLIENT"]


def test_a_partner_sale_still_needs_no_client_name(env: Env) -> None:
    pid = make_partner(env, "TEST PARTENAIRE", "500")
    enter(env.staff_a(), tk("3940299401"), ag=str(pid), clt="", pb="")
    assert stored_clients(env) == [None]


def test_a_paste_without_a_client_is_prefilled_and_stored_as_client(env: Env) -> None:
    c = env.staff_a()
    r = paste(c, without_client(one_passenger()))
    assert r.status_code == 200
    assert values(r.text, "clt") == ["CLIENT"]
    # even when the agent empties the field, the server stores « CLIENT »
    assert confirm_form(c, r.text, ag_answer="direct", clt="").status_code == 303
    assert stored_clients(env) == ["CLIENT"]


def test_a_paste_with_a_client_keeps_it(env: Env) -> None:
    c = env.staff_a()
    r = paste(c, one_passenger())
    assert values(r.text, "clt") == ["SYNTHCLIENT PAYEUR"]
    assert confirm_form(c, r.text, ag_answer="direct").status_code == 303
    assert stored_clients(env) == ["SYNTHCLIENT PAYEUR"]


def test_a_screenshot_without_a_client_is_stored_as_client(env: Env) -> None:
    c = env.staff_a()
    r, _ = shoot(env, c, without_client(one_passenger()))
    assert r.status_code == 200
    assert values(r.text, "clt") == ["CLIENT"]
    assert confirm_form(c, r.text, ag_answer="direct", clt="").status_code == 303
    assert stored_clients(env) == ["CLIENT"]


# --- 2. tickets issued before today are not read ------------------------------------------------


def test_a_mixed_paste_proposes_only_todays_tickets_and_names_the_old_one(env: Env) -> None:
    c = env.staff_a()
    r = paste(c, dated(pnr("three_adults_rm"), DOCS[0], "20OCT26"))
    assert r.status_code == 200
    text = text_of(r.text)
    assert offered_docs(r.text) == list(DOCS[1:])
    assert f"1 {OLD_NOTICE} : {DOCS[0]}" in text
    assert text.count(OLD_NOTICE) == 1  # one notice for the page, not one per proposal
    assert "SYNTHA/ONE" not in text


def test_the_deja_saisie_flow_works_on_the_filtered_list(env: Env) -> None:
    c = env.staff_a()
    first = paste(c, dated(pnr("three_adults_rm"), DOCS[0], "20OCT26"))
    r1 = save(c, offered(first.text)[0])
    assert r1.status_code == 200
    text = text_of(r1.text)
    assert text.count("déjà saisie") == 1 and text.count("Saisir cette vente") == 1
    assert f"1 {OLD_NOTICE} : {DOCS[0]}" in text  # still said after coming back
    r2 = save(c, offered(r1.text)[0])
    assert "Toutes les ventes de ce texte sont saisies" in text_of(r2.text)
    assert sorted(t.doc_number for t in table(env, SaleTicket)) == list(DOCS[1:])


def test_an_all_old_paste_proposes_nothing_and_only_says_so(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)  # the tickets were issued yesterday
    c = env.staff_a()
    r = paste(c, pnr("three_adults_rm"))
    assert r.status_code == 400
    text = text_of(r.text)
    assert f"3 {OLD_NOTICE} : {', '.join(DOCS)}" in text
    assert "Saisir cette vente" not in text and "Aucune vente lisible" not in text
    assert values(r.text, "doc_number") == [""]  # the blank form, nothing prefilled
    assert table(env, Sale) == []


def test_old_tickets_are_ignored_for_the_manager_and_the_accountant_too(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    for c in (env.manager_a(), accountant(env)):
        r = paste(c, pnr("three_adults_rm"))
        assert r.status_code == 400 and OLD_NOTICE in text_of(r.text)


def test_one_old_ticket_of_a_single_passenger_is_named_on_the_form(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    r = paste(env.staff_a(), one_passenger())
    assert r.status_code == 400
    assert f"1 {OLD_NOTICE} : {DOCS[0]}" in text_of(r.text)


def test_a_mixed_screenshot_is_filtered_the_same_way(env: Env) -> None:
    c = env.staff_a()
    r, fake = shoot(env, c, dated(pnr("three_adults_rm"), DOCS[2], "19OCT26"))
    assert r.status_code == 200
    assert offered_docs(r.text) == list(DOCS[:2])
    assert f"1 {OLD_NOTICE} : {DOCS[2]}" in text_of(r.text)
    r1 = save(c, offered(r.text)[0])
    assert text_of(r1.text).count("déjà saisie") == 1
    assert len(fake.calls) == 1


def test_an_all_old_screenshot_proposes_nothing(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
    r, _ = shoot(env, env.staff_a(), pnr("three_adults_rm"))
    assert r.status_code == 400
    assert f"3 {OLD_NOTICE}" in text_of(r.text)
    assert "Saisir cette vente" not in text_of(r.text)


def test_a_ticket_without_a_readable_date_is_still_proposed_for_today(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    text = one_passenger().replace("21OCT26/NKC", "99XYZ26/NKC")
    c = env.staff_a()
    r = paste(c, text)
    assert r.status_code == 200
    page = text_of(r.text)
    assert values(r.text, "doc_number") == [DOCS[0]]
    assert "Date d'émission illisible" in page and OLD_NOTICE not in page
    assert confirm_form(c, r.text, ag_answer="direct").status_code == 303
    assert {d.day for d in table(env, ReportDay)} == {datetime(2026, 10, 22).date()}


def test_the_hand_typed_form_still_takes_a_late_ticket(env: Env) -> None:
    env.clock.now = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
    manager = env.manager_a()
    sale_id = enter(manager, tk(DOCS[0]), staff_sign=SIGN, day="2026-10-20")
    assert sale_id > 0
    assert {d.day for d in table(env, ReportDay)} == {datetime(2026, 10, 20).date()}
