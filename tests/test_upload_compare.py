"""The upload runs the comparison; a closed day refuses the upload; a failed comparison keeps it."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import func, select

import app.flags as flags_module
from app import days
from app.models import ComparisonRun, RateLimitBucket, Upload
from tests.fixtures import synthetic_20oct as fx
from tests.flag_support import D20, NOW, FlagWeb
from tests.web_support import WebDb, audit_events, csrf_for, owner_session

SIGN_OF = {str(line["DOC NUMBER"]): str(line["AS"]) for line in fx.LINES}
CLOSED_MESSAGE = "Journée clôturée : demandez au gérant de la rouvrir"


@pytest.fixture
def world(web_db: WebDb) -> FlagWeb:
    return FlagWeb(web_db)


def tile(text: str, label: str) -> str:
    match = re.search(rf"{re.escape(label)}</dt><dd class=\"num\">(\d+)</dd>", text)
    assert match, f"no {label!r} in the page"
    return match.group(1)


def day_status(world: FlagWeb) -> str:
    from app.tenancy import rebind_tenant

    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        found = days.get_day(s, world.seeded.agency_a, D20)
        assert found is not None
        return found.status


def test_an_upload_of_equal_sales_is_compared_and_the_day_approved(world: FlagWeb) -> None:
    world.sell_report()
    with world.manager() as c:
        response = world.upload(c)
        assert response.status_code == 303
        page = c.get(response.headers["location"])
    text = page.text
    assert tile(text, "Billets identiques") == "34"
    assert tile(text, "Écarts bloquants") == "0"
    assert tile(text, "Billets annulés") == "9"
    assert tile(text, "Billets déplacés") == "0"
    assert tile(text, "Billets partenaires absents du rapport") == "0"
    assert 'href="/flags?date=2026-10-20"' in text
    assert "Comparaison à relancer" not in text
    assert day_status(world) == "approved"
    assert world.flags_rows() == []
    assert [
        e.details["blocking"]
        for e in audit_events(world.db.owner_url, world.seeded.agency_a, "comparison_run")
    ] == [0]


def test_a_changed_cost_is_one_blocking_flag_after_the_upload(world: FlagWeb) -> None:
    first_doc = fx.LINES[0]["DOC NUMBER"]
    world.sell_report(extra_cost={first_doc: 100})
    with world.manager() as c:
        page = c.get(world.upload(c).headers["location"])
    assert tile(page.text, "Billets identiques") == "33"
    assert tile(page.text, "Écarts bloquants") == "1"
    assert day_status(world) == "compared"
    (flag,) = world.flags_rows()
    assert (flag.kind, flag.field, flag.status) == ("different", "total_doc", "open")


def test_a_staff_upload_is_compared_too(world: FlagWeb) -> None:
    world.sell_report()
    with world.staff() as c:
        response = world.upload(c)
        assert response.status_code == 303
        page = c.get(response.headers["location"])
    assert page.status_code == 200
    # a staff upload compares, but never approves, and shows no run-wide numbers
    assert "Billets identiques" not in page.text and "Billets déplacés" not in page.text
    assert tile(page.text, "Écarts bloquants") == "0"
    assert day_status(world) == "compared"


def test_staff_see_counts_of_their_own_signs_only(world: FlagWeb) -> None:
    skip = [str(fx.LINES[i]["DOC NUMBER"]) for i in range(0, 12) if fx.LINES[i]["TRNC"] == "TKTT"]
    world.sell_report(skip=skip)
    world.link_staff("0004SV")
    own = sum(1 for d in skip if SIGN_OF[d] == "0004SV")
    with world.manager() as c:
        manager_page = c.get(world.upload(c).headers["location"])
    assert tile(manager_page.text, "Écarts bloquants") == str(len(skip))
    assert "Billets identiques" in manager_page.text
    with world.staff() as c:
        staff_page = c.get(world.upload(c, name="2.xlsx").headers["location"])
    assert tile(staff_page.text, "Écarts bloquants") == str(own)
    assert own < len(skip)


def test_an_identical_reupload_keeps_the_answer_to_a_flag(world: FlagWeb) -> None:
    missing = str(fx.LINES[0]["DOC NUMBER"])
    world.sell_report(skip=[missing])
    with world.manager() as c:
        world.upload(c)
        world.set_all_status("answered")
        again = c.get(world.upload(c, name="again.xlsx").headers["location"])
    rows = world.flags_rows()
    assert [(f.kind, f.status) for f in rows] == [("not_sent", "answered")]
    assert tile(again.text, "Écarts bloquants") == "1"


# --- a closed day -------------------------------------------------------------------------------


def close_day(world: FlagWeb) -> None:
    from app.models import User
    from app.tenancy import rebind_tenant

    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        assert manager is not None
        report_day = days.get_or_open_day(s, manager, D20, now=NOW)
        days.close_day(s, manager, report_day, now=NOW)
        s.commit()


def count(world: FlagWeb, model: type) -> int:
    from app.tenancy import rebind_tenant

    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        return s.scalar(select(func.count()).select_from(model)) or 0


def test_an_upload_for_a_closed_day_is_refused_and_nothing_is_stored(world: FlagWeb) -> None:
    close_day(world)
    with world.manager() as c:
        response = world.upload(c)
    assert response.status_code == 409
    assert CLOSED_MESSAGE in response.text
    assert count(world, Upload) == 0 and count(world, ComparisonRun) == 0
    failed = audit_events(world.db.owner_url, world.seeded.agency_a, "upload_failed")
    assert [e.details["reason"] for e in failed] == ["day_closed"]
    assert [set(e.details) for e in failed] == [{"filename", "reason"}]


def test_the_quota_is_given_back_when_the_day_is_closed(world: FlagWeb) -> None:
    close_day(world)
    with world.manager() as c:
        for _ in range(3):
            assert world.upload(c).status_code == 409
    with owner_session(world.db.owner_url) as s:
        hits = sum(b.count for b in s.scalars(select(RateLimitBucket)) if "upload" in b.key)
    assert hits == 0


def test_a_reopened_day_takes_the_upload_again(world: FlagWeb) -> None:
    from app.models import User
    from app.tenancy import rebind_tenant

    close_day(world)
    with owner_session(world.db.owner_url) as s:
        rebind_tenant(s, world.seeded.agency_a)
        manager = s.get(User, world.seeded.manager_a)
        found = days.get_day(s, world.seeded.agency_a, D20)
        assert manager is not None and found is not None
        days.reopen_day(s, manager, found, "rapport reçu en retard", now=NOW)
        s.commit()
    with world.manager() as c:
        assert world.upload(c).status_code == 303


# --- the comparison fails ----------------------------------------------------------------------


def test_a_failed_comparison_keeps_the_upload_and_asks_to_run_it_again(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    world.sell_report()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the comparison broke")

    with monkeypatch.context() as patch:
        patch.setattr(flags_module, "run_comparison", boom)
        with world.manager() as c:
            response = world.upload(c)
            assert response.status_code == 303
            page = c.get(response.headers["location"])
    assert count(world, Upload) == 1 and count(world, ComparisonRun) == 0
    assert "Comparaison à relancer" in page.text
    assert "/flags/run?date=2026-10-20" in page.text
    assert "comparison after upload failed" in caplog.text
    # run it again by hand: the page then shows the numbers
    with world.manager() as c:
        run = c.post(
            "/flags/run?date=2026-10-20",
            data={"csrf_token": csrf_for(c, "/flags")},
            follow_redirects=False,
        )
        assert run.status_code == 303
        again = c.get(response.headers["location"])
    assert "Comparaison à relancer" not in again.text
    assert tile(again.text, "Billets identiques") == "34"
    assert day_status(world) == "approved"


def test_a_staff_member_cannot_run_it_again(
    world: FlagWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the comparison broke")

    monkeypatch.setattr(flags_module, "run_comparison", boom)
    with world.staff() as c:
        page = c.get(world.upload(c).headers["location"])
    assert "Comparaison à relancer" in page.text
    assert "/flags/run" not in page.text
    assert "Demandez au gérant de relancer la comparaison." in page.text
