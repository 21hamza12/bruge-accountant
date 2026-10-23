"""A scripted ``TranscribeClient`` for tests: it never touches the network.

``replies`` is what each call returns, in order: a text (fixed token counts), a ``Transcription``,
or an exception to raise. A callable receives ``(image_bytes, media_type, model)`` and returns one
of those. ``calls`` records the media type, the model and the size of each image (and the bytes,
so a test can check what would have been sent).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

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
