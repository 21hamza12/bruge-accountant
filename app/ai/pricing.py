"""Prices and the cost of a call, in micro-USD (1 000 000 micro-USD = 1 USD).

``PRICES`` is USD per million tokens, ``(input, output)``. Because 1 USD per million tokens is
exactly 1 micro-USD per token, the cost of a call is ``tokens * price`` summed over input and
output, rounded UP to a whole micro-USD (the budget never under-counts).

SOURCE AND STATUS. Anthropic's published first-party prices (checked 23 Oct 2026): Haiku 5.5
0.10 / 0.50, Sonnet 5.5 2 / 10 (Haiku is dearer above 100K prompt tokens; a screenshot is far
below). They reproduce the cost measured on the 16 real screenshots to the micro-dollar (662 tokens
in, 992 out on Haiku: 0.0005622 USD). A model that is not in the table is priced at the most
expensive entry, so a new model name can only make the budget stricter, never looser. Update this
table when prices or models change.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-sonnet-5-5": (Decimal("2"), Decimal("10")),
}
# What one image is estimated to cost before the call (the check against the budget). On the 16
# real screens the largest read was 904 tokens in and 2 561 out; the estimate stays above both.
ESTIMATE_INPUT_TOKENS = 1500
ESTIMATE_OUTPUT_TOKENS = 3000
MICRO = 1_000_000


def _price(model: str) -> tuple[Decimal, Decimal]:
    if model in PRICES:
        return PRICES[model]
    return max(PRICES.values(), key=lambda p: p[0] + p[1])


def cost_micro_usd(model: str, input_tokens: int, output_tokens: int) -> int:
    """The cost of a call in whole micro-USD, rounded up."""
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    price_in, price_out = _price(model)
    exact = price_in * input_tokens + price_out * output_tokens
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


def estimate_micro_usd(model: str) -> int:
    """What one image is assumed to cost, before the call."""
    return cost_micro_usd(model, ESTIMATE_INPUT_TOKENS, ESTIMATE_OUTPUT_TOKENS)


def usd_to_micro(usd: Decimal) -> int:
    return int((usd * MICRO).to_integral_value(rounding=ROUND_CEILING))


def micro_to_usd_text(micro: int) -> str:
    """``1234567`` as ``1.234567``."""
    return f"{Decimal(micro) / MICRO:.6f}".rstrip("0").rstrip(".") or "0"


# --- the chat (phase E part 2, docs/design/13-chat-ai.md) -------------------------------------
# Prompt caching: a token written to the cache costs 1.25 x the input price, a token read from it
# 0.10 x. ``input_tokens`` below is only the part of the prompt that is neither written nor read.
CACHE_WRITE_FACTOR = Decimal("1.25")
CACHE_READ_FACTOR = Decimal("0.10")
# What ONE chat call is estimated to cost before it is made: a prompt of 8 000 tokens (system,
# tools, the question and the tool results of the turn: about 3 500 in practice) all written to the
# cache, and the output limit of a call (1 024 tokens, thinking included).
# Above any real call; the budget never under-counts.
CHAT_ESTIMATE_INPUT_TOKENS = 8000
CHAT_ESTIMATE_OUTPUT_TOKENS = 1024  # = chat_client.MAX_OUTPUT_TOKENS (a test pins it)


def cost_with_cache_micro_usd(
    model: str,
    input_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
) -> int:
    """The cost of a call that used prompt caching, in whole micro-USD, rounded up."""
    if min(input_tokens, cache_write_tokens, cache_read_tokens, output_tokens) < 0:
        raise ValueError("token counts cannot be negative")
    price_in, price_out = _price(model)
    exact = (
        price_in * input_tokens
        + price_in * CACHE_WRITE_FACTOR * cache_write_tokens
        + price_in * CACHE_READ_FACTOR * cache_read_tokens
        + price_out * output_tokens
    )
    return int(exact.to_integral_value(rounding=ROUND_CEILING))


def estimate_chat_micro_usd(model: str) -> int:
    """What one chat call is assumed to cost, before the call."""
    return cost_with_cache_micro_usd(
        model, 0, CHAT_ESTIMATE_INPUT_TOKENS, 0, CHAT_ESTIMATE_OUTPUT_TOKENS
    )
