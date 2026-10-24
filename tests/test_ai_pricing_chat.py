"""Phase E part 2, money: the cost of a chat call with prompt caching, the estimate before a call,
and the share of the month the chat may spend. SQLite only; no network."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.ai import budget, chat_client, pricing
from tests.test_ai_budget import NOW, agency_with_user, log_cost

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
FIVE = Decimal(5)


def test_cache_tokens_are_priced_at_their_own_rates() -> None:
    # Sonnet: input 2, write 2 x 1.25 = 2.5, read 2 x 0.10 = 0.2, output 10 (micro-USD per token)
    cost = pricing.cost_with_cache_micro_usd(SONNET, 100, 1000, 10_000, 50)
    assert cost == 200 + 2500 + 2000 + 500 == 5200
    assert pricing.cost_with_cache_micro_usd(HAIKU, 0, 0, 0, 0) == 0


def test_the_cache_cost_is_rounded_up_never_down() -> None:
    assert pricing.cost_with_cache_micro_usd(HAIKU, 0, 0, 1, 0) == 1  # 0.01 micro-USD counts as 1
    assert pricing.cost_with_cache_micro_usd(HAIKU, 0, 1, 0, 0) == 1  # 0.125 -> 1
    assert pricing.cost_with_cache_micro_usd(HAIKU, 1, 1, 1, 1) == 1  # 0.1+0.125+0.01+0.5 = 0.735


def test_without_cache_it_is_the_plain_cost() -> None:
    assert pricing.cost_with_cache_micro_usd(HAIKU, 1500, 0, 0, 120) == pricing.cost_micro_usd(
        HAIKU, 1500, 120
    )


def test_an_unknown_model_is_priced_like_the_dearest_with_cache_too() -> None:
    assert pricing.cost_with_cache_micro_usd("claude-future-9", 10, 10, 10, 10) == (
        pricing.cost_with_cache_micro_usd(SONNET, 10, 10, 10, 10)
    )


def test_negative_cache_tokens_are_refused() -> None:
    import pytest

    for args in ((-1, 0, 0, 0), (0, -1, 0, 0), (0, 0, -1, 0), (0, 0, 0, -1)):
        with pytest.raises(ValueError):
            pricing.cost_with_cache_micro_usd(HAIKU, *args)


def test_the_chat_estimate_is_above_a_realistic_turn() -> None:
    # a turn: tools + system written to the cache (about 2 500 tokens), a short question, one
    # tool result, a 150-token answer, on the first call of the day (all cache writes)
    for model in (HAIKU, SONNET):
        realistic = pricing.cost_with_cache_micro_usd(model, 400, 2500, 0, 150)
        assert pricing.estimate_chat_micro_usd(model) > realistic
    # the estimate is one call: 8 000 input tokens all written to the cache + 1 024 out (the limit)
    assert pricing.estimate_chat_micro_usd(HAIKU) == pricing.cost_with_cache_micro_usd(
        HAIKU, 0, 8000, 0, 1024
    )
    assert pricing.estimate_chat_micro_usd(HAIKU) == 1512
    assert pricing.estimate_chat_micro_usd(SONNET) == 30240
    assert pricing.CHAT_ESTIMATE_OUTPUT_TOKENS == chat_client.MAX_OUTPUT_TOKENS == 1024


def test_a_new_model_name_cannot_make_the_chat_estimate_cheaper() -> None:
    assert pricing.estimate_chat_micro_usd("claude-future-9") == pricing.estimate_chat_micro_usd(
        SONNET
    )


def test_share_arithmetic_the_chat_stops_at_its_share_and_screenshots_do_not(
    session: Session,
) -> None:
    user = agency_with_user(session)
    chat = pricing.estimate_chat_micro_usd(HAIKU)
    shot = pricing.estimate_micro_usd(HAIKU)
    share = Decimal("0.8")
    ceiling = 5_000_000 * 4 // 5  # 80 % of the 5 USD default
    log_cost(session, user, ceiling - chat)  # leaves room for exactly one chat call
    assert budget.within_budget(
        session, user.agency_id, NOW, HAIKU, FIVE, estimate_micro=chat, share=share
    )
    log_cost(session, user, 1)
    assert not budget.within_budget(
        session, user.agency_id, NOW, HAIKU, FIVE, estimate_micro=chat, share=share
    )
    # a screenshot is still allowed between 80 % and 100 %
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE)
    log_cost(session, user, 5_000_000 - (ceiling - chat + 1) - shot)
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE)  # exactly full
    log_cost(session, user, 1)
    assert not budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE)


def test_the_default_call_is_the_screenshot_one(session: Session) -> None:
    user = agency_with_user(session)
    shot = pricing.estimate_micro_usd(HAIKU)
    log_cost(session, user, 5_000_000 - shot)
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE)
    assert not budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE, share=Decimal("0.9"))


def test_a_disabled_agency_is_refused_whatever_the_share(session: Session) -> None:
    user = agency_with_user(session)
    budget.set_limits(
        session, user.agency_id, budget_usd=None, enabled=False, default_budget_usd=FIVE, now=NOW
    )
    session.commit()
    assert not budget.within_budget(
        session, user.agency_id, NOW, HAIKU, FIVE, estimate_micro=1, share=Decimal(1)
    )


def test_record_takes_an_explicit_cost_and_the_chat_purpose(session: Session) -> None:
    user = agency_with_user(session)
    cost = budget.record(
        session,
        user,
        now=NOW,
        model=HAIKU,
        input_tokens=2500,
        output_tokens=100,
        outcome=budget.OUTCOME_OK,
        purpose=budget.PURPOSE_CHAT,
        cost_micro_usd=777,
    )
    session.commit()
    assert cost == 777 and budget.month_to_date_micro(session, user.agency_id, NOW) == 777
    assert budget.PURPOSE_CHAT == "chat"
