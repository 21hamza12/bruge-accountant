"""The chat page: who may open it, what a message does, CSRF, rate limit, limits on the text,
privacy between people, escaping, the paste hand-off, history and the query budget. Synthetic."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, func, select

from app import sales
from app.ai.client import AiUnavailable
from app.ai.fake import FakeChatClient, fake_text, fake_tool
from app.auth import ratelimit
from app.chat import store
from app.chat.agent import AI_DOWN
from app.models import (
    CHAT_APP,
    CHAT_USER,
    ROLE_ACCOUNTANT,
    AgencyAiSettings,
    AiUsage,
    AuditEvent,
    ChatMessage,
    Sale,
    User,
)
from app.sales import SaleInput, TicketInput
from app.settings import Settings
from app.tenancy import rebind_tenant
from app.web.deps import AuthContext, current_user, require_login
from tests.page_support import Env
from tests.web_support import (
    PASSWORD_S,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    owner_session,
)

ACCOUNTANT_PHONE = "+22212345671"
PNR = (Path(__file__).parent / "fixtures" / "pnr" / "three_adults_rm.txt").read_text("utf-8")
HTMX = {"HX-Request": "true"}


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, "0004SV")
    return e


def accountant(env: Env) -> TestClient:
    add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        ACCOUNTANT_PHONE,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )
    return env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)


def say(c: TestClient, text: str, *, htmx: bool = False, token: str | None = None) -> Any:
    data = {"text": text, "csrf_token": token or csrf_for(c, "/chat")}
    return c.post("/chat", data=data, headers=HTMX if htmx else {}, follow_redirects=False)


def rows(env: Env, user_id: int | None = None, agency: int | None = None) -> list[ChatMessage]:
    agency = agency or env.seeded.agency_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, agency)
        query = select(ChatMessage).where(ChatMessage.agency_id == agency).order_by(ChatMessage.id)
        if user_id is not None:
            query = query.where(ChatMessage.user_id == user_id)
        found = list(s.scalars(query))
        s.expunge_all()
        return found


def thread(html: str) -> list[str]:
    """The text of each message of the page, in order (tags removed)."""
    items = re.findall(r'<li class="chat-msg[^"]*">(.*?)</li>', html, flags=re.DOTALL)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", item)).strip() for item in items]


def count_open(html: str) -> int:
    return len(re.findall(r'<li class="chat-msg', html))


# --- who may open it ----------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["manager", "staff", "accountant"])
def test_every_role_opens_the_chat(env: Env, who: str) -> None:
    c = (
        accountant(env)
        if who == "accountant"
        else env.manager_a()
        if who == "manager"
        else env.staff_a()
    )
    page = c.get("/chat")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    html = page.text
    assert "<title>Discussion</title>" in html
    assert 'hx-post="/chat"' in html and 'method="post" action="/chat"' in html
    assert re.search(r'name="csrf_token" value="[^"]+"', html)
    assert 'maxlength="16384"' in html and "<textarea" in html
    assert "Écrivez « aide »" in html  # an empty conversation explains itself
    assert "<script" not in html.replace('<script src="/static/', "")  # no inline script
    assert " style=" not in html  # no inline style


def test_the_sidebar_and_the_home_pages_link_to_the_chat(env: Env) -> None:
    env.load()
    for c in (env.manager_a(), env.staff_a()):
        home = c.get("/").text
        assert 'href="/chat"' in home.split('<div class="content-col">')[0]  # the sidebar
        assert 'class="chat-entry"' in home  # the way in on a phone (the bottom bar is full)
        assert 'aria-current="page"' in c.get("/chat").text.split('href="/chat"')[1][:80]


def test_a_visitor_is_sent_to_the_login(env: Env) -> None:
    c = env.factory()
    assert c.get("/chat", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/chat", data={"text": "aide"}, follow_redirects=False).status_code in (401, 403)
    htmx = c.get("/chat", headers=HTMX, follow_redirects=False)
    assert htmx.status_code == 401 and htmx.headers["hx-redirect"] == "/login"


def test_a_role_nobody_mentions_gets_the_403_page_and_an_audit_event(env: Env) -> None:
    from sqlalchemy.orm import object_session

    app: FastAPI = env.factory.app  # type: ignore[attr-defined]

    def odd_user(auth: AuthContext = Depends(require_login)) -> AuthContext:  # noqa: B008
        session = object_session(auth.user)
        assert session is not None
        session.expunge(auth.user)
        auth.user.role = "root"
        return auth

    c = env.staff_a()
    app.dependency_overrides[current_user] = odd_user
    try:
        assert c.get("/chat").status_code == 403
    finally:
        app.dependency_overrides.clear()
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert [e.details["path"] for e in denied] == ["/chat"]
    assert rows(env) == []


# --- a message ----------------------------------------------------------------------------------


def test_a_message_is_answered_and_both_are_kept(env: Env) -> None:
    c = env.manager_a()
    sent = say(c, "aide")
    assert sent.status_code == 303 and sent.headers["location"] == "/chat#chat-form"
    saved = rows(env)
    assert [(m.role, m.user_id) for m in saved] == [
        (CHAT_USER, env.seeded.manager_a),
        (CHAT_APP, env.seeded.manager_a),
    ]
    assert saved[0].text == "aide" and saved[1].text.startswith("Voici ce que je sais faire")
    assert saved[0].created_at == saved[1].created_at == env.clock.now
    shown = thread(c.get("/chat").text)
    assert shown[0].startswith("aide") and "Voici ce que je sais faire" in shown[1]


def test_htmx_gets_the_whole_panel_with_an_empty_form(env: Env) -> None:
    c = env.staff_a()
    answer = say(c, "qu'est-ce qui bloque ?", htmx=True)
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    html = answer.text
    assert html.lstrip().startswith('<section class="chat" id="chat"')
    assert "<html" not in html and "sidebar" not in html  # a fragment, not a page
    assert "Rien ne bloque" in html
    assert re.search(r"<textarea[^>]*></textarea>", html)  # the form is back, empty
    assert 'hx-target="#chat"' in html and 'hx-swap="outerHTML"' in html
    assert len(rows(env, env.seeded.staff_a)) == 2


def test_a_message_that_is_not_understood_gets_the_polite_answer(env: Env) -> None:
    c = env.staff_a()
    say(c, "bonjour tout le monde")
    last = rows(env)[-1].text
    assert last.startswith("Je n'ai pas compris votre demande.\nVoici ce que je sais faire")
    assert "bonjour tout le monde" not in last


def test_the_replies_are_the_users_own_lookups(env: Env) -> None:
    env.load()
    c = accountant(env)
    say(c, "feuille du 1er octobre")
    reply = rows(env)[-1].text
    assert "Feuille TJQ APRES du 1 octobre 2026" in reply
    html = c.get("/chat").text
    assert 'href="/days/2026-10-01/apres-preview.xlsx"' in html
    staff = env.staff_a()
    say(staff, "feuille du 1er octobre")
    assert (
        rows(env, env.seeded.staff_a)[-1].text
        == "Cette demande n'est pas disponible pour votre rôle."
    )


def test_a_ticket_lookup_goes_through_the_page(env: Env) -> None:
    env.load()
    c = env.manager_a()
    assert "billet" in c.get("/chat").text  # the empty conversation mentions it
    say(c, "billet 3940000777")
    assert rows(env)[-1].text == "Aucun billet 3940000777 trouvé parmi vos ventes."


# --- CSRF and origin ----------------------------------------------------------------------------


def test_a_post_without_the_csrf_token_is_refused_and_audited(env: Env) -> None:
    c = env.manager_a()
    r = c.post("/chat", data={"text": "aide"}, follow_redirects=False)
    assert r.status_code == 403
    assert say(c, "aide", token="x" * 43).status_code == 403
    assert rows(env) == []
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "csrf_failed")) == 2


def test_another_users_token_does_not_work(env: Env) -> None:
    mine, theirs = env.manager_a(), env.staff_a()
    foreign = csrf_for(theirs, "/chat")
    assert say(mine, "aide", token=foreign).status_code == 403
    assert rows(env) == []


def test_a_post_from_another_origin_is_refused(env: Env) -> None:
    c = env.manager_a()
    token = csrf_for(c, "/chat")
    r = c.post(
        "/chat",
        data={"text": "aide", "csrf_token": token},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and rows(env) == []


# --- limits -------------------------------------------------------------------------------------


def test_the_length_limit_is_500_characters(env: Env) -> None:
    c = env.manager_a()
    assert say(c, "a" * store.MAX_INPUT_CHARS).status_code == 303
    too_long = say(c, "a" * (store.MAX_INPUT_CHARS + 1))
    assert too_long.status_code == 400 and store.TOO_LONG in too_long.text
    assert too_long.headers["cache-control"] == "no-store"
    assert len(rows(env)) == 2  # only the first one was taken
    htmx = say(c, "a" * 501, htmx=True)  # htmx would not swap a 400: a 200 with the notice
    assert htmx.status_code == 200 and store.TOO_LONG in htmx.text
    assert len(rows(env)) == 2


def test_the_limit_counts_the_cleaned_text(env: Env) -> None:
    c = env.manager_a()
    spaced = "aide" + " " * 2000  # blanks are not text
    assert say(c, spaced).status_code == 303
    assert rows(env)[0].text == "aide"


@pytest.mark.parametrize("text", ["", "   ", "\n\t\r", "​‎"])
def test_an_empty_message_is_not_taken(env: Env, text: str) -> None:
    c = env.manager_a()
    r = say(c, text)
    assert r.status_code == 400 and store.EMPTY in r.text
    assert rows(env) == []


def test_a_message_without_a_text_field_is_not_a_422(env: Env) -> None:
    c = env.manager_a()
    r = c.post("/chat", data={"csrf_token": csrf_for(c, "/chat")}, follow_redirects=False)
    assert r.status_code == 400 and rows(env) == []


def test_messages_are_rate_limited_per_user(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "CHAT_USER", ratelimit.Limit("chat_user", 2, 3600))
    c = env.manager_a()
    assert say(c, "aide").status_code == 303
    assert say(c, "aide").status_code == 303
    third = say(c, "aide")
    assert third.status_code == 429 and int(third.headers["retry-after"]) > 0
    assert "Trop de messages" in third.text
    htmx = say(c, "aide", htmx=True)  # swapped in place, still told to wait
    assert htmx.status_code == 200 and "Trop de messages" in htmx.text
    assert int(htmx.headers["retry-after"]) > 0
    assert len(rows(env)) == 4  # the refused ones are not kept
    other = say(env.staff_a(), "aide")  # another person has their own budget
    assert other.status_code == 303
    limited = audit_events(env.db.owner_url, env.seeded.agency_a, "rate_limited")
    assert len(limited) == 1 and limited[0].details == {"scope": "chat_user"}
    env.clock.advance(hours=1)  # a new window
    assert say(c, "aide").status_code == 303


def test_the_default_budget_is_sixty_an_hour() -> None:
    assert (ratelimit.CHAT_USER.max_hits, ratelimit.CHAT_USER.window_seconds) == (60, 3600)


# --- privacy ------------------------------------------------------------------------------------


def test_each_person_sees_only_their_own_conversation(env: Env) -> None:
    manager, staff = env.manager_a(), env.staff_a()
    say(staff, "mes billets zq-staff-secret")
    say(manager, "aide")
    other_agency = env.manager_b()
    say(other_agency, "flags")
    assert "zq-staff-secret" not in manager.get("/chat").text  # not even the manager reads it
    assert "zq-staff-secret" in staff.get("/chat").text
    assert "Voici ce que je sais faire" not in other_agency.get("/chat").text
    assert {m.user_id for m in rows(env)} == {env.seeded.manager_a, env.seeded.staff_a}
    assert {m.user_id for m in rows(env, agency=env.seeded.agency_b)} == {env.seeded.manager_b}


def test_no_message_text_reaches_the_audit_trail_or_the_log(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "zq-distinctive-text-4242"
    c = env.staff_a()
    with caplog.at_level(logging.DEBUG):
        say(c, f"billet 3940000555 {marker}")
        say(c, PNR)
    everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert marker not in everything and "SYNTH" not in everything
    assert "3940000555" not in everything
    chat_logs = [r for r in caplog.records if r.name == "app.chat"]
    assert [(r.outcome, r.paste) for r in chat_logs] == [("ok", False), ("ok", True)]  # type: ignore[attr-defined]
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        events = list(s.scalars(select(AuditEvent)))
    assert not [e for e in events if marker in str(e.details)]
    assert not [e for e in events if "chat" in e.event_type]  # nothing sensitive is audited


def test_what_the_person_typed_is_escaped_and_never_echoed(env: Env) -> None:
    c = env.manager_a()
    hostile = '<script>alert(1)</script> "><img src=x onerror=alert(2)> {{7*7}} ${7*7}'
    say(c, hostile)
    html = c.get("/chat").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # their own message, escaped
    assert "{{7*7}} ${7*7}" in thread(html)[0]  # shown as typed, never evaluated
    reply = rows(env)[-1].text
    assert "script" not in reply and "img" not in reply  # the reply does not echo it
    fragment = say(c, hostile, htmx=True).text
    assert "<script>alert(1)" not in fragment and "&lt;script&gt;" in fragment


def test_a_stored_reply_with_a_foreign_link_shows_as_plain_text(env: Env) -> None:
    uid = env.seeded.manager_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        for target in (
            "https://evil.example/x",
            "javascript:alert(1)",
            "//evil.example",
            "/a/../b",
        ):
            s.add(
                ChatMessage(
                    agency_id=env.seeded.agency_a,
                    user_id=uid,
                    role=CHAT_APP,
                    text=f"voir ici\t{target}\nbouton\t>{target}",
                    created_at=env.clock.now,
                )
            )
        s.add(
            ChatMessage(
                agency_id=env.seeded.agency_a,
                user_id=uid,
                role=CHAT_APP,
                text="vente\t/sales/12\nbouton\t>/sales/12/void",
                created_at=env.clock.now,
            )
        )
        s.commit()
    html = env.manager_a().get("/chat").text
    messages = html.split('id="chat-thread"')[1].split("</ol>")[0]
    hrefs = re.findall(r'href="([^"]*)"', messages)
    assert "evil.example" not in messages and "voir ici" in messages  # only the label is shown
    assert hrefs == ["/sales/12", "/sales/12/void"]
    assert "javascript:" not in " ".join(hrefs)


# --- the paste hand-off -------------------------------------------------------------------------


def test_a_pasted_screen_opens_the_prefilled_entry_and_is_not_kept(env: Env) -> None:
    c = env.staff_a()
    before = sales_count(env)
    answer = say(c, PNR, htmx=True)
    assert answer.status_code == 200
    html = answer.text
    assert 'action="/sales/paste"' in html and 'name="pnr_text"' in html
    assert "Ouvrir la saisie préremplie" in html
    assert "vente(s) dans ce texte" in html
    stored = rows(env, env.seeded.staff_a)
    assert [m.text for m in stored][0] == store.PASTE_PLACEHOLDER
    assert "SYNTH" not in " ".join(m.text for m in stored)
    assert sales_count(env) == before  # reading it saves nothing
    # the next visit shows the reply with a plain link and no form carrying the text
    later = c.get("/chat").text
    assert 'action="/sales/paste"' not in later and "SYNTH" not in later
    assert 'href="/sales/new"' in later
    # the hand-off is the existing paste step: same fields, same prefilled form
    form = re.search(r'<form class="chat-handoff".*?</form>', html, flags=re.DOTALL)
    assert form is not None
    token = re.search(r'name="csrf_token" value="([^"]+)"', form.group(0))
    assert token is not None
    text = re.search(r'name="pnr_text" value="([^"]*)"', form.group(0))
    assert text is not None
    opened = c.post(
        "/sales/paste", data={"pnr_text": unescape(text.group(1)), "csrf_token": token.group(1)}
    )
    assert opened.status_code == 200 and "Ventes lues dans le texte" in opened.text


def sales_count(env: Env) -> int:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        return int(s.scalar(select(func.count()).select_from(Sale)) or 0)


def test_a_paste_without_htmx_shows_the_page_with_the_hand_off_form(env: Env) -> None:
    c = env.staff_a()
    r = say(c, PNR)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "<html" in r.text and 'class="chat-handoff"' in r.text
    assert 'action="/sales/paste"' in r.text and "Ouvrir la saisie préremplie" in r.text
    assert rows(env, env.seeded.staff_a)[0].text == store.PASTE_PLACEHOLDER
    assert 'class="chat-handoff"' not in c.get("/chat").text  # for this response only


def test_a_paste_over_16_kb_is_refused(env: Env) -> None:
    c = env.staff_a()
    r = say(c, "RP/X\n" + "A" * 17_000)
    assert r.status_code == 400 and "Texte trop long" in r.text
    assert rows(env) == []


# --- history ------------------------------------------------------------------------------------


def test_the_page_shows_the_last_fifty_messages_of_today(env: Env) -> None:
    uid = env.seeded.manager_a
    start = datetime(2026, 10, 18, tzinfo=UTC)  # the clock's day (T0 is noon on the 18th)
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        for n in range(1, 61):
            s.add(
                ChatMessage(
                    agency_id=env.seeded.agency_a,
                    user_id=uid,
                    role=CHAT_USER,
                    text=f"msg-{n:03d}",
                    created_at=start + timedelta(minutes=n),
                )
            )
        s.commit()
    html = env.manager_a().get("/chat").text
    assert count_open(html) == 50
    shown = thread(html)
    assert shown[0].startswith("msg-011") and shown[-1].startswith("msg-060")
    assert "msg-010" not in html


def test_messages_come_back_oldest_first_even_with_equal_times(env: Env) -> None:
    c = env.manager_a()
    for text in ("aide", "flags"):
        say(c, text)
    assert [m.role for m in rows(env)] == [CHAT_USER, CHAT_APP, CHAT_USER, CHAT_APP]
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        user = s.get(User, env.seeded.manager_a)
        assert user is not None
        found = store.recent(s, user, env.clock.now)
    assert [m.text[:5] for m in found][0] == "aide"
    assert found[0].id < found[1].id < found[2].id < found[3].id


def add_message(env: Env, user_id: int, text: str, at: datetime, role: str = CHAT_USER) -> None:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        s.add(
            ChatMessage(
                agency_id=env.seeded.agency_a, user_id=user_id, role=role, text=text, created_at=at
            )
        )
        s.commit()


def test_only_the_current_day_is_shown_older_messages_are_kept(env: Env) -> None:
    uid = env.seeded.manager_a
    late = datetime(2026, 10, 17, 23, 59, 59, tzinfo=UTC)
    add_message(env, uid, "hier-question", late)
    add_message(env, uid, "hier-reponse", late, CHAT_APP)
    add_message(env, uid, "ce-matin", datetime(2026, 10, 18, 0, 0, 0, tzinfo=UTC))
    html = env.manager_a().get("/chat").text
    assert "ce-matin" in html and "hier-" not in html
    assert len(rows(env, uid)) == 3  # append-only: still there, just not shown
    env.clock.advance(days=1)  # the next day starts a fresh conversation
    assert "ce-matin" not in env.manager_a().get("/chat").text


def test_an_old_all_agency_reply_is_not_shown_again_to_a_demoted_user(env: Env) -> None:
    old = datetime(2026, 10, 17, 9, tzinfo=UTC)
    add_message(env, env.seeded.staff_a, "Ventes du 17 octobre : 40 vente(s).", old, CHAT_APP)
    assert "Ventes du 17 octobre" not in env.staff_a().get("/chat").text


# --- the query budget ---------------------------------------------------------------------------

CEILING = 12  # the budget of test_query_counts.py


@contextmanager
def counting(engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def listener(*args: Any) -> None:
        seen.append(args[2])

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def statements(env: Env, c: TestClient) -> list[str]:
    engine: Engine = env.factory.app.state.engine  # type: ignore[attr-defined]
    assert c.get("/chat").status_code == 200  # warm-up
    with counting(engine) as seen:
        assert c.get("/chat").status_code == 200
    return list(seen)


@pytest.mark.parametrize("who", ["manager", "staff"])
def test_the_page_sends_a_constant_number_of_statements(env: Env, who: str) -> None:
    env.load()
    c = env.manager_a() if who == "manager" else env.staff_a()
    empty = statements(env, c)
    assert 3 <= len(empty) <= CEILING, empty
    uid = env.seeded.manager_a if who == "manager" else env.seeded.staff_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        for n in range(80):
            s.add(
                ChatMessage(
                    agency_id=env.seeded.agency_a,
                    user_id=uid,
                    role=CHAT_USER if n % 2 else CHAT_APP,
                    text=f"m{n}",
                    created_at=env.clock.now + timedelta(seconds=n),
                )
            )
        s.commit()
    full = statements(env, c)
    assert len(full) == len(empty), (empty, full)
    assert sum("chat_messages" in q for q in full) == 1  # one query for the whole conversation


# --- size is checked before anything is read ----------------------------------------------------


@pytest.mark.parametrize("count", [100_000, 2_000_000])
def test_a_flood_of_newlines_is_refused_fast(env: Env, count: int) -> None:
    c = env.manager_a()
    token = csrf_for(c, "/chat")
    for text in ("\n" * count + "x", "\n" * count):
        started = time.perf_counter()
        r = say(c, text, htmx=True, token=token)
        elapsed = time.perf_counter() - started
        # the form parser itself may refuse a body this big (400) before the route is reached
        assert r.status_code == 400 or "Texte trop long" in r.text
        assert elapsed < 1.0, elapsed
    assert rows(env) == []


def test_a_message_just_under_the_paste_cap_is_read_in_linear_time(env: Env) -> None:
    c = env.manager_a()
    started = time.perf_counter()
    r = say(c, "\n" * 16_000 + "x", htmx=True)
    assert time.perf_counter() - started < 1.0
    assert r.status_code == 200 and store.EMPTY not in r.text and "Texte trop long" not in r.text


def test_the_limit_is_in_utf8_bytes_too(env: Env) -> None:
    c = env.manager_a()
    r = say(c, "é" * 9000, htmx=True)  # 9000 characters, 18000 bytes
    assert "Texte trop long" in r.text and rows(env) == []


# --- a refused message comes back in the box ----------------------------------------------------


def box(html: str) -> str:
    found = re.search(r"<textarea[^>]*>(.*?)</textarea>", html, flags=re.DOTALL)
    assert found is not None
    return unescape(found.group(1))


def test_a_refused_message_is_put_back_in_the_text_box(env: Env) -> None:
    c = env.manager_a()
    long = "a<b> " * 150  # 750 characters
    for htmx in (True, False):
        r = say(c, long, htmx=htmx)
        assert store.TOO_LONG in r.text
        assert box(r.text) == long
    ok = say(c, "aide", htmx=True)
    assert box(ok.text) == ""  # a message that was taken empties the box


def test_a_rate_limited_message_is_put_back_in_the_text_box(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "CHAT_USER", ratelimit.Limit("chat_user", 1, 3600))
    c = env.manager_a()
    say(c, "aide")
    r = say(c, "mes billets d'hier", htmx=True)
    assert "Trop de messages" in r.text and box(r.text) == "mes billets d'hier"


def test_the_box_allows_a_paste_and_says_what_the_limits_are(env: Env) -> None:
    html = env.manager_a().get("/chat").text
    assert 'maxlength="16384"' in html
    assert "500 caractères" in html and "16 Ko" in html


# --- a failure inside the lookup ----------------------------------------------------------------


def test_an_error_in_a_lookup_is_answered_and_logged_without_the_text(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app.web.routes import chat as chat_route

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("zq-boom-text-777")

    monkeypatch.setattr(chat_route, "route_message", broken)
    c = env.manager_a()
    with caplog.at_level(logging.DEBUG):
        r = say(c, "mes billets zq-typed-888", htmx=True)
        plain = say(c, "aide")
    assert r.status_code == 200 and "Une erreur est survenue, réessayez." in r.text
    assert plain.status_code == 303
    everything = " ".join(f"{x.getMessage()} {x.__dict__} {x.exc_text}" for x in caplog.records)
    assert "zq-boom-text-777" not in everything and "zq-typed-888" not in everything
    done = [x for x in caplog.records if x.name == "app.chat" and x.getMessage() == "chat message"]
    assert [x.outcome for x in done] == ["error", "error"]  # type: ignore[attr-defined]
    assert rows(env)[1].text == "Une erreur est survenue, réessayez."


# --- a half-typed Amadeus line is not kept ------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "1.DUPONT/JEAN MR",
        "  2  FA PAX 235-3940299439/ETKL/MRU1000",
        "FA 235-3940299439/ETKL",
        "FP CASH",
        "DUPONT/JEAN MR",
    ],
)
def test_a_pasted_fragment_is_stored_as_the_placeholder(env: Env, text: str) -> None:
    c = env.staff_a()
    say(c, text)
    assert rows(env)[0].text == store.PASTE_PLACEHOLDER  # the reply is the app's own words


@pytest.mark.parametrize("text", ["billet 3940299439", "aide", "feuille TJQ/APRES du 19/09"])
def test_an_ordinary_message_is_stored_as_typed(env: Env, text: str) -> None:
    c = env.manager_a()
    say(c, text)
    assert rows(env)[0].text == text


# --- queries of one answer ----------------------------------------------------------------------


def sell_today(env: Env, doc: str) -> None:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        staff = s.get(User, env.seeded.staff_a)
        assert staff is not None
        ticket = TicketInput(
            doc_number=doc,
            airline="199",
            total_doc=Decimal("1000"),
            pax_name="SYNTH01/PAX",
            pax_type="ADT",
            rloc="ABC123",
            trnc="TKTT",
        )
        data = SaleInput(
            tickets=[ticket],
            ag_kind="direct",
            pb="2000",
            clt="Client Synth",
            payment_method="cash",
            staff_sign="0004SV",
        )
        sales.create_sale(s, staff, data, day=env.clock.now.date(), now=env.clock.now)
        s.commit()


def post_statements(env: Env, c: TestClient, text: str) -> list[str]:
    engine: Engine = env.factory.app.state.engine  # type: ignore[attr-defined]
    token = csrf_for(c, "/chat")
    with counting(engine) as seen:
        r = c.post("/chat", data={"text": text, "csrf_token": token}, headers=HTMX)
        assert r.status_code == 200
    return list(seen)


def test_my_tickets_reads_the_signs_once_and_stays_in_budget(env: Env) -> None:
    env.load()
    sell_today(env, "3940000777")
    c = env.staff_a()
    post_statements(env, c, "aide")  # warm-up
    seen = post_statements(env, c, "mes billets")
    assert sum("FROM staff_sign_links" in q for q in seen) == 1, seen
    assert sum("FROM comparison_flags" in q for q in seen) == 1, seen
    assert len(seen) <= 3 * CEILING, seen


def test_a_ticket_lookup_filters_the_flags_by_ticket_in_the_query(env: Env) -> None:
    env.load()
    sell_today(env, "3940000777")
    c = env.staff_a()
    post_statements(env, c, "aide")
    seen = post_statements(env, c, "billet 3940000777")
    flag_queries = [q for q in seen if "FROM comparison_flags" in q]
    assert len(flag_queries) == 1 and re.search(r"doc_number = \?", flag_queries[0]), flag_queries
    assert sum("FROM staff_sign_links" in q for q in seen) <= 2, seen
    assert len(seen) <= 3 * CEILING, seen


# --- phase E part 2: the model answers what the rules do not understand ------------------------


def with_chat_ai(env: Env, replies: Any) -> FakeChatClient:
    fake = FakeChatClient(replies)
    env.factory.app.state.chat_client = fake  # type: ignore[attr-defined]
    return fake


def usage_rows(env: Env) -> list[AiUsage]:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        found = list(s.scalars(select(AiUsage).order_by(AiUsage.id)))
        s.expunge_all()
        return found


def test_without_a_chat_client_a_free_question_is_the_polite_answer(env: Env) -> None:
    assert env.factory.app.state.chat_client is None  # type: ignore[attr-defined]
    c = env.staff_a()
    say(c, "raconte-moi une histoire sur Dakar")
    assert rows(env)[-1].text.startswith("Je n'ai pas compris votre demande.")
    assert usage_rows(env) == []


def test_a_free_question_is_answered_by_the_model_and_the_links_work(env: Env) -> None:
    env.load()
    sell_today(env, "3940000777")
    fake = with_chat_ai(
        env,
        [
            fake_tool("list_sales", {"day": env.clock.now.date().isoformat()}),
            fake_text("Vous avez une vente aujourd'hui, & elle est complète <b>peut-être</b>."),
        ],
    )
    c = env.staff_a()
    answer = say(c, "comment se présente ma journée de travail ?", htmx=True)
    assert answer.status_code == 200
    html = answer.text
    assert "<b>" not in html and "&amp; elle est complète" in html  # escaped, tags gone
    link = re.search(r'<a class="chat-link" href="(/sales/\d+)"', html)
    assert link is not None and c.get(link.group(1)).status_code == 200
    assert [q.model for q in fake.requests] == ["claude-haiku-5-5"] * 2
    saved = rows(env)
    assert [m.role for m in saved] == [CHAT_USER, CHAT_APP]
    assert saved[0].text == "comment se présente ma journée de travail ?"
    assert saved[1].text.startswith("Vous avez une vente aujourd'hui, & elle est complète")
    assert f"\t{link.group(1)}" in saved[1].text  # the tool's own link, stored as a link
    assert [(u.purpose, u.outcome) for u in usage_rows(env)] == [("chat", "ok")] * 2


def test_the_model_is_not_asked_when_a_rule_understands(env: Env) -> None:
    fake = with_chat_ai(env, [])
    c = env.staff_a()
    say(c, "aide")
    say(c, "billet 3940000777")
    say(c, "mes billets")
    assert fake.requests == [] and usage_rows(env) == []


def test_an_amadeus_paste_never_reaches_the_model(env: Env) -> None:
    fake = with_chat_ai(env, [])
    say(env.staff_a(), PNR)
    assert fake.requests == []


def test_a_failing_model_is_a_polite_answer_never_a_500(env: Env) -> None:
    c = env.staff_a()
    for htmx in (True, False):
        with_chat_ai(env, [AiUnavailable("server")])
        answer = say(c, "une question libre sur le monde", htmx=htmx)
        assert answer.status_code == (200 if htmx else 303)
    assert AI_DOWN in rows(env)[-1].text
    shown = thread(c.get("/chat").text)
    assert AI_DOWN in unescape(shown[-1]) and "Voici ce que je sais faire" in shown[-1]


def test_a_bug_in_the_client_is_the_same_polite_answer(env: Env) -> None:
    def broken(request: Any) -> Any:
        raise RuntimeError("zq-secret-9")

    with_chat_ai(env, broken)
    c = env.staff_a()
    answer = say(c, "une question libre", htmx=True)
    assert answer.status_code == 200 and "zq-secret-9" not in answer.text
    assert AI_DOWN in rows(env)[-1].text


def test_the_history_sent_is_the_persons_own_messages_of_today_only(env: Env) -> None:
    fake = with_chat_ai(env, lambda request: fake_text("Oui."))
    c = env.staff_a()
    say(env.manager_a(), "message du gérant")
    say(c, "premier message libre")
    say(c, "deuxième message libre")
    say(c, "aide")  # answered by the rules: an app reply is stored, never sent
    say(c, PNR)  # a placeholder is stored, never sent
    say(c, "troisième message libre")
    say(c, "quatrième message libre")
    say(c, "cinquième message libre")
    turn = fake.requests[-1].messages[0]["content"][0]["text"]
    assert "- troisième message libre" in turn and "- quatrième message libre" in turn
    assert "- aide" in turn  # the latest three of the person's own, whoever answered them
    assert "deuxième message libre" not in turn and "du gérant" not in turn
    assert "Voici ce que je sais faire" not in turn and "Texte Amadeus" not in turn
    assert turn.endswith("Message : cinquième message libre") and turn.count("\n- ") == 3


def test_a_typed_message_answered_by_the_rules_is_still_context(env: Env) -> None:
    fake = with_chat_ai(env, lambda request: fake_text("Oui."))
    c = env.staff_a()
    say(c, "aide")
    say(c, PNR)
    say(c, "une question libre")
    turn = fake.requests[-1].messages[0]["content"][0]["text"]
    assert "- aide" in turn and "Texte Amadeus" not in turn and turn.count("\n- ") == 1


def test_the_hourly_limit_of_free_questions_is_a_normal_answer(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "CHAT_AI_USER", ratelimit.Limit("chat_ai_user", 1, 3600))
    fake = with_chat_ai(env, [fake_text("Bonjour.")])
    c = env.staff_a()
    say(c, "première question libre")
    answer = say(c, "seconde question libre", htmx=True)
    assert answer.status_code == 200 and "Trop de questions libres" in answer.text
    assert len(fake.requests) == 1
    assert say(c, "aide").status_code == 303  # the simple requests still work


def test_the_model_is_off_for_an_agency_that_switched_the_ai_off(env: Env) -> None:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        s.add(
            AgencyAiSettings(
                agency_id=env.seeded.agency_a, enabled=False, monthly_budget_micro_usd=5_000_000
            )
        )
        s.commit()
    fake = with_chat_ai(env, [fake_text("jamais")])
    say(env.staff_a(), "une question libre")
    assert fake.requests == [] and rows(env)[-1].text.startswith("Je n'ai pas compris")


def test_the_chat_client_exists_only_with_a_key_and_the_chat_switch(db_url: str) -> None:
    from pydantic import SecretStr

    from app.ai.chat_client import AnthropicChatClient
    from app.web.main import create_app

    def build(**kw: Any) -> Any:
        app = create_app(Settings(database_url=SecretStr(db_url), **kw))
        app.state.engine.dispose()
        return app.state.chat_client

    assert build() is None
    key = SecretStr("sk-test-not-real")
    assert isinstance(build(anthropic_api_key=key), AnthropicChatClient)
    assert build(anthropic_api_key=key, ai_chat_enabled=False) is None
    assert build(anthropic_api_key=key, ai_enabled=False) is None
    fake = FakeChatClient()
    app = create_app(Settings(database_url=SecretStr(db_url)), chat_client=fake)
    app.state.engine.dispose()
    assert app.state.chat_client is fake


def test_one_ai_turn_stays_within_the_query_budget(env: Env) -> None:
    env.load()
    sell_today(env, "3940000777")
    day = env.clock.now.date().isoformat()
    c = env.staff_a()
    with_chat_ai(env, lambda request: fake_text("Voilà."))
    post_statements(env, c, "une question libre pour chauffer")  # warm-up
    with_chat_ai(env, [fake_tool("list_sales", {"day": day}), fake_text("Une vente aujourd'hui.")])
    seen = post_statements(env, c, "comment se présente ma journée ?")
    assert sum("FROM chat_messages" in q for q in seen) == 2, seen  # history + the thread
    assert sum("FROM staff_sign_links" in q for q in seen) <= 2, seen
    assert sum("INSERT INTO ai_usage" in q for q in seen) == 2, seen
    assert len(seen) <= 6 * CEILING, seen
