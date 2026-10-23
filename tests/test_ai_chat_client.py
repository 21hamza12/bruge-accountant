"""The chat client (app/ai/chat_client.py) on a stubbed SDK, and the fake that replaces it in every
other test. No network: ``messages.create`` is replaced, as in tests/test_ai_units.py."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai.chat_client import AnthropicChatClient, ChatRequest, ChatResponse, ToolUse
from app.ai.client import API_BASE_URL, AiUnavailable
from app.ai.fake import FakeChatClient, fake_text, fake_tool, fake_tools

HAIKU = "claude-haiku-5-5"
TOOLS = [
    {
        "name": "list_sales",
        "description": "Vos ventes d'un jour.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "strict": True,
    }
]


def request(**kw: Any) -> ChatRequest:
    fields: dict[str, Any] = {
        "model": HAIKU,
        "system": "Tu es l'assistant.",
        "tools": TOOLS,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "Message : bonjour"}]}],
        "tool_choice": {"type": "auto"},
    }
    fields.update(kw)
    return ChatRequest(**fields)


class _Messages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _real(outcome: Any) -> tuple[AnthropicChatClient, _Messages]:
    client = AnthropicChatClient("sk-test-not-real", timeout_s=5, max_retries=1)
    messages = _Messages(outcome)
    client._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return client, messages


def _usage(**kw: Any) -> Any:
    fields: dict[str, Any] = {
        "input_tokens": 400,
        "output_tokens": 90,
        "cache_creation_input_tokens": 2100,
        "cache_read_input_tokens": 0,
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def _answer(content: list[Any], stop_reason: str | None = "end_turn", usage: Any = None) -> Any:
    return SimpleNamespace(
        content=content, usage=usage or _usage(), stop_reason=stop_reason, model=HAIKU
    )


def _text(text: str) -> Any:
    import anthropic

    return anthropic.types.TextBlock(type="text", text=text, citations=None)


def _tool_use(name: str, input: Any, id: str = "toolu_1") -> Any:
    import anthropic

    # model_construct: the SDK type refuses a non-object input, which is what is being tested
    return anthropic.types.ToolUseBlock.model_construct(
        type="tool_use", id=id, name=name, input=input
    )


# --- what is sent -------------------------------------------------------------------------------


def test_the_request_carries_tools_cached_system_effort_and_nothing_forbidden() -> None:
    client, messages = _real(_answer([_text("Bonjour")]))
    client.complete(request())
    sent = messages.kwargs
    assert sent["model"] == HAIKU and sent["max_tokens"] == 300
    assert sent["tools"] == TOOLS and sent["tools"][0]["strict"] is True
    assert sent["tool_choice"] == {"type": "auto"}
    assert sent["system"] == [
        {"type": "text", "text": "Tu es l'assistant.", "cache_control": {"type": "ephemeral"}}
    ]
    assert sent["output_config"] == {"effort": "low"}
    for forbidden in ("thinking", "temperature", "top_p", "top_k"):
        assert forbidden not in sent
    assert sent["messages"][-1]["role"] == "user"  # never an assistant prefill
    assert set(sent) == {
        "model", "max_tokens", "system", "tools", "tool_choice", "messages", "output_config"
    }  # fmt: skip


def test_tool_choice_none_and_a_forced_choice() -> None:
    client, messages = _real(_answer([_text("ok")]))
    client.complete(request(tool_choice={"type": "none"}))
    assert messages.kwargs["tool_choice"] == {"type": "none"}
    for forced in ({"type": "any"}, {"type": "tool", "name": "list_sales"}):
        with pytest.raises(ValueError):  # a 400 on Sonnet 5.5: refused before it is sent
            client.complete(request(tool_choice=forced))


def test_an_assistant_message_last_is_refused_before_it_is_sent() -> None:
    client, _ = _real(_answer([_text("ok")]))
    with pytest.raises(ValueError):
        client.complete(request(messages=[{"role": "assistant", "content": "prefill"}]))


def test_the_sdk_is_built_with_the_timeout_and_a_limited_retry() -> None:
    client = AnthropicChatClient("sk-test-not-real", timeout_s=7, max_retries=2)
    assert client._client.max_retries == 2 and client._client.timeout == 7


def test_the_base_url_is_explicit_and_the_environment_cannot_redirect_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example")
    client = AnthropicChatClient("sk-test-not-real", timeout_s=7, max_retries=0)
    assert str(client._client.base_url) == "https://api.anthropic.com"
    assert API_BASE_URL == "https://api.anthropic.com"


# --- what comes back ----------------------------------------------------------------------------


def test_text_usage_and_cache_fields_are_read() -> None:
    client, _ = _real(_answer([_text("Bonjour "), _text("Marie")]))
    got = client.complete(request())
    assert got.stop_reason == "end_turn" and got.text == "Bonjour Marie" and got.tool_uses == ()
    assert (got.input_tokens, got.cache_write_tokens, got.cache_read_tokens) == (400, 2100, 0)
    assert got.output_tokens == 90 and got.model == HAIKU


def test_missing_cache_counts_are_zero() -> None:
    client, _ = _real(
        _answer(
            [_text("x")],
            usage=_usage(cache_creation_input_tokens=None, cache_read_input_tokens=None),
        )
    )
    got = client.complete(request())
    assert (got.cache_write_tokens, got.cache_read_tokens) == (0, 0)


def test_tool_uses_are_read_in_order_and_the_content_is_passed_back_as_it_came() -> None:
    blocks = [
        _text("Je regarde."),
        _tool_use("list_sales", {"day": "2026-10-23"}, "toolu_a"),
        _tool_use("open_flags", {}, "toolu_b"),
    ]
    client, _ = _real(_answer(blocks, stop_reason="tool_use"))
    got = client.complete(request())
    assert got.stop_reason == "tool_use"
    assert got.tool_uses == (
        ToolUse("toolu_a", "list_sales", {"day": "2026-10-23"}),
        ToolUse("toolu_b", "open_flags", {}),
    )
    assert got.assistant_content == blocks and got.assistant_content is not blocks


def test_a_tool_input_that_is_not_an_object_becomes_empty() -> None:
    client, _ = _real(_answer([_tool_use("list_sales", "oops")], stop_reason="tool_use"))
    assert client.complete(request()).tool_uses == (ToolUse("toolu_1", "list_sales", {}),)


def test_a_refusal_and_a_cut_answer_are_returned_not_raised() -> None:
    for reason in ("refusal", "max_tokens"):
        client, _ = _real(_answer([], stop_reason=reason))
        got = client.complete(request())
        assert got.stop_reason == reason and got.text == ""


def test_an_unknown_stop_reason_is_kept_as_text() -> None:
    client, _ = _real(_answer([_text("x")], stop_reason="pause_turn"))
    assert client.complete(request()).stop_reason == "pause_turn"
    client, _ = _real(_answer([_text("x")], stop_reason=None))
    assert client.complete(request()).stop_reason == "end_turn"


# --- errors -------------------------------------------------------------------------------------


def _status_error(code: int) -> Exception:
    import anthropic
    import httpx2 as httpx

    req = httpx.Request("POST", "https://api.invalid/v1/messages")
    response = httpx.Response(code, request=req, json={"error": {"message": "PNR ABC123"}})
    cls = {
        401: anthropic.AuthenticationError,
        429: anthropic.RateLimitError,
        400: anthropic.BadRequestError,
        500: anthropic.InternalServerError,
    }[code]
    return cls("secret body PNR ABC123", response=response, body=None)


@pytest.mark.parametrize(
    ("code", "kind"), [(401, "auth"), (429, "rate_limit"), (400, "request"), (500, "server")]
)
def test_sdk_errors_become_a_kind_without_the_message(code: int, kind: str) -> None:
    client, _ = _real(_status_error(code))
    with pytest.raises(AiUnavailable) as caught:
        client.complete(request())
    assert caught.value.kind == kind
    assert "ABC123" not in str(caught.value) and caught.value.__cause__ is None


def test_timeouts_and_connection_errors() -> None:
    import anthropic
    import httpx2 as httpx

    req = httpx.Request("POST", "https://api.invalid/v1/messages")
    for error, kind in (
        (anthropic.APITimeoutError(request=req), "timeout"),
        (anthropic.APIConnectionError(request=req), "connection"),
    ):
        client, _ = _real(error)
        with pytest.raises(AiUnavailable) as caught:
            client.complete(request())
        assert caught.value.kind == kind


def test_the_sdk_message_and_the_conversation_never_reach_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = _real(_status_error(400))
    with caplog.at_level(logging.DEBUG), pytest.raises(AiUnavailable):
        client.complete(request(messages=[{"role": "user", "content": "SECRET-QUESTION"}]))
    logged = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert "ABC123" not in logged and "SECRET-QUESTION" not in logged
    assert "sk-test-not-real" not in logged


# --- the fake -----------------------------------------------------------------------------------


def test_the_fake_answers_in_order_records_deep_copies_and_runs_out() -> None:
    fake = FakeChatClient([fake_tool("list_sales", {"day": "2026-10-23"}), fake_text("Voilà")])
    req = request()
    first = fake.complete(req)
    assert first.stop_reason == "tool_use" and first.tool_uses[0].name == "list_sales"
    assert (first.input_tokens, first.cache_write_tokens, first.cache_read_tokens) == (1500, 0, 0)
    assert first.output_tokens == 120 and first.model == HAIKU
    req.messages.append({"role": "user", "content": "changed afterwards"})
    second = fake.complete(request())
    assert second.stop_reason == "end_turn" and second.text == "Voilà"
    assert len(fake.requests) == 2 and len(fake.requests[0].messages) == 1
    with pytest.raises(AssertionError):
        fake.complete(request())


def test_the_fake_raises_scripted_errors_and_takes_a_callable() -> None:
    fake = FakeChatClient([AiUnavailable("timeout")])
    with pytest.raises(AiUnavailable):
        fake.complete(request())
    calls = FakeChatClient(lambda req: fake_text(req.model))
    assert calls.complete(request(model="claude-sonnet-5-5")).text == "claude-sonnet-5-5"


def test_fake_helpers_build_parallel_tool_calls_and_keep_ids_apart() -> None:
    got = FakeChatClient([fake_tools([("list_sales", {}), ("open_flags", {})])]).complete(request())
    assert [t.name for t in got.tool_uses] == ["list_sales", "open_flags"]
    assert len({t.id for t in got.tool_uses}) == 2
    assert isinstance(got, ChatResponse)
