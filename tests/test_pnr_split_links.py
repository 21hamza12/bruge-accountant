"""A multi-screen booking that lost a ticket (owner, 29 Oct 2026).

Three things in one real-shaped booking (made up: tests/fixtures/pnr/exchange_split_links.txt):

1. Amadeus shows ticket numbers as links; copying one breaks the line around it with blank lines
   (`` 36 FA PAX `` / blank / ``071-3940281552`` / blank / ``/ETET/...``). The reader rejoins it.
2. An item cut at the end of one screen (``.../512`` then ``)>``) and complete on the next is the
   same item, not "différent entre deux écrans"; the complete version is kept.
3. Conjunction tickets (``071-3940281214-15``) are read; the ticket is the first ten digits.

The two old conjunction tickets were exchanged (FO) for two new ones issued today: only the new
ones are proposed (tickets issued before today are not read, decision 25).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.parsing.pnr import parse_pnr_text
from tests.page_support import Env
from tests.test_sale_pages import SIGN, text_of
from tests.test_sale_paste import paste, values
from tests.test_sale_paste_batch import offered, offered_docs, open_proposal
from tests.web_support import WebDb

FIXTURE = Path(__file__).parent / "fixtures" / "pnr" / "exchange_split_links.txt"
OLD = ("3940281214", "3940281216")
NEW = ("3940281551", "3940281552")


def booking() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_every_ticket_is_read_and_nothing_is_reported_different() -> None:
    reading = parse_pnr_text(booking())
    assert [t.doc_number for t in reading.tickets] == [*OLD, *NEW]
    assert not any("différent entre deux écrans" in w for w in reading.warnings)
    assert not any("n'a pas pu être lue" in w for w in reading.warnings)


def test_the_ticket_split_by_the_link_is_complete() -> None:
    reading = parse_pnr_text(booking())
    (ticket,) = [t for t in reading.tickets if t.doc_number == NEW[1]]
    assert ticket.airline == "071" and ticket.cost is not None and str(ticket.cost) == "11583.00"
    assert ticket.issue_date == date(2026, 10, 27)
    assert ticket.issuing_office == "NKC262353"  # another office than the booking's
    assert ticket.iata == "51211845" and ticket.segments == "S3-4" and ticket.passenger == 2


def test_conjunction_tickets_are_read_by_their_first_number() -> None:
    reading = parse_pnr_text(booking())
    old = {t.doc_number: t for t in reading.tickets if t.doc_number in OLD}
    assert set(old) == set(OLD)
    assert old[OLD[0]].issue_date == date(2026, 10, 5) and old[OLD[0]].passenger == 1
    assert old[OLD[1]].passenger == 2
    assert reading.exchanges == OLD  # the FO lines name the original (conjunction) tickets


def test_a_real_difference_between_two_screens_still_warns() -> None:
    text = booking().replace(
        "/ETET/MRU11583.00/27OCT26/NKC262353/512\n       11845/S3-4/P2",
        "/ETET/MRU99999.00/27OCT26/NKC262353/512\n       11845/S3-4/P2",
    )
    reading = parse_pnr_text(text)
    assert "élément 36 différent entre deux écrans : vérifier" in reading.warnings


def test_the_cut_item_is_completed_with_a_soft_notice() -> None:
    reading = parse_pnr_text(booking())
    assert "élément 36 complété depuis l'écran suivant" in reading.warnings


def _two_screens(first: str, second: str, *, end: str = ">") -> str:
    head = "--- TST RLR ---\nRP/NKC262374/NKC262374            AB/SU  27OCT26/1517Z   ZQ9TST\n"
    return f"{head}{first}\n{end}\n{head}{second}\n>\n"


def test_two_complete_items_that_differ_by_a_last_digit_still_warn() -> None:
    # neither screen was cut after element 16: EUR100 and EUR1000 are a real conflict
    first = " 16 FE PAX VALID ON ET ONLY/EUR100\n 17 FV PAX ET/S3-4/P1-2"
    second = " 16 FE PAX VALID ON ET ONLY/EUR1000\n 17 FV PAX ET/S3-4/P1-2"
    for end in (">", ")>"):
        reading = parse_pnr_text(_two_screens(first, second, end=end))
        assert "élément 16 différent entre deux écrans : vérifier" in reading.warnings
        assert not any("complété" in w for w in reading.warnings)
    # the last item of a screen closed by ">" (the end of the display) was not cut either
    last = parse_pnr_text(_two_screens(" 16 FE PAX X/EUR100", " 16 FE PAX X/EUR1000"))
    assert "élément 16 différent entre deux écrans : vérifier" in last.warnings


def test_only_the_last_item_of_a_screen_cut_by_the_prompt_is_completed() -> None:
    first = " 15 FV PAX ET/S3-4/P1-2\n 16 FE PAX VALID ON ET ONLY/EUR10"
    second = " 16 FE PAX VALID ON ET ONLY/EUR1000\n 17 RM TEST"
    reading = parse_pnr_text(_two_screens(first, second, end=")>"))
    assert "élément 16 complété depuis l'écran suivant" in reading.warnings
    assert not any("différent" in w for w in reading.warnings)
    # the end of the pasted text cuts the last item too (here the later, shorter copy)
    head = "RP/NKC262374/NKC262374            AB/SU  27OCT26/1517Z   ZQ9TST\n"
    text = f"{head} 16 FE PAX X/EUR1000\n 17 RM TEST\n>\n{head} 16 FE PAX X/EUR1"
    reading = parse_pnr_text(text)
    assert "élément 16 complété depuis l'écran suivant" in reading.warnings
    assert not any("différent" in w for w in reading.warnings)


def test_a_blank_line_that_does_not_fit_the_grammar_is_not_rejoined() -> None:
    # the middle piece is not a ticket number: nothing is glued, the ticket is reported unread
    text = booking().replace("\n071-3940281552\n", "\nTEST 3940281552\n")
    reading = parse_pnr_text(text)
    assert [t.doc_number for t in reading.tickets] == [*OLD, NEW[0]]
    assert "Une ligne FA (billet) n'a pas pu être lue." in reading.warnings


def test_the_same_reading_from_a_screenshot_transcript() -> None:
    reading = parse_pnr_text(booking(), transcript=True)
    assert [t.doc_number for t in reading.tickets] == [*OLD, *NEW]
    assert not any("différent entre deux écrans" in w for w in reading.warnings)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    e.clock.now = datetime(2026, 10, 27, 15, 30, tzinfo=UTC)  # the new tickets' issue day
    return e


def test_both_new_tickets_are_proposed_and_the_old_exchanged_ones_are_not(env: Env) -> None:
    c = env.staff_a()
    r = paste(c, booking())
    assert r.status_code == 200, text_of(r.text)[:1500]
    assert offered_docs(r.text) == list(NEW)
    text = text_of(r.text)
    assert f"2 billet(s) émis avant aujourd'hui ignoré(s) : {', '.join(OLD)}" in text
    assert "différent entre deux écrans" not in text
    for proposal, doc in zip(offered(r.text), NEW, strict=True):
        assert values(open_proposal(c, proposal).text, "doc_number") == [doc]
