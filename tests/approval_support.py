"""Scenarios for the approval tests: the made-up 20 October report with one chosen difference.

Everything is synthetic. ``diff_world`` sells every issued line as the report has it except one
ticket, whose field is changed, and runs the comparison, so exactly one flag is open."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app import approvals, sales
from app.models import AmadeusLine, ComparisonFlag, FlagDecision, User
from app.sales import SaleInput, SaleView
from tests.flag_support import D20, NOW, FlagWorld, changed

OTHER_NAME = "SYNTHETIC/OTHER"


def agent_for(w: FlagWorld, sign: str) -> User:
    return {"0004SV": w.staff, "0006AC": w.staff2}[sign]


def diff_world(
    w: FlagWorld,
    field: str = "total_doc",
    *,
    sign: str = "0004SV",
    nth: int = 0,
    value: Any = None,
) -> tuple[ComparisonFlag, AmadeusLine]:
    """Sell the report as it is, but one ticket of ``sign`` with ``field`` changed; compare."""
    upload = w.upload()
    lines = w.lines(upload.upload_id)
    target = [x for x in lines if x.trnc == "TKTT" and x.staff_sign == sign][nth]
    if value is None:
        value = {
            "total_doc": w.cost_of(target) + 100,
            "pax_name": OTHER_NAME,
            "rloc": "AAAAAA",
            "pax_type": "CHD",
            "staff_sign": target.staff_sign,
        }[field]
    for line in lines:
        if line.trnc != "TKTT":
            continue
        ticket = changed(line, **{field: value}) if line is target else w.ticket_of(line)
        w.sell([ticket], sign=line.staff_sign)
    w.run()
    flag = next(f for f in w.live() if f.doc_number == target.doc_number)
    return flag, target


def sale_of(w: FlagWorld, sale_id: int) -> SaleView:
    view = sales.get_sale_for(w.db, w.manager, sale_id)
    assert view is not None
    return view


def decisions(w: FlagWorld, flag_id: int) -> list[FlagDecision]:
    return list(
        w.db.scalars(
            select(FlagDecision)
            .where(FlagDecision.agency_id == w.agency_id, FlagDecision.flag_id == flag_id)
            .order_by(FlagDecision.id)
        )
    )


def answer(
    w: FlagWorld, actor: User, flag_id: int, choice: str, reason: str | None = None
) -> approvals.AnswerResult:
    result = approvals.answer_flag(w.db, actor, flag_id, choice, reason, now=NOW)
    w.db.commit()
    return result


def validate(
    w: FlagWorld, actor: User, flag_id: int, approve: bool, reason: str | None = None
) -> approvals.ValidateResult:
    result = approvals.validate_flag(w.db, actor, flag_id, approve, reason, now=NOW)
    w.db.commit()
    return result


def status_of(w: FlagWorld, flag_id: int) -> str:
    w.db.expire_all()
    row = w.db.get(ComparisonFlag, flag_id)
    assert row is not None
    return row.status


def partner_override_sale(
    w: FlagWorld, doc: str, *, actor: User | None = None, day: date = D20, fee: int = 300
) -> SaleView:
    """A partner sale of one ticket (not in the report) with a one-off fee."""
    line = w.lines(w.db.scalar(select(AmadeusLine.upload_id).limit(1)) or 0)[0]
    ticket = w.ticket_of(line, doc_number=doc)
    result = sales.create_sale(
        w.db,
        actor or w.manager,
        SaleInput(
            tickets=[ticket],
            ag_kind="partner",
            partner_id=w.badi.id,
            payment_method="credit",
            staff_sign="0004SV",
            fee_override=Decimal(fee),
            fee_override_reason="Remise accordée au partenaire",
        ),
        day=day,
        now=NOW,
    )
    w.db.commit()
    return result.sale
