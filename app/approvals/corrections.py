"""The corrections the TJQ APRES shows (B10 reads them through ``corrections_for``)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CHOICE_MINE_RIGHT,
    CHOICE_VALIDATED,
    FLAG_DIFFERENT,
    FLAG_RESOLVED,
    STAGE_VALIDATOR,
    ComparisonFlag,
    FlagDecision,
    User,
)

# TJQ column titles of the flag fields that have a column (pax type has none).
COLUMN_TITLES: Final = {
    "airline": "A/L",
    "total_doc": "TOTAL DOC",
    "pax_name": "PAX NAME",
    "staff_sign": "AS",
    "rloc": "RLOC",
    "trnc": "TRNC",
}
Corrections = Mapping[tuple[str, str], tuple[str, str, str]]


def corrections_for(db: Session, agency_id: int, report_day_id: int) -> Corrections:
    """What the TJQ APRES shows as corrected: ``{(doc number, column title): (Amadeus original,
    approved value, who approved)}`` for the resolved ``different`` flags of the day whose app
    value was kept. Only fields with a TJQ column (not pax type). A value taken from Amadeus is no
    correction: the sale was edited and the flag is gone. Keys use the flag's 10-digit number
    (the export looks them up by the normalised number of each line)."""
    wanted = [
        f
        for f in db.scalars(
            select(ComparisonFlag)
            .where(
                ComparisonFlag.agency_id == agency_id,
                ComparisonFlag.report_day_id == report_day_id,
                ComparisonFlag.kind == FLAG_DIFFERENT,
                ComparisonFlag.status == FLAG_RESOLVED,
            )
            .order_by(ComparisonFlag.id)
        )
        if f.field in COLUMN_TITLES and f.app_value != f.amadeus_value
    ]
    if not wanted:
        return {}
    # Who approved: the validator when there was one, else the agent whose answer settled it.
    approver: dict[int, int] = {}
    decisions = db.scalars(
        select(FlagDecision)
        .where(
            FlagDecision.agency_id == agency_id,
            FlagDecision.flag_id.in_([f.id for f in wanted]),
            FlagDecision.choice.in_((CHOICE_VALIDATED, CHOICE_MINE_RIGHT)),
        )
        .order_by(FlagDecision.id)
    )
    for d in decisions:
        if d.flag_id not in approver or d.stage == STAGE_VALIDATOR:
            approver[d.flag_id] = d.actor_user_id
    users = {
        u.id: u
        for u in db.scalars(
            select(User).where(User.agency_id == agency_id, User.id.in_(set(approver.values())))
        )
    }
    out: dict[tuple[str, str], tuple[str, str, str]] = {}
    for f in wanted:
        user = users.get(approver.get(f.id, -1))
        who = "—" if user is None else (user.display_name or "").strip() or f"Utilisateur {user.id}"
        out[(f.doc_number, COLUMN_TITLES[f.field or ""])] = (
            f.amadeus_value or "",
            f.app_value or "",
            who,
        )
    return out
