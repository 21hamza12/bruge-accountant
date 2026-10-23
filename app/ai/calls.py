"""One paid call to the AI, with its budget check before and its usage row after.

Shared by the screenshot reader and the chat, so the money rules exist once:

* before a call the month-to-date cost plus the estimate must fit the budget
  (``budget.within_budget``); otherwise a ``refused_budget`` row (0 tokens) is committed and
  ``BudgetRefused`` is raised;
* a call that fails is recorded as ``error`` (cost 0; a timeout is recorded at the estimate, it may
  have been billed) and ``CallFailed`` is raised with the kind, never a message from the API;
* ``commit_before_call`` (the chat; the screenshots do not) commits the session before the call so
  that the pooled connection is not held while the model thinks; a commit that fails then is a
  failed call (``CallFailed("commit_failed")``), never an exception out of the request;
* a call that answers is recorded at once, with its real tokens (cache writes and reads priced
  at their own rates), and committed: money spent is logged even when a later step fails. A usage
  row that cannot be committed after a paid call is logged as an error (model, cost, agency) and
  raised as ``CallFailed("usage_lost")``: the log is then the only trace of that call.

Nothing here sees what was sent or answered, so nothing here can log it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TypeVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import budget, pricing
from app.ai.client import AiUnavailable
from app.models import User

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def billed_input(self) -> int:
        """Every input token, whatever its price: what ``ai_usage.input_tokens`` holds."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


class BudgetRefused(Exception):
    """The month's budget (or its share) is spent; the refusal is already in ``ai_usage``."""


class CallFailed(Exception):
    """The call gave no usable answer; ``kind`` is an ``AiUnavailable`` kind, an exception class
    name, or ``usage_lost``."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class Caller:
    """The calls of one request: one purpose, one estimate, one share of the budget."""

    def __init__(
        self,
        db: Session,
        user: User,
        now: datetime,
        *,
        default_budget_usd: Decimal,
        purpose: str,
        estimate: Callable[[str], int],
        share: Decimal = Decimal(1),
        commit_before_call: bool = False,
    ) -> None:
        self.db, self.user, self.now = db, user, now
        self.default_budget_usd, self.purpose = default_budget_usd, purpose
        self.estimate, self.share = estimate, share
        self.commit_before_call = commit_before_call
        self.agency_id = user.agency_id

    def affordable(self, model: str) -> bool:
        """True when one more call to ``model`` fits; nothing is recorded."""
        return budget.within_budget(
            self.db,
            self.agency_id,
            self.now,
            model,
            self.default_budget_usd,
            estimate_micro=self.estimate(model),
            share=self.share,
        )

    def call(
        self,
        model: str,
        outcome: str,
        make: Callable[[], T],
        usage_of: Callable[[T], Usage],
        *,
        outcome_of: Callable[[T], str] | None = None,
    ) -> T:
        """Check the budget, make the call, record it. ``outcome`` is the row's on success, unless
        ``outcome_of`` looks at the answer and says otherwise (a refusal is an ``error``)."""
        db = self.db
        if not self.affordable(model):
            self.refuse(model)
            raise BudgetRefused
        if self.commit_before_call and not self._release_connection():
            raise CallFailed("commit_failed")
        try:
            result = make()
        except AiUnavailable as exc:
            kind = exc.kind
        except Exception as exc:  # noqa: BLE001 - a client bug must not become a 500
            kind = type(exc).__name__  # the class only: a message could quote the content
        else:
            self._record_paid(model, result, usage_of(result), outcome_of, outcome)
            return result
        log.warning("ai call failed", extra={"kind": kind, "model": model, "purpose": self.purpose})
        # a timeout may still have been billed: count the estimate; other failures were refused
        cost = self.estimate(model) if kind == "timeout" else 0
        self._record(model, Usage(0, 0), budget.OUTCOME_ERROR, cost)
        db.commit()
        raise CallFailed(kind)

    def refuse(self, model: str) -> None:
        """Record (and commit) a ``refused_budget`` row: the call was not made."""
        self._record(model, Usage(0, 0), budget.OUTCOME_REFUSED, 0)
        self.db.commit()

    def _release_connection(self) -> bool:
        """Commit so the pooled connection is free while the model thinks (up to the timeout).
        False when the commit fails (logged by class name only, the session rolled back)."""
        try:
            self.db.commit()
        except SQLAlchemyError as exc:
            self.db.rollback()
            log.error(
                "ai commit before the call failed",
                extra={"error": type(exc).__name__, "purpose": self.purpose},
            )
            return False
        return True

    def _record(self, model: str, usage: Usage, outcome: str, cost: int) -> int:
        return budget.record(
            self.db,
            self.user,
            now=self.now,
            model=model,
            input_tokens=usage.billed_input,
            output_tokens=usage.output_tokens,
            outcome=outcome,
            purpose=self.purpose,
            cost_micro_usd=cost,
        )

    def _record_paid(
        self,
        model: str,
        result: T,
        usage: Usage,
        outcome_of: Callable[[T], str] | None,
        outcome: str,
    ) -> None:
        db = self.db
        cost = pricing.cost_with_cache_micro_usd(
            model,
            usage.input_tokens,
            usage.cache_write_tokens,
            usage.cache_read_tokens,
            usage.output_tokens,
        )
        try:
            self._record(model, usage, outcome_of(result) if outcome_of else outcome, cost)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            # the call was paid and is not in ai_usage: the log is the only trace of it
            log.error(
                "ai usage row lost after a paid call",
                exc_info=True,
                extra={"model": model, "cost_micro_usd": cost, "agency_id": self.agency_id},
            )
            raise CallFailed("usage_lost") from None
