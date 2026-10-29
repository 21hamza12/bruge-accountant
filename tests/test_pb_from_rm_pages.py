"""P.B from the RM line when the AP agency is not a known partner, and the P.B input staying
visible while the agency question is open (synthetic booking)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from app.web.sale_pages import IGNORED_PRICE
from tests.acceptance_support import fields_of, text_of
from tests.page_support import Env
from tests.test_sale_pages import env as base_env  # noqa: F401  (the fixture)
from tests.test_sale_pages import make_partner
from tests.web_support import csrf_for


@pytest.fixture
def env(base_env: Env) -> Env:  # noqa: F811
    """The booking below was issued on 25 October, and a paste only reads the tickets issued today
    (decision 25): the clock is that day."""
    base_env.clock.now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    return base_env


PASTE_UNKNOWN_AGENCY = """--- TST RLR ---
RP/NKC262353/NKC262353            SV/SU  25OCT26/1501Z   SYN7QA
  1.SYNTHX/PASSAGER
  2  L6 121 D 30OCT 5 LPANKC HK1          2230 2310   *1A/E*
  3 AP NKC +222 22000000 - AGENCE SYNTHESE VOYAGES - A.S.V. - A
  4 RM 29000 MRU
  5 FA PAX 495-3940299902/ETL6/MRU28803.00/25OCT26/NKC262353/51211845/S2
  6 FP CASH
>"""


def paste(env: Env, text: str) -> str:
    c = env.staff_a()
    r = c.post("/sales/paste", data={"pnr_text": text, "csrf_token": csrf_for(c, "/sales/new")})
    assert r.status_code == 200
    return r.text


def test_an_unknown_agency_still_gets_its_rm_price_and_the_price_input_shows(env: Env) -> None:
    html = paste(env, PASTE_UNKNOWN_AGENCY)
    assert 'id="ag_answer_direct"' in html  # the agent is asked: partner or direct
    assert re.search(r'id="pb"[^>]*value="29\s?000(,00|\.00)?"', html)
    assert "data-partner" not in html  # nothing proposed as a partner: no rule hides P.B yet


def test_a_proposed_partner_marks_its_yes_answer_so_the_price_hides_when_chosen(env: Env) -> None:
    make_partner(env, "REND", "500")
    html = paste(env, PASTE_UNKNOWN_AGENCY.replace("AGENCE SYNTHESE VOYAGES - A.S.V.", "REND"))
    assert re.search(r'<input id="ag_answer_yes"[^>]*\sdata-partner[\s>]', html)


def readback_for_partner(env: Env, pb: str | None) -> str:
    """Paste the unknown-agency booking, answer with a partner, optionally retype P.B, preview."""
    pid = make_partner(env, "BADI", "500")
    c = env.staff_a()
    html = c.post(
        "/sales/paste",
        data={"pnr_text": PASTE_UNKNOWN_AGENCY, "csrf_token": csrf_for(c, "/sales/new")},
    ).text
    data = fields_of(html, "/sales/preview")
    data.update({"ag_answer": ["other"], "ag_other": [str(pid)], "payment_method": ["cash"]})
    if pb is not None:
        data["pb"] = [pb]
    r = c.post("/sales/preview", data=data)
    assert r.status_code == 200, text_of(r.text)[:800]
    return text_of(r.text)


def test_choosing_a_partner_after_the_rm_price_raises_no_false_alarm(env: Env) -> None:
    assert IGNORED_PRICE not in readback_for_partner(env, None)


def test_a_price_the_agent_typed_for_a_partner_sale_is_still_called_out(env: Env) -> None:
    assert IGNORED_PRICE in readback_for_partner(env, "31000")


# --- the price line at the top of a prefilled sale ----------------------------------------------


def price_line(html: str) -> str:
    m = re.search(r'<p class="notice price-read" id="price-read"[^>]*>(.*?)</p>', html, re.S)
    assert m is not None, "no price line"
    return text_of(m.group(1))


def test_the_top_line_shows_the_rm_price_and_the_amadeus_cost(env: Env) -> None:
    line = price_line(paste(env, PASTE_UNKNOWN_AGENCY))
    assert re.search(r"P\.B lu sur la ligne RM : 29\s000 MRU", line)
    assert re.search(r"Coût Amadeus : 28\s803 MRU", line)


def test_the_top_line_says_when_there_is_no_rm_price(env: Env) -> None:
    line = price_line(paste(env, PASTE_UNKNOWN_AGENCY.replace("  4 RM 29000 MRU\n", "")))
    assert "aucun prix sur la ligne RM" in line and "Coût Amadeus" in line


def test_no_price_line_on_a_plain_form(env: Env) -> None:
    assert 'id="price-read"' not in env.staff_a().get("/sales/new").text
