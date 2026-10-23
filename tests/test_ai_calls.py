"""The shared check-call-record path (app/ai/calls.py): the commit before a call is the chat's
choice (the screenshots keep their behaviour), and a failed commit is a failed call, not a 500."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.ai import budget, pricing
from app.ai.calls import BudgetRefused, Caller, CallFailed, Usage
from app.ai.fake import FakeChatClient, FakeTranscribeClient, fake_text
from app.ai.screenshots import read_screenshots
from app.chat import agent
from app.chat import reply as r
from app.chat.tools import ChatContext
from app.models import AiUsage, User
from app.settings import Settings
from tests.ai_support import png, pnr
from tests.test_ai_budget import agency_with_user

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
HAIKU = "claude-haiku-5-5"


def caller(db: Session, user: User, **kw: Any) -> Caller:
    return Caller(
        db,
        user,
        NOW,
        default_budget_usd=Decimal(5),
        purpose=budget.PURPOSE_CHAT,
        estimate=pricing.estimate_chat_micro_usd,
        **kw,
    )


class Spy:
    """Records, in order, the commits of the session and the calls made inside them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail_commit: int | None = None) -> None:
        self.events: list[str] = []
        self.commits = 0
        real = Session.commit
        spy = self

        def commit(session: Session) -> None:
            spy.commits += 1
            spy.events.append("commit")
            if spy.commits == fail_commit:
                raise OperationalError("COMMIT", {}, Exception("db down"))
            real(session)

        monkeypatch.setattr(Session, "commit", commit)

    def made(self) -> str:
        self.events.append("call")
        return "answer"


def test_the_commit_before_the_call_is_opt_in(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = agency_with_user(session)
    spy = Spy(monkeypatch)
    caller(session, user).call(HAIKU, "ok", spy.made, lambda _: Usage(10, 5))
    assert spy.events == ["call", "commit"]  # the default: no commit before the call
    spy.events.clear()
    caller(session, user, commit_before_call=True).call(
        HAIKU, "ok", spy.made, lambda _: Usage(10, 5)
    )
    assert spy.events == ["commit", "call", "commit"]


def test_a_failed_commit_before_the_call_is_a_failed_call_not_an_exception(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    user = agency_with_user(session)
    spy = Spy(monkeypatch, fail_commit=1)
    with caplog.at_level("WARNING"), pytest.raises(CallFailed) as failed:
        caller(session, user, commit_before_call=True).call(
            HAIKU, "ok", spy.made, lambda _: Usage(10, 5)
        )
    assert failed.value.kind == "commit_failed" and "call" not in spy.events  # nothing was sent
    assert "db down" not in caplog.text


def test_without_the_opt_in_nothing_is_committed_before_the_call(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = agency_with_user(session)
    spy = Spy(
        monkeypatch, fail_commit=1
    )  # the first commit is the usage row: that has its own rule
    with pytest.raises(CallFailed) as failed:
        caller(session, user).call(HAIKU, "ok", spy.made, lambda _: Usage(10, 5))
    assert failed.value.kind == "usage_lost" and spy.events[0] == "call"


def test_a_budget_refusal_is_still_recorded_and_committed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = agency_with_user(session)
    spy = Spy(monkeypatch)
    tight = Caller(
        session, user, NOW, default_budget_usd=Decimal("0.000001"), purpose=budget.PURPOSE_CHAT,
        estimate=pricing.estimate_chat_micro_usd, commit_before_call=True,
    )  # fmt: skip
    with pytest.raises(BudgetRefused):
        tight.call(HAIKU, "ok", spy.made, lambda _: Usage(1, 1))
    assert "call" not in spy.events and spy.events == ["commit"]
    assert [x.outcome for x in session.query(AiUsage)] == ["refused_budget"]


# --- the two callers ---------------------------------------------------------------------------


def test_the_chat_commits_before_the_model_is_called(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = agency_with_user(session)
    spy = Spy(monkeypatch)

    def script(request: Any) -> Any:
        spy.made()
        return fake_text("Bonjour.")

    fake = FakeChatClient(script)
    answer = agent.answer_with_ai(
        ChatContext(session, user, NOW),
        "une question libre",
        client=fake,
        settings=Settings(),
        history=list,
    )
    assert answer.outcome == r.UNKNOWN
    i = spy.events.index("call")
    assert spy.events[i - 1] == "commit" and spy.events[i + 1] == "commit"


def test_a_commit_that_fails_before_the_model_is_the_ai_down_answer_not_a_500(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = agency_with_user(session)
    state = {"armed": False, "checks": 0}
    real = Session.commit
    sent: list[str] = []

    def commit(db: Session) -> None:
        if state["armed"]:
            state["armed"] = False
            raise OperationalError("COMMIT", {}, Exception("db down"))
        real(db)

    def script(request: Any) -> Any:
        sent.append("sent")
        return fake_text("jamais")

    monkeypatch.setattr(Session, "commit", commit)
    original = Caller.affordable

    def arm_before_the_call(self: Caller, model: str) -> bool:
        ok = original(self, model)
        state["checks"] += 1
        # the gates check once, then the call checks again: the next commit is the one before it
        state["armed"] = state["checks"] == 2
        return ok

    monkeypatch.setattr(Caller, "affordable", arm_before_the_call)
    answer = agent.answer_with_ai(
        ChatContext(session, user, NOW),
        "une question libre",
        client=FakeChatClient(script),
        settings=Settings(),
        history=list,
    )
    assert sent == [] and answer.outcome == r.ERROR and answer.lines[0].text == agent.AI_DOWN


def test_the_screenshot_reader_keeps_its_behaviour_no_commit_before_the_call(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = agency_with_user(session)
    spy = Spy(monkeypatch)

    def reply(image: bytes, media_type: str, model: str) -> str:
        spy.events.append("call")
        return pnr("adult_and_child")

    read_screenshots(
        session, client=FakeTranscribeClient(reply), settings=Settings(), user=user, now=NOW,
        images=[png()],
    )  # fmt: skip
    assert spy.events == ["call", "commit"]
