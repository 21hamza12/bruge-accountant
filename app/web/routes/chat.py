"""The chat page: ``GET /chat`` shows today's conversation (at most 50 messages) of the signed-in
person, ``POST /chat`` answers one message.

* **Who**: ``permissions.can_use_chat`` (staff, manager, accountant). The conversation is the
  person's own: the user and the agency come from the session, nothing from the request, and not
  even a manager reads another person's messages.
* **No write beyond the conversation**: the reply comes from ``app.chat.router`` (fixed,
  read-only lookups with the person's own rights). A message no rule understands goes to the
  model (``app.chat.agent``) when the app has a chat client, which can only run the same
  lookups; without one it is the polite "not understood". A pasted Amadeus text is handed to the
  existing
  paste screen by a form in THIS response only (``Reply.handoff_text``); the conversation stores a
  placeholder, never the text.
* **htmx**: the form posts with ``hx-post`` and the response is the whole panel (thread and an
  empty form), swapped in place. Without htmx the same POST redirects to the page (303), so a
  reload never sends the message again; except an Amadeus paste, whose hand-off form exists in that
  one response only: the page is rendered with it (200). htmx does not swap a 4xx answer, so for
  an htmx request a refused message (empty, too long, rate limited) is a 200 with a notice in the
  panel; any other request gets the real status (400, 429 with ``Retry-After``). A refused
  message is put back in the text box (up to the paste cap).
* **Limits**: 60 messages an hour for each user (``ratelimit.CHAT_USER``), counted before anything
  else is looked at; then the SIZE (``MAX_TEXT_BYTES``, characters and UTF-8 bytes) before any
  text is read; then a typed message is at most ``store.MAX_INPUT_CHARS`` characters (an Amadeus
  paste: the paste screen's 16 KB).
* **A failing lookup** is answered "Une erreur est survenue, réessayez." (outcome ``error``) and
  logged by exception name only.
* **Privacy**: no-store pages; CSRF on the router; the audit trail and the log get no message
  text, only the outcome name and counts. A reply is made of fixed phrases and typed values, so
  what the person typed is never echoed; every template value is autoescaped.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response

from app import audit, permissions
from app.auth import ratelimit
from app.chat import agent, ai_prompt, store
from app.chat import reply as r
from app.chat.reply import Line, Reply
from app.chat.router import UnknownHandler, looks_like_amadeus, polite_unknown, route_message
from app.chat.tools import ChatContext
from app.clock import Clock
from app.models import CHAT_APP, CHAT_USER
from app.parsing.pnr import MAX_TEXT_BYTES
from app.settings import Settings
from app.web import sale_paste
from app.web.csrf import csrf_protect
from app.web.deps import AuthContext, client_ip, get_clock, get_session, require_permission
from app.web.pages import page
from app.web.sale_pages import posted_fields
from app.web.shell import ACTIVE_CHAT, shell_context
from app.web.templating import render

log = logging.getLogger("app.chat")
router = APIRouter(prefix="/chat", dependencies=[Depends(csrf_protect)])

NO_STORE = {"Cache-Control": "no-store"}
Db = Annotated[Session, Depends(get_session)]
Now = Annotated[Clock, Depends(get_clock)]
Chatter = Annotated[AuthContext, Depends(require_permission(permissions.can_use_chat))]
Posted = Annotated[FormData, Depends(posted_fields)]
TEXT_FIELD = "text"
TOO_MANY = "Trop de messages pour le moment : réessayez dans quelques minutes."
FAILED = "Une erreur est survenue, réessayez."
MAX_DRAFT = MAX_TEXT_BYTES  # characters of a refused message put back in the box


@dataclass(frozen=True)
class MessageView:
    """One message as the template prints it. A person's message is plain text; a reply is the
    lines parsed back from its stored form (``reply.parse_stored``)."""

    from_user: bool
    text: str
    lines: tuple[Line, ...]
    at: datetime

    @property
    def stamp(self) -> str:
        return f"{self.at:%d/%m %H:%M}"


def _view(message: store.StoredMessage) -> MessageView:
    if message.role == CHAT_APP:
        return MessageView(False, "", r.parse_stored(message.text), message.created_at)
    return MessageView(message.role == CHAT_USER, message.text, (), message.created_at)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "").lower() == "true"


def _context(
    db: Session,
    auth: AuthContext,
    now: datetime,
    *,
    handoff: str | None = None,
    notice: str = "",
    draft: str = "",
) -> dict[str, Any]:
    return {
        "messages": [_view(m) for m in store.recent(db, auth.user, now)],
        "handoff": handoff,
        "paste_field": sale_paste.TEXT_FIELD,
        "notice": notice,
        "draft": draft,
        "max_chars": store.MAX_INPUT_CHARS,
        "max_paste_kb": MAX_TEXT_BYTES // 1024,
        "max_paste_chars": MAX_TEXT_BYTES,
    }


def _panel(
    db: Session,
    auth: AuthContext,
    now: datetime,
    *,
    handoff: str | None = None,
    notice: str = "",
    draft: str = "",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Only the panel (thread and form): the htmx answer."""
    body = render(
        "chat/_panel.html",
        csrf_token=auth.csrf_token,
        **_context(db, auth, now, handoff=handoff, notice=notice, draft=draft),
    )
    return Response(
        body,
        status_code=status_code,
        media_type="text/html",
        headers={**NO_STORE, **(headers or {})},
    )


def _full_page(
    db: Session,
    auth: AuthContext,
    now: datetime,
    *,
    handoff: str | None = None,
    notice: str = "",
    draft: str = "",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    return page(
        "chat/page.html",
        status_code=status_code,
        headers={**NO_STORE, **(headers or {})},
        **shell_context(db, auth, ACTIVE_CHAT),
        **_context(db, auth, now, handoff=handoff, notice=notice, draft=draft),
    )


def _refused(
    db: Session,
    request: Request,
    auth: AuthContext,
    now: datetime,
    notice: str,
    status_code: int,
    *,
    draft: str = "",
    headers: dict[str, str] | None = None,
) -> Response:
    """A message that was not taken: nothing is stored, the text goes back in the box. htmx gets a
    200 it will swap."""
    if _is_htmx(request):
        return _panel(db, auth, now, notice=notice, draft=draft, headers=headers)
    return _full_page(
        db, auth, now, notice=notice, draft=draft, status_code=status_code, headers=headers
    )


@router.get("")
def chat_page(db: Db, clock: Now, auth: Chatter) -> Response:
    return _full_page(db, auth, clock())


def _draft(raw: object) -> str:
    """The posted text to put back in the box after a refusal (never more than the paste cap)."""
    return raw[:MAX_DRAFT] if isinstance(raw, str) else ""


def _rate_limited(
    db: Session, request: Request, auth: AuthContext, now: datetime, raw: object
) -> Response | None:
    """Count this message against the user's budget (committed at once, so a refused message still
    counts); the refusal when it is spent. Only the first refusal of a window is a database row."""
    limit = ratelimit.CHAT_USER
    decision = ratelimit.hit(db, limit, str(auth.user.id), now)
    db.commit()
    if decision.allowed:
        return None
    user, ip, details = auth.user, client_ip(request), {"scope": limit.name}
    if decision.first_rejection:
        audit.record_and_commit(
            db,
            audit.RATE_LIMITED,
            agency_id=user.agency_id,
            actor_user_id=user.id,
            ip=ip,
            details=details,
            now=now,
        )
    else:
        audit.log_only(
            audit.RATE_LIMITED,
            agency_id=user.agency_id,
            actor_user_id=user.id,
            ip=ip,
            details=details,
        )
    return _refused(
        db,
        request,
        auth,
        now,
        TOO_MANY,
        429,
        draft=_draft(raw),
        headers={"Retry-After": str(decision.retry_after)},
    )


def _checked(raw: object) -> tuple[str, bool, str]:
    """``(text, is_paste, problem)`` of a posted message: the problem is a French message when the
    text must not be taken. The SIZE is checked first, before anything reads the text: the
    characters (cheap, and never more than the bytes), then the UTF-8 bytes."""
    if not isinstance(raw, str):
        return "", False, store.EMPTY
    if len(raw) > MAX_TEXT_BYTES or len(raw.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return "", False, sale_paste.TOO_LONG
    if not raw.strip():
        return "", False, store.EMPTY
    if looks_like_amadeus(raw):
        return raw, True, sale_paste.check_text(raw)
    text = store.clean_message(raw)
    if not text:
        return "", False, store.EMPTY
    if len(text) > store.MAX_INPUT_CHARS:
        return "", False, store.TOO_LONG
    return text, False, ""


def _unknown_handler(request: Request) -> UnknownHandler:
    """What answers a message no rule understood: the model when the app has a chat client (a key,
    AI and the chat not switched off), else the polite "not understood". The person's earlier
    messages are read lazily (a callable), only once every gate has passed and a model is really
    going to be asked: a fragment, an agency without AI, a spent allowance or budget costs no
    query for them."""
    client = getattr(request.app.state, "chat_client", None)
    if client is None:
        return polite_unknown
    settings: Settings = request.app.state.settings

    def ask(ctx: ChatContext, text: str) -> Reply:
        def history() -> list[str]:
            return store.recent_user_texts(ctx.db, ctx.user, ctx.now, ai_prompt.AI_HISTORY)

        return agent.answer_with_ai(ctx, text, client=client, settings=settings, history=history)

    return ask


def _answer(request: Request, ctx: ChatContext, db: Session, text: str) -> Reply:
    """The reply to one message. A lookup that fails is a polite error (the session is rolled
    back first), logged by the exception's name only: its message may hold the person's text."""
    try:
        return route_message(ctx, text, unknown=_unknown_handler(request))
    except Exception as exc:
        db.rollback()
        log.error("chat lookup failed", extra={"error": type(exc).__name__})
        return r.refuse(r.ERROR, r.say(FAILED))


@router.post("")
def chat_post(request: Request, db: Db, clock: Now, auth: Chatter, posted: Posted) -> Response:
    now = clock()
    raw = posted.get(TEXT_FIELD)
    limited = _rate_limited(db, request, auth, now, raw)
    if limited is not None:
        return limited
    text, is_paste, problem = _checked(raw)
    if problem:
        return _refused(db, request, auth, now, problem, 400, draft=_draft(raw))
    answer = _answer(request, ChatContext(db, auth.user, now), db, text)
    store.append(db, auth.user, CHAT_USER, store.stored_text(text, is_paste=is_paste), now)
    store.append(db, auth.user, CHAT_APP, answer.stored(), now)
    db.commit()
    log.info(
        "chat message",
        extra={
            "outcome": answer.outcome,
            "tool": answer.tool,
            "paste": is_paste,
            "chars": len(text),
        },
    )
    if _is_htmx(request):
        return _panel(db, auth, now, handoff=answer.handoff_text)
    if answer.handoff_text is not None:  # the form exists in this response only: no redirect
        return _full_page(db, auth, now, handoff=answer.handoff_text)
    return RedirectResponse("/chat#chat-form", status_code=303)


__all__ = ["router"]
