"""app/flags.py: running the comparison for a day and keeping its flags (B8 wiring).

Runs on SQLite through the service on the made-up 20 October report. The partial unique index
needs Postgres (tests/test_migration_0011.py checks it there).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import days, flags, sales
from app.models import (
    AuditEvent,
    ComparisonFlag,
    ComparisonRun,
    Sale,
)
from tests.fixtures import synthetic_20oct as fx
from tests.flag_support import D19, D20, NOW, FlagWorld

TKTT_INDEXES = [i for i, line in enumerate(fx.LINES) if line["TRNC"] == "TKTT"]


@pytest.fixture
def w(session: Session) -> FlagWorld:
    return FlagWorld(session)


def loaded(w: FlagWorld) -> int:
    """Upload the report and sell every issued line as it is; returns the upload id."""
    upload = w.upload()
    w.sell_all_issued(upload.upload_id)
    return upload.upload_id


def doc_at(w: FlagWorld, upload_id: int, index: int) -> str:
    return w.lines(upload_id)[index].doc_number


# --- the happy path -----------------------------------------------------------------------------


def test_equal_sales_and_report_give_no_flag_and_an_approved_day(w: FlagWorld) -> None:
    upload_id = loaded(w)
    run = w.run()
    assert (run.matched_count, run.flag_count, run.blocking_count) == (34, 0, 0)
    assert (run.cancelled_count, run.move_count, run.partner_missing_count) == (9, 0, 0)
    assert (run.agency_id, run.upload_id, run.started_by_user_id) == (
        w.agency_id,
        upload_id,
        w.manager.id,
    )
    day = w.day()
    assert day.status == "approved" and day.compared_upload_id == upload_id
    assert w.all_flags() == []
    events = days.day_events(w.db, w.agency_id, day.id)
    assert [(e.kind, e.from_status, e.to_status) for e in events] == [
        ("open", None, "open"),
        ("approve", "open", "approved"),
    ]


def test_a_changed_cost_is_one_different_flag_that_needs_validation(w: FlagWorld) -> None:
    upload = w.upload()
    lines = w.lines(upload.upload_id)
    for line in lines:
        if line.trnc == "TKTT":
            cost = w.cost_of(line) + 100 if line is lines[TKTT_INDEXES[2]] else w.cost_of(line)
            w.sell([w.ticket_of(line, total_doc=cost)], sign=line.staff_sign)
    run = w.run()
    assert (run.matched_count, run.flag_count, run.blocking_count) == (33, 1, 1)
    (flag,) = w.live()
    target = lines[TKTT_INDEXES[2]]
    assert (flag.kind, flag.field, flag.status) == ("different", "total_doc", "open")
    assert flag.needs_validation and flag.blocking
    assert flag.doc_number == target.doc_number
    assert Decimal(flag.app_value or "0") == w.cost_of(target) + 100
    assert Decimal(flag.amadeus_value or "0") == w.cost_of(target)
    assert flag.responsible_sign == target.staff_sign
    assert flag.sale_id is not None and flag.amadeus_seq == target.seq_order
    assert flag.run_id == run.id
    assert w.day().status == "compared"


def test_a_name_difference_needs_no_validation(w: FlagWorld) -> None:
    upload = w.upload()
    for line in w.lines(upload.upload_id):
        if line.trnc == "TKTT":
            name = "SYNTHETIC/OTHER" if line.seq_order == 1 else line.pax_name
            w.sell([w.ticket_of(line, pax_name=name)], sign=line.staff_sign)
    w.run()
    (flag,) = w.live()
    assert (flag.field, flag.needs_validation) == ("pax_name", False)


def test_a_ticket_missing_from_the_app_is_not_sent_for_its_amadeus_sign(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    missing = doc_at(w, upload_id, TKTT_INDEXES[0])
    w.sell_all_issued(upload_id, skip=[missing])
    run = w.run()
    assert (run.matched_count, run.flag_count, run.blocking_count) == (33, 1, 1)
    (flag,) = w.live()
    line = w.lines(upload_id)[TKTT_INDEXES[0]]
    assert (flag.kind, flag.field, flag.doc_number) == ("not_sent", None, missing)
    assert flag.responsible_sign == line.staff_sign and flag.sale_id is None
    assert (flag.app_value, flag.amadeus_value) == (None, "TKTT")
    assert flag.blocking and not flag.needs_validation
    assert w.day().status == "compared"


def test_an_app_only_direct_sale_is_not_in_amadeus(w: FlagWorld) -> None:
    upload_id = loaded(w)
    extra = w.ticket_of(w.lines(upload_id)[0], doc_number="3949999999")
    sale = w.sell([extra], sign="0006AC")
    run = w.run()
    assert (run.flag_count, run.blocking_count) == (1, 1)
    (flag,) = w.live()
    assert (flag.kind, flag.doc_number, flag.sale_id) == ("not_in_amadeus", "3949999999", sale.id)
    assert flag.responsible_sign == "0006AC" and flag.blocking
    assert w.day().status == "compared"


def test_a_partner_ticket_missing_from_the_report_does_not_block(w: FlagWorld) -> None:
    upload_id = loaded(w)
    extra = w.ticket_of(w.lines(upload_id)[0], doc_number="3949999998")
    w.sell([extra], partner=True)
    run = w.run()
    assert (run.flag_count, run.blocking_count, run.partner_missing_count) == (0, 0, 1)
    assert w.day().status == "approved"
    summary = flags.day_summary(w.db, w.manager, w.day())
    assert [t.doc_number for t in summary.partner_not_in_report] == ["3949999998"]
    assert len(summary.cancelled) == 9 and summary.flags == ()


def test_a_partner_ticket_that_is_in_the_report_but_differs_still_blocks(w: FlagWorld) -> None:
    upload = w.upload()
    lines = w.lines(upload.upload_id)
    first = True
    for line in lines:
        if line.trnc != "TKTT":
            continue
        if first:
            w.sell(
                [w.ticket_of(line, total_doc=w.cost_of(line) + 1)],
                partner=True,
                sign=line.staff_sign,
            )
            first = False
        else:
            w.sell([w.ticket_of(line)], sign=line.staff_sign)
    run = w.run()
    assert (run.flag_count, run.blocking_count) == (1, 1)


def test_the_summary_lists_the_cancelled_lines(w: FlagWorld) -> None:
    loaded(w)
    w.run()
    summary = flags.day_summary(w.db, w.manager, w.day())
    assert summary.run is not None and summary.run.cancelled_count == 9
    assert len(summary.cancelled) == 9
    assert {t.trnc for t in summary.cancelled} == {"CANX"}
    staff = flags.day_summary(w.db, w.staff, w.day())
    assert staff.cancelled and all(t.staff_sign == "0004SV" for t in staff.cancelled)


# --- re-running ---------------------------------------------------------------------------------


def test_a_second_run_with_nothing_changed_keeps_every_status(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    missing = doc_at(w, upload_id, TKTT_INDEXES[0])
    w.sell_all_issued(upload_id, skip=[missing])
    first = w.run()
    (flag,) = w.live()
    w.set_status(flag.id, "answered")
    second = w.run()
    assert second.id != first.id
    rows = w.all_flags()
    assert [(f.id, f.status) for f in rows] == [(flag.id, "answered")]
    assert second.flag_count == 1 and second.blocking_count == 1
    assert w.db.scalar(select(func.count()).select_from(ComparisonRun)) == 2
    assert w.day().status == "compared"  # an answered flag still blocks


def test_a_resolved_flag_does_not_block_the_day(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    missing = doc_at(w, upload_id, TKTT_INDEXES[0])
    w.sell_all_issued(upload_id, skip=[missing])
    w.run()
    (flag,) = w.live()
    w.set_status(flag.id, "resolved")
    w.run()
    assert [f.status for f in w.all_flags()] == ["resolved"]
    assert w.day().status == "approved"


def test_an_identical_reupload_keeps_the_decisions(w: FlagWorld) -> None:
    data = fx.build_synthetic_20oct()  # built once: an .xlsx carries its creation time
    upload_id = w.upload(data).upload_id
    missing = doc_at(w, upload_id, TKTT_INDEXES[0])
    w.sell_all_issued(upload_id, skip=[missing])
    w.run()
    (flag,) = w.live()
    w.set_status(flag.id, "answered")
    again = w.upload(
        data, name="20OCT-bis.xlsx"
    )  # the same bytes: the line set is the first upload's
    assert again.upload_id != upload_id
    run = w.run()
    assert run.upload_id == upload_id
    assert [(f.id, f.status) for f in w.all_flags()] == [(flag.id, "answered")]


def test_a_reupload_with_a_changed_value_supersedes_the_old_flag(w: FlagWorld) -> None:
    first = w.upload()
    lines = w.lines(first.upload_id)
    target = lines[TKTT_INDEXES[1]]
    for line in lines:
        if line.trnc == "TKTT":
            cost = w.cost_of(line) + 50 if line.id == target.id else w.cost_of(line)
            w.sell([w.ticket_of(line, total_doc=cost)], sign=line.staff_sign)
    w.run()
    (old,) = w.live()
    w.set_status(old.id, "answered")
    second = w.upload(w.changed_20oct(TKTT_INDEXES[1], **{"TOTAL DOC": int(w.cost_of(target)) - 7}))
    assert second.upload_id != first.upload_id
    run = w.run()
    assert run.upload_id == second.upload_id
    rows = w.all_flags()
    assert len(rows) == 2
    by_id = {f.id: f for f in rows}
    assert by_id[old.id].status == "superseded"
    (new,) = w.live()
    assert new.id != old.id and new.status == "open" and new.run_id == run.id
    assert Decimal(new.amadeus_value or "0") == w.cost_of(target) - 7
    assert w.day().status == "compared" and w.day().compared_upload_id == second.upload_id


def test_a_flag_that_no_longer_appears_is_superseded_and_the_day_approves(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    lines = w.lines(upload_id)
    target = lines[TKTT_INDEXES[3]]
    made = {}
    for line in lines:
        if line.trnc == "TKTT":
            cost = w.cost_of(line) + 5 if line.id == target.id else w.cost_of(line)
            made[line.id] = w.sell([w.ticket_of(line, total_doc=cost)], sign=line.staff_sign)
    w.run()
    (flag,) = w.live()
    assert w.day().status == "compared"
    sale = made[target.id]
    sales.edit_sale(
        w.db,
        w.manager,
        sale.id,
        _same_sale(w, target),
        "correction du coût",
        now=NOW,
    )
    w.db.commit()
    run = w.run()
    assert run.flag_count == 0
    assert [(f.id, f.status) for f in w.all_flags()] == [(flag.id, "superseded")]
    assert w.day().status == "approved"


def _same_sale(w: FlagWorld, line: Any) -> Any:
    from app.sales import SaleInput

    return SaleInput(
        tickets=[w.ticket_of(line)],
        ag_kind="direct",
        pb="99999",
        clt="Client Synth",
        payment_method="cash",
        staff_sign=line.staff_sign,
    )


def test_an_approved_day_goes_back_to_compared_when_a_new_flag_appears(w: FlagWorld) -> None:
    upload_id = loaded(w)
    w.run()
    assert w.day().status == "approved"
    w.sell([w.ticket_of(w.lines(upload_id)[0], doc_number="3949999997")])
    w.run()
    assert w.day().status == "compared"
    kinds = [(e.kind, e.to_status) for e in days.day_events(w.db, w.agency_id, w.day().id)]
    assert kinds[-2:] == [("approve", "approved"), ("compare", "compared")]


# --- tickets of other days ----------------------------------------------------------------------


def _earlier_day_with_ticket(w: FlagWorld, upload_id: int) -> tuple[str, Sale | Any]:
    """Day 19 holds a sale of a ticket that the 20 report lists; day 19 has its own report."""
    moved = w.lines(upload_id)[TKTT_INDEXES[0]]
    sale = w.sell([w.ticket_of(moved)], sign=moved.staff_sign, day=D19)
    other = [dict(fx.LINES[TKTT_INDEXES[5]])]
    other[0]["DOC NUMBER"] = "1993333333"
    w.upload(w.report(other, "19OCT"), name="19OCT.xlsx")
    return moved.doc_number, sale


def test_a_ticket_of_an_earlier_open_day_is_moved_to_the_reported_day(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    moved_doc, sale = _earlier_day_with_ticket(w, upload_id)
    w.sell_all_issued(upload_id, skip=[moved_doc])
    # Day 19: its own report lists another ticket, so the sale there is "not in Amadeus".
    w.run(D19)
    earlier = next(f for f in w.live(D19) if f.kind == "not_in_amadeus")
    assert earlier.doc_number == moved_doc
    assert w.day(D19).status == "compared"
    run = w.run(D20)
    assert run.move_count == 1 and run.flag_count == 0 and run.blocking_count == 0
    head = w.db.get(Sale, sale.id)
    assert head is not None and head.report_day_id == w.day(D20).id
    versions = sales.sale_history_for(w.db, w.manager, sale.id)
    assert (versions[-1].source, versions[-1].report_day_id) == ("comparison", w.day(D20).id)
    assert "20 octobre 2026" in (versions[-1].reason or "")
    flag19 = next(f for f in w.all_flags(D19) if f.id == earlier.id)
    assert flag19.status == "moved"
    assert w.day(D20).status == "approved" and w.day(D19).status == "compared"


def test_a_staff_run_never_moves_a_sale_and_a_manager_run_does(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    moved_doc, sale = _earlier_day_with_ticket(w, upload_id)
    w.sell_all_issued(upload_id, skip=[moved_doc])
    run = w.run(D20, actor=w.staff)
    head = w.db.get(Sale, sale.id)
    assert head is not None and head.report_day_id == w.day(D19).id
    assert run.started_by_user_id == w.staff.id and run.move_count == 0
    assert [(f.kind, f.doc_number, f.app_value) for f in w.live()] == [
        ("elsewhere", moved_doc, "2026-10-19")
    ]
    assert w.day().status == "compared"
    # a manager running it again moves the sale and the flag goes
    again = w.run(D20)
    assert again.move_count == 1 and w.live() == []
    assert w.day().status == "approved"


def test_a_ticket_of_an_earlier_closed_day_is_not_moved(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    moved_doc, sale = _earlier_day_with_ticket(w, upload_id)
    w.sell_all_issued(upload_id, skip=[moved_doc])
    days.close_day(w.db, w.manager, w.day(D19), now=NOW)
    w.db.commit()
    run = w.run(D20)
    assert run.move_count == 0 and run.blocking_count == 1
    (flag,) = w.live()
    assert (flag.kind, flag.doc_number, flag.sale_id) == ("elsewhere", moved_doc, sale.id)
    assert flag.app_value == "2026-10-19"  # the day the sale really is on
    head = w.db.get(Sale, sale.id)
    assert head is not None and head.report_day_id == w.day(D19).id


def test_a_ticket_of_a_later_day_is_elsewhere(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    moved = w.lines(upload_id)[TKTT_INDEXES[0]]
    w.sell([w.ticket_of(moved)], sign=moved.staff_sign, day=date(2026, 10, 21))
    w.sell_all_issued(upload_id, skip=[moved.doc_number])
    w.run()
    (flag,) = w.live()
    assert (flag.kind, flag.app_value) == ("elsewhere", "2026-10-21")


# --- refusals -----------------------------------------------------------------------------------


def test_a_closed_day_cannot_be_compared(w: FlagWorld) -> None:
    loaded(w)
    w.run()
    days.close_day(w.db, w.manager, w.day(), now=NOW)
    w.db.commit()
    before = w.db.scalar(select(func.count()).select_from(ComparisonRun))
    with pytest.raises(days.DayClosed):
        flags.run_comparison(w.db, w.manager, w.day(), now=NOW)
    w.db.rollback()
    assert w.db.scalar(select(func.count()).select_from(ComparisonRun)) == before


def test_a_day_without_a_report_cannot_be_compared(w: FlagWorld) -> None:
    day = days.get_or_open_day(w.db, w.manager, D20, now=NOW)
    w.db.commit()
    with pytest.raises(flags.FlagError, match="Aucun rapport"):
        flags.run_comparison(w.db, w.manager, day, now=NOW)


def test_a_day_of_another_agency_is_not_found(w: FlagWorld) -> None:
    loaded(w)
    day = w.day()
    with pytest.raises(days.DayError):
        flags.run_comparison(w.db, w.other_manager, day, now=NOW)
    assert flags.flags_for_day(w.db, w.other_manager, day) == []


# --- reading ------------------------------------------------------------------------------------


def _three_flags(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    skip = [doc_at(w, upload_id, i) for i in TKTT_INDEXES[:6]]
    w.sell_all_issued(upload_id, skip=skip)
    w.run()


def test_staff_see_only_the_flags_of_their_own_signs(w: FlagWorld) -> None:
    _three_flags(w)
    everyone = flags.flags_for_day(w.db, w.manager, w.day())
    assert len(everyone) == 6
    assert len(flags.flags_for_day(w.db, w.accountant, w.day())) == 6
    mine = flags.flags_for_day(w.db, w.staff, w.day())
    assert mine and all(f.responsible_sign == "0004SV" for f in mine)
    assert len(mine) < 6
    theirs = flags.flags_for_day(w.db, w.staff2, w.day())
    assert all(f.responsible_sign == "0006AC" for f in theirs)
    assert {f.id for f in mine}.isdisjoint(f.id for f in theirs)
    assert {f.id for f in flags.flags_for_me(w.db, w.staff)} == {f.id for f in mine}
    assert len(flags.flags_for_me(w.db, w.manager)) == 6


def test_the_open_blocking_count_follows_the_viewer(w: FlagWorld) -> None:
    _three_flags(w)
    assert flags.open_blocking_count(w.db, w.manager) == 6
    mine = len(flags.flags_for_day(w.db, w.staff, w.day()))
    assert flags.open_blocking_count(w.db, w.staff) == mine
    first = flags.flags_for_day(w.db, w.manager, w.day())[0]
    w.set_status(first.id, "answered")
    assert flags.open_blocking_count(w.db, w.manager) == 5


def test_flags_are_scoped_to_the_agency(w: FlagWorld) -> None:
    _three_flags(w)
    assert flags.flags_for_me(w.db, w.other_manager) == []
    assert flags.open_blocking_count(w.db, w.other_manager) == 0
    assert flags.latest_run(w.db, w.other_manager, w.day()) is None


def test_flags_are_never_deleted(w: FlagWorld) -> None:
    from app.db_roles import GRANTS

    assert "DELETE" not in GRANTS["comparison_flags"]
    assert "UPDATE" not in GRANTS["comparison_runs"] and "DELETE" not in GRANTS["comparison_runs"]
    upload = w.upload()
    lines = w.lines(upload.upload_id)
    for line in lines:
        if line.trnc == "TKTT":
            w.sell([w.ticket_of(line, total_doc=w.cost_of(line) + 1)], sign=line.staff_sign)
    w.run()
    first = {f.id for f in w.all_flags()}
    assert len(first) == 34
    w.upload(w.changed_20oct(TKTT_INDEXES[0], **{"TOTAL DOC": 1}))
    w.run()
    assert first <= {f.id for f in w.all_flags()}  # superseded, not removed
    assert (
        w.db.scalar(
            select(func.count())
            .select_from(ComparisonFlag)
            .where(ComparisonFlag.status == "superseded")
        )
        == 1
    )


def test_a_run_is_audited_with_ids_and_counts_only(w: FlagWorld) -> None:
    loaded(w)
    run = w.run()
    (event,) = w.db.scalars(
        select(AuditEvent).where(AuditEvent.event_type == "comparison_run")
    ).all()
    assert event.agency_id == w.agency_id and event.actor_user_id == w.manager.id
    assert event.details == {
        "run_id": run.id,
        "report_day_id": w.day().id,
        "upload_id": run.upload_id,
        "matched": 34,
        "flags": 0,
        "blocking": 0,
        "cancelled": 9,
        "moves": 0,
        "partner_not_in_report": 0,
    }


def test_only_flags_py_writes_flags() -> None:
    """The status of a flag changes only through app/flags.py and its merge, and (B9) through the
    compare-and-set in the app/approvals package, where an agent's answer or a validator's decision
    lands."""
    root = Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name not in {"flags.py", "flag_merge.py", "models.py"}
        and "approvals" not in path.relative_to(root).parts
        and re.search(r"\bComparisonFlag\b", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_the_run_time_is_the_given_time(w: FlagWorld) -> None:
    loaded(w)
    run = w.run()
    assert run.created_at.replace(tzinfo=UTC) == datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
