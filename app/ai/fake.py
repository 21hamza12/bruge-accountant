"""A scripted ``TranscribeClient`` for tests: it never touches the network.

``replies`` is what each call returns, in order: a text (fixed token counts), a ``Transcription``,
or an exception to raise. A callable receives ``(image_bytes, media_type, model)`` and returns one
of those. ``calls`` records the media type, the model and the size of each image (and the bytes,
so a test can check what would have been sent).

``FakeChatClient`` is the same for the chat: ``replies`` are ``ChatResponse`` objects (built with
``fake_text``, ``fake_tool``, ``fake_tools``) or exceptions, in order, or a function of the
``ChatRequest``. ``requests`` keeps a deep copy of every request, so a test can check exactly what
would have been sent, even after the code goes on changing its own lists.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.ai.chat_client import ChatRequest, ChatResponse, ToolUse
from app.ai.client import Transcription

Reply = str | Transcription | Exception
DEFAULT_INPUT_TOKENS = 650
DEFAULT_OUTPUT_TOKENS = 400


@dataclass(frozen=True)
class Call:
    media_type: str
    model: str
    image: bytes


@dataclass
class FakeTranscribeClient:
    replies: list[Reply] | Callable[[bytes, str, str], Reply] = field(default_factory=list)
    input_tokens: int = DEFAULT_INPUT_TOKENS
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    calls: list[Call] = field(default_factory=list)

    def transcribe(self, image_bytes: bytes, media_type: str, model: str) -> Transcription:
        self.calls.append(Call(media_type, model, image_bytes))
        if callable(self.replies):
            reply = self.replies(image_bytes, media_type, model)
        elif self.replies:
            reply = self.replies.pop(0)
        else:
            raise AssertionError("the fake client has no reply left")
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, Transcription):
            return reply
        return Transcription(reply, self.input_tokens, self.output_tokens, model)


# --- the chat -----------------------------------------------------------------------------------

DEFAULT_CHAT_INPUT_TOKENS = 1500
DEFAULT_CHAT_OUTPUT_TOKENS = 120
DEFAULT_CHAT_MODEL = "claude-haiku-5-5"


def fake_response(
    stop_reason: str,
    text: str = "",
    tool_uses: Sequence[ToolUse] = (),
    *,
    model: str = DEFAULT_CHAT_MODEL,
    input_tokens: int = DEFAULT_CHAT_INPUT_TOKENS,
    cache_write: int = 0,
    cache_read: int = 0,
    output_tokens: int = DEFAULT_CHAT_OUTPUT_TOKENS,
) -> ChatResponse:
    content: list[Any] = [{"type": "text", "text": text}] if text else []
    content += [
        {"type": "tool_use", "id": t.id, "name": t.name, "input": copy.deepcopy(t.input)}
        for t in tool_uses
    ]
    return ChatResponse(
        stop_reason, text, tuple(tool_uses), content,
        input_tokens, cache_write, cache_read, output_tokens, model,
    )  # fmt: skip


def fake_text(text: str, **usage: Any) -> ChatResponse:
    """The model answers in words and stops."""
    return fake_response("end_turn", text, **usage)


def fake_tools(
    calls: Sequence[tuple[str, Any]], text: str = "", *, stop_reason: str = "tool_use", **usage: Any
) -> ChatResponse:
    """The model asks for several tools at once: ``[(name, input), ...]`` (ids ``t1``...)."""
    uses = [ToolUse(f"t{n}", name, args) for n, (name, args) in enumerate(calls, start=1)]
    return fake_response(stop_reason, text, uses, **usage)


def fake_tool(name: str, input: Any = None, id: str = "t1", **usage: Any) -> ChatResponse:
    """The model asks for one tool."""
    return fake_response(
        "tool_use", "", [ToolUse(id, name, {} if input is None else input)], **usage
    )


ChatReply = ChatResponse | Exception


@dataclass
class FakeChatClient:
    replies: list[ChatReply] | Callable[[ChatRequest], ChatReply] = field(default_factory=list)
    requests: list[ChatRequest] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(copy.deepcopy(request))
        if callable(self.replies):
            reply = self.replies(request)
        elif self.replies:
            reply = self.replies.pop(0)
        else:
            raise AssertionError("the fake chat client has no reply left")
        if isinstance(reply, Exception):
            raise reply
        return ChatResponse(
            reply.stop_reason, reply.text, reply.tool_uses, reply.assistant_content,
            reply.input_tokens, reply.cache_write_tokens, reply.cache_read_tokens,
            reply.output_tokens, request.model,
        )  # fmt: skip
