"""The chat client: an interface, and the real one on the Anthropic SDK (phase E part 2,
docs/design/13-chat-ai.md).

One ``complete`` call is one request to the Messages API with the tools of the person's role. The
real client sends ``tools`` (strict schemas), ``tool_choice`` ``auto`` or ``none`` (``any`` and
``tool`` are a 400 on Sonnet 5.5, so a request carrying them cannot even be built), the system
prompt as one cached block, ``output_config`` effort ``low`` and nothing else: no ``thinking``, no
sampling parameter, never an assistant prefill. The reply keeps the assistant content untouched
(``assistant_content``: a thinking block must go back as it came) next to the parts the code reads.

Timeouts and retries come from ``Settings``, errors are mapped exactly as for the transcriber
(``client.map_errors``): a kind, never the SDK's message, which can quote the conversation. Nothing
is logged here, and only the key is secret; it stays inside the SDK client object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.ai.client import API_BASE_URL, map_errors

MAX_OUTPUT_TOKENS = 300  # a chat answer is one to three sentences: this is the cost cap
EFFORT = "low"
ALLOWED_TOOL_CHOICES = ("auto", "none")


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ChatRequest:
    """``system`` is the text of the one system block; ``tools`` the definitions (``name``,
    ``description``, ``input_schema``, ``strict``); ``messages`` end with a user message."""

    model: str
    system: str
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    tool_choice: Mapping[str, Any]
    max_tokens: int = MAX_OUTPUT_TOKENS
    effort: str = EFFORT

    def __post_init__(self) -> None:
        if self.tool_choice.get("type") not in ALLOWED_TOOL_CHOICES or len(self.tool_choice) != 1:
            raise ValueError("tool_choice is auto or none: any and tool are a 400 on these models")
        if not self.messages or self.messages[-1].get("role") != "user":
            raise ValueError("the last message must be the user's: an assistant prefill is refused")


@dataclass(frozen=True)
class ChatResponse:
    """``stop_reason``: ``end_turn``, ``tool_use``, ``max_tokens``, ``refusal`` (or another value
    the API adds). ``input_tokens`` counts only what was neither written to nor read from the
    cache. ``assistant_content`` is opaque: it goes back as it came."""

    stop_reason: str
    text: str
    tool_uses: tuple[ToolUse, ...]
    assistant_content: list[Any]
    input_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    output_tokens: int
    model: str


class ChatModelClient(Protocol):
    def complete(self, request: ChatRequest) -> ChatResponse:
        """One model call. Raises ``AiUnavailable``."""
        ...


class AnthropicChatClient:
    """``ChatModelClient`` on the Anthropic Messages API."""

    def __init__(self, api_key: str, *, timeout_s: float, max_retries: int) -> None:
        import anthropic  # imported here: nothing else needs the SDK, and tests never build this

        self._anthropic = anthropic
        # base_url is explicit: the SDK would otherwise read ANTHROPIC_BASE_URL and send the key,
        # and every question, to whatever host it names.
        self._client = anthropic.Anthropic(
            api_key=api_key, base_url=API_BASE_URL, timeout=timeout_s, max_retries=max_retries
        )

    def complete(self, request: ChatRequest) -> ChatResponse:
        anthropic = self._anthropic
        # Any: the SDK types each of these as a union of TypedDicts; ours are built by the code.
        system: Any = [
            {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
        ]
        tools: Any = request.tools
        messages: Any = request.messages
        tool_choice: Any = dict(request.tool_choice)
        output_config: Any = {"effort": request.effort}
        with map_errors(anthropic):
            response = self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=system,
                tools=tools,
                tool_choice=tool_choice,
                messages=messages,
                output_config=output_config,
            )
        texts: list[str] = []
        tool_uses: list[ToolUse] = []
        for block in response.content:
            if isinstance(block, anthropic.types.TextBlock):
                texts.append(block.text)
            elif isinstance(block, anthropic.types.ToolUseBlock):
                args = block.input if isinstance(block.input, dict) else {}
                tool_uses.append(ToolUse(block.id, block.name, dict(args)))
        usage = response.usage
        return ChatResponse(
            stop_reason=response.stop_reason or "end_turn",
            text="".join(texts),
            tool_uses=tuple(tool_uses),
            assistant_content=list(response.content),
            input_tokens=int(usage.input_tokens),
            cache_write_tokens=int(usage.cache_creation_input_tokens or 0),
            cache_read_tokens=int(usage.cache_read_input_tokens or 0),
            output_tokens=int(usage.output_tokens),
            model=request.model,
        )
