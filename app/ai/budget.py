"""The monthly AI budget of an agency, and the usage log behind it.

* ``ai_usage`` (append-only) gets one row for each call and each refusal. The month-to-date cost is
  one indexed ``SUM`` over ``(agency_id, created_at)`` from the first of the month, UTC.
* The check before a call: month-to-date + the estimate of that call must not go over the budget.
  Two requests at the same moment can each pass the check and overshoot by a call or two; that is
  accepted (a screen costs under a cent) rather than serialising every call behind a lock.
* ``agency_ai_settings`` holds an agency's own switch and budget. No row means the defaults: on,
  ``AI_MONTHLY_BUDGET_USD``. It is written only by ``python -m app ai-budget``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import pricing
from app.models import AgencyAiSettings, AiUsage, User

PURPOSE_SCREENSHOT = "screenshot"
PURPOSE_CHAT = "chat"
OUTCOME_OK = "ok"
OUTCOME_FALLBACK = "fallback"
OUTCOME_ERROR = "error"
OUTCOME_REFUSED = "refused_budget"
BUDGET_MESSAGE = "Budget IA du mois atteint : collez le texte Amadeus."
BUDGET_CHAT = "Budget IA du mois atteint : je ne comprends que les demandes simples."


@dataclass(frozen=True)
class AgencyLimits:
    enabled: bool
    budget_micro: int


def month_start(now: datetime) -> datetime:
    """The first instant of ``now``'s month, UTC."""
    moment = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_start(now: datetime) -> datetime:
    start = month_start(now)
    return (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )


def get_limits(db: Session, agency_id: int, default_budget_usd: Decimal) -> AgencyLimits:
    row = db.get(AgencyAiSettings, agency_id)
    if row is None:
        return AgencyLimits(True, pricing.usd_to_micro(default_budget_usd))
    return AgencyLimits(bool(row.enabled), int(row.monthly_budget_micro_usd))


def agency_enabled(db: Session, agency_id: int) -> bool:
    """False only when the agency has a settings row that switches the AI off."""
    enabled = db.scalar(
        select(AgencyAiSettings.enabled).where(AgencyAiSettings.agency_id == agency_id)
    )
    return True if enabled is None else bool(enabled)


def month_to_date_micro(db: Session, agency_id: int, now: datetime) -> int:
    total = db.scalar(
        select(func.coalesce(func.sum(AiUsage.cost_micro_usd), 0)).where(
            AiUsage.agency_id == agency_id,
            AiUsage.created_at >= month_start(now),
            AiUsage.created_at < next_month_start(now),
        )
    )
    return int(total or 0)


def day_start(now: datetime) -> datetime:
    """UTC midnight of ``now``'s day."""
    moment = now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def fallbacks_today(db: Session, agency_id: int, now: datetime) -> int:
    """Calls the chat made to the stronger model for this agency since UTC midnight: the rows
    ``fallback`` of purpose ``chat``. One indexed query (``agency_id``, ``created_at``)."""
    found = db.scalar(
        select(func.count())
        .select_from(AiUsage)
        .where(
            AiUsage.agency_id == agency_id,
            AiUsage.created_at >= day_start(now),
            AiUsage.purpose == PURPOSE_CHAT,
            AiUsage.outcome == OUTCOME_FALLBACK,
        )
    )
    return int(found or 0)


def within_budget(
    db: Session,
    agency_id: int,
    now: datetime,
    model: str,
    default_budget_usd: Decimal,
    *,
    estimate_micro: int | None = None,
    share: Decimal = Decimal(1),
) -> bool:
    """True when one more call to ``model`` keeps the month under ``share`` of the budget. The
    estimate is a screenshot's unless ``estimate_micro`` says otherwise (the chat passes its own
    and a share below 1, which keeps the last part of the month for screenshots)."""
    limits = get_limits(db, agency_id, default_budget_usd)
    if not limits.enabled:
        return False
    estimate = pricing.estimate_micro_usd(model) if estimate_micro is None else estimate_micro
    spent = month_to_date_micro(db, agency_id, now)
    return spent + estimate <= int(limits.budget_micro * share)


def record(
    db: Session,
    user: User,
    *,
    now: datetime,
    model: str,
    input_tokens: int,
    output_tokens: int,
    outcome: str,
    purpose: str = PURPOSE_SCREENSHOT,
    cost_micro_usd: int | None = None,
) -> int:
    """Append a usage row and return the cost in micro-USD: computed here from the tokens, unless
    ``cost_micro_usd`` says otherwise (a timed-out call has no tokens but may have been billed).
    The caller commits."""
    cost = (
        pricing.cost_micro_usd(model, input_tokens, output_tokens)
        if cost_micro_usd is None
        else cost_micro_usd
    )
    db.add(
        AiUsage(
            agency_id=user.agency_id,
            user_id=user.id,
            created_at=now,
            purpose=purpose,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_micro_usd=cost,
            outcome=outcome,
        )
    )
    db.flush()
    return cost


def set_limits(
    db: Session,
    agency_id: int,
    *,
    budget_usd: Decimal | None,
    enabled: bool | None,
    default_budget_usd: Decimal,
    now: datetime,
) -> AgencyAiSettings:
    """Create or update the agency's row (a field left ``None`` keeps its value, or the default
    for a new row). The caller commits."""
    row = db.get(AgencyAiSettings, agency_id)
    if row is None:
        row = AgencyAiSettings(
            agency_id=agency_id,
            enabled=True,
            monthly_budget_micro_usd=pricing.usd_to_micro(default_budget_usd),
        )
        db.add(row)
    if budget_usd is not None:
        row.monthly_budget_micro_usd = pricing.usd_to_micro(budget_usd)
    if enabled is not None:
        row.enabled = enabled
    row.updated_at = now
    db.flush()
    return row
