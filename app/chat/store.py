"""The conversation of one person, append-only (``chat_messages``, migration 0015).

Every query filters on the agency AND the user of the session: the conversation is the person's
own, and not even a manager reads another one. Nothing here takes an id from a request.
Neither a pasted Amadeus text nor a screenshot is stored (``PASTE_PLACEHOLDER`` takes its place).

The table is append-only and is kept for audit, but only the CURRENT DAY's conversation is shown
(``recent``): an old reply made with rights the person no longer has (a manager since demoted, a
sign since reassigned) never comes back on the screen. This module is the only one that names
``ChatMessage`` (a code guard checks it); the page reads it through ``StoredMessage``.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.router import looks_like_fragment
from app.models import CHAT_APP, CHAT_MAX_TEXT, CHAT_USER, ChatMessage, User

StoredMessage = ChatMessage  # the row type the page reads (see the module note)
HISTORY = 50  # messages shown
MAX_INPUT_CHARS = 500  # what a person may type (an Amadeus paste has its own, larger limit)
PASTE_PLACEHOLDER = "[Texte Amadeus collé]"
TOO_LONG = f"Message trop long : {MAX_INPUT_CHARS} caractères au plus."
EMPTY = "Écrivez un message."


def clean_message(raw: str) -> str:
    """The text of a typed message: NFC, one line, no control or invisible format character
    (a zero-width or bidirectional character lets one text pose as another)."""
    text = unicodedata.normalize("NFC", raw)
    text = "".join(" " if c in "\r\n\t" else c for c in text)
    text = "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf"))
    return " ".join(text.split())


def append(db: Session, user: User, role: str, text: str, now: datetime) -> ChatMessage:
    """Add one message to the user's conversation (the caller commits). The text is cut to what
    the table allows; the agency and the user are the actor's own."""
    if role not in (CHAT_USER, CHAT_APP):
        raise ValueError("a chat message is from the user or from the app")
    message = ChatMessage(
        agency_id=user.agency_id,
        user_id=user.id,
        role=role,
        text=(text.strip() or "…")[:CHAT_MAX_TEXT],
        created_at=now,
    )
    db.add(message)
    db.flush()
    return message


def stored_text(text: str, *, is_paste: bool) -> str:
    """What is kept of the person's message: the placeholder for a pasted screen or any piece of
    one (an element line, a fare line, a passenger name), else the text as typed."""
    return PASTE_PLACEHOLDER if is_paste or looks_like_fragment(text) else text


def day_start(now: datetime) -> datetime:
    """Midnight of the application clock's day (in the clock's own time zone)."""
    return datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)


def recent(db: Session, user: User, now: datetime, limit: int = HISTORY) -> list[ChatMessage]:
    """The last ``limit`` messages of this user's conversation since the start of today (the
    application clock's day), oldest first. Earlier ones stay in the table, unshown."""
    rows = db.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.agency_id == user.agency_id,
            ChatMessage.user_id == user.id,
            ChatMessage.created_at >= day_start(now),
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    )
    return list(reversed(list(rows)))
