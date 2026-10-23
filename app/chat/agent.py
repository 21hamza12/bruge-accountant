"""The chat asks Claude when plain code does not understand (docs/design/13-chat-ai.md).

``answer_with_ai`` is the ``unknown`` handler of ``router.route_message``. Plain code decides first
and costs nothing: a piece of an Amadeus screen never reaches the model; an agency with the AI
switched off, a person past their hourly limit of free questions, or a month whose chat share of
the budget is spent, each get a French sentence and the help of their role. Otherwise the model
is given the tools of the person's ROLE (``ai_prompt.tool_definitions``) and at most
``MAX_ROUNDS`` calls: it asks for lookups, the code runs them with the person's own rights
(``tools.run_tool``: permission, typed arguments, the agency and the signs of the session) and
gives back ``ai_prompt.model_view`` of each result, never ``Reply.lines``. The answer shown is the
model's words made plain (``ai_prompt.clean_prose``, and replaced when a number in them comes from
nowhere) followed by the lines of the lookups it ran, which carry the real links.

A turn the cheap model could not resolve (cut answer, refusal, still wanting tools at the round
cap, every lookup invalid) is started again from the same messages by the stronger model, once.
Every model call is checked against the budget before and recorded after (``app.ai.calls``).
Nothing the person or the model said is logged or kept in ``ai_usage``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from app.ai import budget, pricing
from app.ai.calls import BudgetRefused, Caller, CallFailed, Usage
from app.ai.chat_client import ChatModelClient, ChatRequest, ChatResponse, ToolUse
from app.auth import ratelimit
from app.chat import ai_prompt
from app.chat import reply as r
from app.chat.reply import Line, Reply
from app.chat.router import looks_like_fragment, polite_unknown
from app.chat.tools import ChatContext, help_lines, run_tool
from app.settings import Settings

log = logging.getLogger("app.chat")

MAX_TOOL_CALLS = 4  # lookups run in one turn
MAX_ROUNDS = 3  # model calls in one turn (the last one cannot ask for a tool)
TOOL_UNAVAILABLE = "outil indisponible"
CALL_LIMIT = "limite d'appels atteinte"
FRAGMENT_HINT = "Collez l'écran Amadeus complet"
TOO_MANY_AI = (
    "Trop de questions libres pour le moment : réessayez plus tard, ou posez une demande simple."
)
AI_DOWN = (
    "Je n'ai pas pu répondre à cette question pour le moment. "
    "Les demandes simples marchent toujours :"
)
PARTIAL = "Je n'ai pas pu répondre complètement."

# how a pass ends
DONE, UNRESOLVED, BUDGET, DOWN = "done", "unresolved", "budget", "down"
CUT_OFF = ("max_tokens", "refusal")  # stop reasons that leave the turn unresolved


@dataclass
class _Totals:
    """Tokens of the whole turn, for the log (and nothing else)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    rounds: int = 0


@dataclass
class _Pass:
    """One model's attempt at the turn."""

    model: str
    status: str = DONE
    reason: str = ""  # why UNRESOLVED: max_tokens, refusal, round_cap or invalid
    prose: str = ""
    ran: list[tuple[str, Reply]] = field(default_factory=list)  # the lookups, in order
    sent: list[str] = field(default_factory=list)  # the results as the model saw them
    rounds: int = 0


def _lines_of(reply: Reply) -> tuple[Line, ...]:
    """The lines a lookup shows to the person. The one place that reads them: they hold names, and
    go to the page, never to a model."""
    return reply.lines


def _tool_lines(ran: Sequence[tuple[str, Reply]]) -> list[Line]:
    seen: set[tuple[str, str | None, bool]] = set()
    out: list[Line] = []
    for _, reply in ran:
        for line in _lines_of(reply):
            key = (line.text, line.href, line.button)
            if key not in seen and len(out) < ai_prompt.MAX_TOOL_LINES:
                seen.add(key)
                out.append(line)
    return out


def _error_block(use: ToolUse, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": use.id, "content": message, "is_error": True}


def _run_uses(
    ctx: ChatContext, uses: Sequence[ToolUse], offered: set[str], state: _Pass
) -> list[dict[str, Any]]:
    """Run the lookups the model asked for, in order, and the ``tool_result`` blocks to send back.
    A name that was not offered to this role, arguments that are not an object, and every call past
    the cap run nothing."""
    blocks: list[dict[str, Any]] = []
    for use in uses:
        if len(state.ran) >= MAX_TOOL_CALLS:
            blocks.append(_error_block(use, CALL_LIMIT))
        elif use.name not in offered or not isinstance(use.input, dict):
            blocks.append(_error_block(use, TOOL_UNAVAILABLE))
        else:
            reply = run_tool(ctx, use.name, use.input)
            state.ran.append((use.name, reply))
            sent = ai_prompt.model_view(reply)
            state.sent.append(sent)
            blocks.append(
                {"type": "tool_result", "tool_use_id": use.id, "content": sent, "is_error": False}
            )
    return blocks


def _usage(response: ChatResponse) -> Usage:
    return Usage(
        response.input_tokens,
        response.output_tokens,
        response.cache_write_tokens,
        response.cache_read_tokens,
    )


def _run_pass(
    ctx: ChatContext,
    caller: Caller,
    client: ChatModelClient,
    turn: str,
    tools: list[dict[str, Any]],
    model: str,
    outcome: str,
    totals: _Totals,
) -> _Pass:
    state = _Pass(model)
    offered = {t["name"] for t in tools}
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": turn}]}]
    asked = 0  # tool_use blocks seen
    for number in range(1, MAX_ROUNDS + 1):
        last = number == MAX_ROUNDS
        request = ChatRequest(
            model, ai_prompt.SYSTEM, tools, messages, {"type": "none" if last else "auto"}
        )
        try:
            response = caller.call(
                model,
                outcome,
                partial(client.complete, request),
                _usage,
                outcome_of=lambda got: (
                    budget.OUTCOME_ERROR if got.stop_reason in CUT_OFF else outcome
                ),
            )
        except BudgetRefused:
            state.status = BUDGET
            return state
        except CallFailed:
            state.status = DOWN
            return state
        state.rounds += 1
        totals.rounds += 1
        totals.input_tokens += _usage(response).billed_input
        totals.output_tokens += response.output_tokens
        totals.cache_read += response.cache_read_tokens
        if response.stop_reason in CUT_OFF:
            state.status, state.reason = UNRESOLVED, response.stop_reason
            return state
        if response.stop_reason != "tool_use" or not response.tool_uses:
            state.prose = response.text
            break
        if last:  # a last round with tool_choice none that still asks for a tool
            state.status, state.reason = UNRESOLVED, "round_cap"
            return state
        asked += len(response.tool_uses)
        blocks = _run_uses(ctx, response.tool_uses, offered, state)
        messages.append({"role": "assistant", "content": response.assistant_content})
        messages.append({"role": "user", "content": blocks})
    if asked and all(reply.outcome == r.INVALID for _, reply in state.ran):
        state.status, state.reason = UNRESOLVED, "invalid"
    return state


def _compose(ctx: ChatContext, text: str, state: _Pass, status: str) -> Reply:
    """The reply shown for a pass that ended as ``status``."""
    user = ctx.user
    tool_lines = _tool_lines(state.ran)
    names = [name for name, _ in state.ran]
    outcome = r.AI if tool_lines else r.UNKNOWN
    if status == DOWN or (status == UNRESOLVED and state.reason == "refusal" and not state.prose):
        lines = [*tool_lines, r.say(AI_DOWN), *help_lines(user)]
        outcome = r.ERROR
    elif status == BUDGET:
        lines = [*tool_lines, r.say(budget.BUDGET_CHAT), *([] if tool_lines else help_lines(user))]
    elif status == UNRESOLVED and not state.prose:
        lines = [*tool_lines, r.say(PARTIAL), *([] if tool_lines else help_lines(user))]
    else:
        prose = ai_prompt.clean_prose(state.prose)
        words = " ".join(line.text for line in prose)
        sources = [*state.sent, text, ctx.today.isoformat()]
        if prose and not ai_prompt.grounded(words, sources):
            prose = (r.say(ai_prompt.DEFAULT_PROSE),) if tool_lines else ()
        if not prose and not tool_lines:
            return _with_data(polite_unknown(ctx, text), state, names)
        lines = [*prose, *tool_lines] if tool_lines else [*prose, *help_lines(user)]
    return Reply(outcome, tuple(lines), names[0] if names else None, _data(state, names))


def _data(state: _Pass, names: list[str]) -> dict[str, Any]:
    return {"tools": names, "rounds": state.rounds, "model": state.model}


def _with_data(reply: Reply, state: _Pass, names: list[str]) -> Reply:
    return Reply(reply.outcome, _lines_of(reply), None, _data(state, names))


def _gated(ctx: ChatContext, text: str) -> Reply | None:
    """The answers plain code gives before any model is asked, or None to go on."""
    user, db = ctx.user, ctx.db
    if looks_like_fragment(text):  # ticket content never reaches the chat model
        first, *rest = _lines_of(polite_unknown(ctx, text))
        return Reply(r.UNKNOWN, (first, r.say(FRAGMENT_HINT), *rest))
    if not budget.agency_enabled(db, user.agency_id):
        return polite_unknown(ctx, text)
    decision = ratelimit.hit(db, ratelimit.CHAT_AI_USER, str(user.id), ctx.now)
    db.commit()
    if not decision.allowed:
        log.info("chat ai limited", extra={"retry_after": decision.retry_after})
        return Reply(r.UNKNOWN, (r.say(TOO_MANY_AI), *help_lines(user)))
    return None


def answer_with_ai(
    ctx: ChatContext,
    text: str,
    *,
    client: ChatModelClient,
    settings: Settings,
    history: Sequence[str] = (),
) -> Reply:
    """The reply to a message that no rule understood. ``history`` is the person's own earlier
    messages of today (``store.recent_user_texts``)."""
    gated = _gated(ctx, text)
    if gated is not None:
        return gated
    caller = Caller(
        ctx.db,
        ctx.user,
        ctx.now,
        default_budget_usd=settings.ai_monthly_budget_usd,
        purpose=budget.PURPOSE_CHAT,
        estimate=pricing.estimate_chat_micro_usd,
        share=settings.ai_chat_budget_share,
    )
    turn = ai_prompt.build_user_turn(ctx.user, ctx.now, text, history)
    tools = ai_prompt.tool_definitions(ctx.user)
    totals = _Totals()
    primary, stronger = settings.ai_chat_model, settings.ai_chat_fallback_model
    state = _run_pass(ctx, caller, client, turn, tools, primary, budget.OUTCOME_OK, totals)
    status = state.status
    if status == UNRESOLVED and stronger and stronger != primary:
        second = _run_pass(
            ctx, caller, client, turn, tools, stronger, budget.OUTCOME_FALLBACK, totals
        )
        if second.status in (DONE, UNRESOLVED):
            state, status = second, second.status
        elif second.status == DOWN:
            status = DOWN  # the first pass's lookups are shown, then the polite error
        # BUDGET: the first pass's partial result is kept (status stays UNRESOLVED)
    answer = _compose(ctx, text, state, status)
    log.info(
        "chat ai",
        extra={
            "model": state.model,
            "rounds": totals.rounds,
            "tools": len(state.ran),
            "outcome": answer.outcome,
            "in_tok": totals.input_tokens,
            "out_tok": totals.output_tokens,
            "cache_read": totals.cache_read,
        },
    )
    return answer
