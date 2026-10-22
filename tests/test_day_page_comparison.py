"""The comparison on the day page (B7 page, B8 numbers): manager and accountant versus staff."""

from __future__ import annotations

import re

import pytest

import app.flags as flags_module
from tests.fixtures import synthetic_20oct as fx
from tests.flag_support import FlagWeb
from tests.web_support import WebDb, csrf_for

URL = "/days/2026-10-20"


@pytest.fixture
def world(web_db: WebDb) -> FlagWeb:
    return FlagWeb(web_db)


def count(text: str, label: str) -> str:
    match = re.search(rf'{re.escape(label)}</dt><dd class="num">(\d+)</dd>', text)
    assert match, label
    return match.group(1)


def test_the_manager_sees_the_run_counts_and_can_run_it_again(world: FlagWeb) -> None:
    skip = [str(fx.LINES[0]["DOC NUMBER"])]
    world.sell_report(skip=skip)
    with world.manager() as c:
        assert world.upload(c).status_code == 303
        page = c.get(URL)
        assert page.status_code == 200
        assert count(page.text, "Billets identiques") == "33"
        assert count(page.text, "Écarts bloquants") == "1"
        assert count(page.text, "Billets annulés") == "9"
        assert 'href="/flags?date=2026-10-20"' in page.text
        assert "Voir les écarts" in page.text
        assert 'action="/flags/run?date=2026-10-20"' in page.text
        run = c.post(
            "/flags/run?date=2026-10-20",
            data={"csrf_token": csrf_for(c, URL)},
            follow_redirects=False,
        )
        assert run.status_code == 303


def test_staff_see_only_their_own_counts_and_no_rerun_button(world: FlagWeb) -> None:
    skip = [str(line["DOC NUMBER"]) for line in fx.LINES[:12] if line["TRNC"] == "TKTT"]
    world.sell_report(skip=skip)
    world.link_staff("0004SV")
    with world.manager() as c:
        world.upload(c)
    with world.staff() as c:
        page = c.get(URL)
    own = sum(
        1 for d in skip if str(next(x["AS"] for x in fx.LINES if x["DOC NUMBER"] == d)) == "0004SV"
    )
    assert "Billets identiques" not in page.text and "Billets déplacés" not in page.text
    assert count(page.text, "Écarts bloquants") == str(own)
    assert "Voir les écarts" in page.text
    assert "/flags/run" not in page.text


def test_no_comparison_card_without_an_amadeus_report(world: FlagWeb) -> None:
    world.sell_report()
    with world.manager() as c:
        page = c.get(URL)
    assert page.status_code == 200
    assert "Comparaison avec le rapport Amadeus" not in page.text


def test_a_report_that_was_not_compared_says_so(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("broken")

    monkeypatch.setattr(flags_module, "run_comparison", boom)
    world.sell_report()
    with world.manager() as c:
        world.upload(c)
        page = c.get(URL)
    assert "Pas encore comparée." in page.text
    assert "/flags/run?date=2026-10-20" in page.text
