"""What a lookup answers: a ``Reply`` (structured data and the lines that show it), and the one
text format those lines are stored in.

A reply is built only from fixed French phrases, the application's own records and typed values
(a date, a ten-digit number): it never copies what the person typed, so it carries no markup.
Links are built by the code (``/sales/12``), never taken from a message. When a stored reply is
read back, every link is checked again against ``SAFE_HREF``, so a row that somehow held a
foreign link would show as plain text.

Stored form, one line each: ``text``, or ``text<TAB>/path`` (a link), or ``text<TAB>>/path`` (a
link shown as a button). The text of the conversation is at most ``models.CHAT_MAX_TEXT``
characters: whole lines are dropped from the end, with a note, rather than cutting one in two.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.models import CHAT_MAX_TEXT

OK = "ok"
UNKNOWN = "unknown"  # nothing matched: the seam part 2 plugs the AI into
DENIED = "denied"  # a tool the role may not use
INVALID = "invalid"  # a tool name or an argument that is not usable
ERROR = "error"  # a lookup that failed on our side (the page says so, politely)

SAFE_HREF = re.compile(r"/(?:[A-Za-z0-9_\-.]+(?:/[A-Za-z0-9_\-.]+)*)?(?:\?[A-Za-z0-9_\-.=&]*)?")
MAX_LINE = 300
TRUNCATED = "… (réponse raccourcie)"
_BUTTON = ">"
_TAB = "\t"
_BREAKS = re.compile(r"[\t\r\n]+")


def safe_href(href: str) -> bool:
    return SAFE_HREF.fullmatch(href) is not None and ".." not in href


@dataclass(frozen=True)
class Line:
    text: str
    href: str | None = None
    button: bool = False

    def __post_init__(self) -> None:
        if self.href is not None and not safe_href(self.href):
            raise ValueError("a chat link must be a site path made of plain characters")


def say(text: str) -> Line:
    return Line(text)


def link(text: str, href: str) -> Line:
    return Line(text, href)


def button(text: str, href: str) -> Line:
    return Line(text, href, button=True)


@dataclass(frozen=True)
class Reply:
    """``outcome``: ``ok``, ``unknown``, ``denied`` or ``invalid``; ``tool``: the registry name
    that answered (None when none did); ``data``: the structured result (ids, counts, dates:
    what part 2 hands back to the model); ``handoff_text``: Amadeus text the page puts in a form
    for this one response only (never stored)."""

    outcome: str
    lines: tuple[Line, ...]
    tool: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    handoff_text: str | None = None

    @property
    def plain(self) -> str:
        """The reply as text for a reader that cannot click (links as ``label (path)``)."""
        return "\n".join(f"{x.text} ({x.href})" if x.href else x.text for x in self.lines)

    def stored(self) -> str:
        return serialize(self.lines)


def serialize(lines: tuple[Line, ...]) -> str:
    out: list[str] = []
    size = 0
    for line in lines:
        text = _BREAKS.sub(" ", line.text).strip()[:MAX_LINE]
        if not text:
            continue
        row = text
        if line.href is not None:
            row += _TAB + (_BUTTON if line.button else "") + line.href
        if size + len(row) + 1 > CHAT_MAX_TEXT - len(TRUNCATED) - 1:
            out.append(TRUNCATED)
            break
        out.append(row)
        size += len(row) + 1
    return "\n".join(out) or TRUNCATED


def parse_stored(text: str) -> tuple[Line, ...]:
    """The lines of a stored reply. A link that is not a safe site path becomes plain text."""
    lines: list[Line] = []
    for row in text.split("\n"):
        label, _, target = row.partition(_TAB)
        is_button = target.startswith(_BUTTON)
        href = target[1:] if is_button else target
        if target and safe_href(href):
            lines.append(Line(label, href, is_button))
        else:
            lines.append(Line(label))
    return tuple(lines)


def reply(tool: str, *lines: Line, **data: Any) -> Reply:
    return Reply(OK, tuple(lines), tool, data)


def refuse(outcome: str, *lines: Line, tool: str | None = None) -> Reply:
    return Reply(outcome, tuple(lines), tool)
