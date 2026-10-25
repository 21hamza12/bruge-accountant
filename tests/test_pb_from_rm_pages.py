"""P.B from the RM line when the AP agency is not a known partner, and the P.B input staying
visible while the agency question is open (synthetic booking)."""

from __future__ import annotations

import re

from app.web.sale_pages import IGNORED_PRICE
from tests.acceptance_support import fields_of, text_of
from tests.page_support import Env
from tests.test_sale_pages import env as env  # the fixture
from tests.test_sale_pages import make_partner
from tests.web_support import csrf_for

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
