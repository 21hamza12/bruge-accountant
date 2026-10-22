"""Moves, fingerprints, approvals and locks of the comparison (review round of B8 wiring)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app import flags, sales
from app.flag_merge import fingerprint, norm_sign
from app.models import AuditEvent, ComparisonRun, Sale
from tests.fixtures import synthetic_20oct as fx
from tests.flag_support import D19, D20, NOW, FlagWorld

TKTT = [i for i, line in enumerate(fx.LINES) if line["TRNC"] == "TKTT"]


@pytest.fixture
def w(session: Session) -> FlagWorld:
    return FlagWorld(session)


def line_report(w: FlagWorld, indexes: list[int], range_raw: str = "19OCT") -> bytes:
    """A small report holding some lines of the synthetic one (same numbers, same facts)."""
    return w.report([dict(fx.LINES[i]) for i in indexes], range_raw)


def kinds(w: FlagWorld, day: Any) -> list[tuple[str, str]]:
    return sorted((f.kind, f.doc_number) for f in w.live(day))


def audits(w: FlagWorld, event_type: str) -> list[AuditEvent]:
    return list(w.db.scalars(select(AuditEvent).where(AuditEvent.event_type == event_type)))


# --- a sale of two tickets ---------------------------------------------------------------------


def two_ticket_sale_on_d19(w: FlagWorld, upload_id: int) -> tuple[Sale, list[str]]:
    lines = w.lines(upload_id)
    a = lines[TKTT[0]]
    b = next(lines[i] for i in TKTT[1:] if lines[i].staff_sign == a.staff_sign)
    view = w.sell([w.ticket_of(a), w.ticket_of(b)], sign=a.staff_sign, day=D19)
    head = w.db.get(Sale, view.id)
    assert head is not None
    return head, [a.doc_number, b.doc_number]


def test_a_sale_moves_whole_when_every_ticket_is_in_the_report(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    sale, docs = two_ticket_sale_on_d19(w, upload_id)
    w.upload(line_report(w, [TKTT[5]]), name="19OCT.xlsx")
    w.sell_all_issued(upload_id, skip=docs)
    w.run(D19)  # the earlier day has two NOT_IN_AMADEUS flags and one NOT_SENT
    assert [k for k, _ in kinds(w, D19)].count("not_in_amadeus") == 2
    run = w.run(D20)
    assert (run.move_count, run.flag_count, run.blocking_count) == (1, 0, 0)
    assert w.db.get(Sale, sale.id).report_day_id == w.day(D20).id  # type: ignore[union-attr]
    # no stale flag anywhere: the two NOT_IN_AMADEUS flags are `moved`
    moved = [f for f in w.all_flags(D19) if f.status == "moved"]
    assert sorted(f.doc_number for f in moved) == sorted(docs)
    assert all(k != "not_in_amadeus" for k, _ in kinds(w, D19))
    assert w.day(D20).status == "approved"
    # idempotent, and no ping-pong
    again = w.run(D20)
    assert (again.move_count, again.flag_count) == (0, 0)
    w.run(D19)
    assert w.db.get(Sale, sale.id).report_day_id == w.day(D20).id  # type: ignore[union-attr]
    assert all(k != "not_in_amadeus" for k, _ in kinds(w, D19))


def test_a_sale_with_only_one_ticket_in_the_report_does_not_move(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    sale, docs = two_ticket_sale_on_d19(w, upload_id)
    w.upload(line_report(w, [TKTT[5]]), name="19OCT.xlsx")
    w.run(D19)
    # day 20's report lists only one of the sale's two tickets
    only_first = w.report([dict(fx.LINES[TKTT[0]])], "20OCT")
    w.upload(only_first, name="20OCT-short.xlsx")
    run = w.run(D20)
    assert run.move_count == 0
    assert w.db.get(Sale, sale.id).report_day_id == w.day(D19).id  # type: ignore[union-attr]
    (flag,) = w.live(D20)
    assert (flag.kind, flag.doc_number, flag.app_value) == ("elsewhere", docs[0], "2026-10-19")
    # the earlier day keeps its NOT_IN_AMADEUS flags for both tickets
    assert sorted(d for k, d in kinds(w, D19) if k == "not_in_amadeus") == sorted(docs)


def test_a_ticket_the_earlier_day_already_reported_is_not_moved(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    lines = w.lines(upload_id)
    moved = lines[TKTT[0]]
    sale = w.sell([w.ticket_of(moved)], sign=moved.staff_sign, day=D19)
    w.upload(line_report(w, [TKTT[0]]), name="19OCT.xlsx")  # day 19's own report has it
    w.sell_all_issued(upload_id, skip=[moved.doc_number])
    run = w.run(D20)
    assert run.move_count == 0
    assert w.db.get(Sale, sale.id).report_day_id == w.day(D19).id  # type: ignore[union-attr]
    (flag,) = w.live(D20)
    assert (flag.kind, flag.doc_number) == ("elsewhere", moved.doc_number)


def test_a_move_settles_the_earlier_compared_day(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    lines = w.lines(upload_id)
    moved, other = lines[TKTT[0]], lines[TKTT[5]]
    w.sell([w.ticket_of(moved)], sign=moved.staff_sign, day=D19)
    w.sell([w.ticket_of(other, doc_number="1993333333")], sign=other.staff_sign, day=D19)
    w.upload(w.report([{**dict(fx.LINES[TKTT[5]]), "DOC NUMBER": "1993333333"}], "19OCT"))
    w.run(D19)
    assert w.day(D19).status == "compared"  # the moved ticket is "not in Amadeus" there
    w.sell_all_issued(upload_id, skip=[moved.doc_number])
    w.run(D20)
    assert w.day(D19).status == "approved"
    assert w.live(D19) == []
    runs = w.db.scalar(
        select(func.count())
        .select_from(ComparisonRun)
        .where(ComparisonRun.report_day_id == w.day(D19).id)
    )
    assert runs == 2  # the earlier day was compared again after losing the sale


# --- fingerprints ------------------------------------------------------------------------------


def test_a_decision_survives_an_identical_reupload_but_not_a_changed_cost(w: FlagWorld) -> None:
    data = fx.build_synthetic_20oct()
    upload_id = w.upload(data).upload_id
    missing = w.lines(upload_id)[TKTT[0]]
    w.sell_all_issued(upload_id, skip=[missing.doc_number])
    w.run()
    (flag,) = w.live()
    w.set_status(flag.id, "resolved")
    w.upload(data, name="again.xlsx")
    w.run()
    assert [(f.id, f.status) for f in w.all_flags()] == [(flag.id, "resolved")]
    # the Amadeus cost of that ticket changes: same key, same values (none on the app side,
    # the type on the Amadeus side), but not the same ticket
    w.upload(w.changed_20oct(TKTT[0], **{"TOTAL DOC": int(w.cost_of(missing)) + 5}))
    w.run()
    rows = w.all_flags()
    assert [f.status for f in rows] == ["superseded", "open"]
    assert rows[0].fingerprint != rows[1].fingerprint


def test_the_fingerprint_follows_the_ticket_facts() -> None:
    from decimal import Decimal

    from app.comparison import AmadeusTicket

    def amd(**kw: Any) -> AmadeusTicket:
        base: dict[str, Any] = {
            "doc_number": "1992600001",
            "airline": "199",
            "total_doc": Decimal("5022"),
            "pax_name": "DUPONT/JEAN",
            "pax_type": "ADT",
            "rloc": "ABC123",
            "trnc": "TKTT",
            "staff_sign": "0004SV",
            "seq_order": 1,
        }
        base.update(kw)
        return AmadeusTicket(**base)

    same = fingerprint(None, amd())
    assert same == fingerprint(None, amd(pax_name=" dupont/jean ", seq_order=9))
    assert same == fingerprint(None, amd(total_doc=Decimal("5022.00")))
    for change in (
        {"total_doc": Decimal("5023")},
        {"pax_name": "OTHER/NAME"},
        {"airline": "147"},
        {"trnc": "CANX"},
        {"pax_type": "CHD"},
        {"rloc": "ZZZ999"},
        {"staff_sign": "0006AC"},
    ):
        assert fingerprint(None, amd(**change)) != same, change


def test_signs_are_normalised() -> None:
    assert norm_sign(" 0004sv ") == "0004SV" and norm_sign("") == ""


# --- approvals and audit -----------------------------------------------------------------------


def test_a_staff_run_never_approves_the_day(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id)
    run = w.run(D20, actor=w.staff)
    assert run.flag_count == 0
    assert w.day().status == "compared"
    assert audits(w, "day_approved") == []
    w.run(D20)  # the manager approves
    assert w.day().status == "approved"
    (event,) = audits(w, "day_approved")
    assert event.actor_user_id == w.manager.id
    assert event.details == {"report_day_id": w.day().id, "day": "2026-10-20"}
    w.run(D20, actor=w.staff)  # an approved day with nothing blocking stays approved
    assert w.day().status == "approved"


def test_superseding_a_blocking_flag_is_audited(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    lines = w.lines(upload_id)
    target = lines[TKTT[1]]
    for line in lines:
        if line.trnc == "TKTT":
            cost = w.cost_of(line) + (7 if line.id == target.id else 0)
            w.sell([w.ticket_of(line, total_doc=cost)], sign=line.staff_sign)
    w.run()
    (old,) = w.live()
    assert audits(w, "comparison_flags_superseded") == []
    sales.edit_sale(
        w.db,
        w.manager,
        _sale_of(w, target.doc_number),
        _same(w, target),
        "correction du coût",
        now=NOW,
    )
    w.db.commit()
    w.run()
    (event,) = audits(w, "comparison_flags_superseded")
    assert event.details["flag_ids"] == [old.id]
    assert set(event.details) == {"run_id", "report_day_id", "flag_ids"}


def _sale_of(w: FlagWorld, doc: str) -> int:
    from app.models import SaleTicket

    found = w.db.scalar(select(SaleTicket.sale_id).where(SaleTicket.doc_number == doc))
    assert found is not None
    return found


def _same(w: FlagWorld, line: Any) -> Any:
    from app.sales import SaleInput

    return SaleInput(
        tickets=[w.ticket_of(line)],
        ag_kind="direct",
        pb="99999",
        clt="Client Synth",
        payment_method="cash",
        staff_sign=line.staff_sign,
    )


# --- locks and scoping -------------------------------------------------------------------------


def test_the_days_are_locked_together_in_id_order(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    moved = w.lines(upload_id)[TKTT[0]]
    w.sell([w.ticket_of(moved)], sign=moved.staff_sign, day=D19)
    w.sell_all_issued(upload_id, skip=[moved.doc_number])
    statements: list[str] = []

    def spy(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(" ".join(statement.split()))

    engine = w.db.get_bind()
    event.listen(engine, "before_cursor_execute", spy)
    try:
        w.run(D20)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    locking = [s for s in statements if "report_days.created_at" in s and "report_days.id IN" in s]
    assert locking, "the days are locked with one query over several ids"
    assert all("ORDER BY report_days.id" in s for s in locking)


def test_staff_counts_are_the_size_of_what_they_see(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    skip = [w.lines(upload_id)[i].doc_number for i in TKTT[:6]]
    w.sell_all_issued(upload_id, skip=skip)
    w.run()
    day = w.day()
    everyone = flags.day_summary(w.db, w.manager, day).counts
    mine = flags.day_summary(w.db, w.staff, day)
    assert everyone is not None and mine.counts is not None
    assert everyone.matched == 28 and everyone.blocking == 6 and everyone.moves == 0
    assert mine.counts.matched is None and mine.counts.moves is None
    assert mine.counts.blocking == len(mine.flags) < 6
    assert mine.counts.cancelled == len(mine.cancelled)
    unscoped = flags.day_summary(w.db, w.staff2, day).counts
    assert unscoped is not None and unscoped.blocking + mine.counts.blocking <= 6


def test_a_blank_sign_flag_is_for_managers_only(w: FlagWorld) -> None:
    upload_id = w.upload().upload_id
    w.sell_all_issued(upload_id, skip=[w.lines(upload_id)[TKTT[0]].doc_number])
    w.run()
    (flag,) = w.live()
    flag.responsible_sign = ""
    w.db.commit()
    assert [f.id for f in flags.flags_for_day(w.db, w.manager, w.day())] == [flag.id]
    assert flags.flags_for_day(w.db, w.staff, w.day()) == []
    assert flags.flags_for_day(w.db, w.staff2, w.day()) == []
