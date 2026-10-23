"""The AI budget (app/ai/budget.py), the ``ai-budget`` command, and the client the app builds."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.ai import budget, pricing
from app.ai.client import AnthropicTranscriber
from app.ai.fake import FakeTranscribeClient
from app.cli import EXIT_USAGE, main
from app.db import make_engine, make_session_factory
from app.models import AgencyAiSettings, AiUsage, AuditEvent, User
from app.repository import create_agency
from app.settings import Settings
from app.tenancy import rebind_tenant
from app.web.main import create_app

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
HAIKU = "claude-haiku-5-5"
FIVE = Decimal(5)


def agency_with_user(s: Session, slug: str = "agence-a", phone: str = "+22212345601") -> User:
    agency = create_agency(s, slug, slug.upper())
    rebind_tenant(s, agency.id)
    user = User(agency_id=agency.id, phone=phone, role="staff")
    s.add(user)
    s.commit()
    return user


def log_cost(s: Session, user: User, micro: int, when: datetime = NOW) -> None:
    s.add(
        AiUsage(
            agency_id=user.agency_id,
            user_id=user.id,
            created_at=when,
            purpose="screenshot",
            model=HAIKU,
            input_tokens=1,
            output_tokens=1,
            cost_micro_usd=micro,
            outcome="ok",
        )
    )
    s.commit()


def test_month_start_is_the_first_instant_of_the_utc_month() -> None:
    assert budget.month_start(NOW) == datetime(2026, 10, 1, tzinfo=UTC)
    assert budget.month_start(datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)) == datetime(
        2026, 12, 1, tzinfo=UTC
    )
    assert budget.month_start(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)) == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_no_row_means_on_with_the_default_budget(session: Session) -> None:
    user = agency_with_user(session)
    limits = budget.get_limits(session, user.agency_id, FIVE)
    assert limits == budget.AgencyLimits(True, 5_000_000)
    assert budget.agency_enabled(session, user.agency_id) is True


def test_record_computes_the_cost_from_the_tokens(session: Session) -> None:
    user = agency_with_user(session)
    cost = budget.record(
        session, user, now=NOW, model=HAIKU, input_tokens=650, output_tokens=400, outcome="ok"
    )
    session.commit()
    assert cost == 265
    row = session.scalars(select(AiUsage)).one()
    assert (row.cost_micro_usd, row.created_at, row.purpose) == (265, NOW, "screenshot")


def test_month_to_date_counts_this_month_and_this_agency_only(session: Session) -> None:
    a = agency_with_user(session)
    b = agency_with_user(session, "agence-b", "+22212345602")
    rebind_tenant(session, a.agency_id)
    log_cost(session, a, 100)
    log_cost(session, a, 50, datetime(2026, 10, 1, 0, 0, tzinfo=UTC))  # the first instant: counts
    log_cost(session, a, 9999, datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC))  # last month
    log_cost(session, a, 7777, datetime(2026, 11, 1, 0, 0, tzinfo=UTC))  # not yet: not this month
    rebind_tenant(session, b.agency_id)
    log_cost(session, b, 1_000_000)
    rebind_tenant(session, a.agency_id)
    assert budget.month_to_date_micro(session, a.agency_id, NOW) == 150
    assert (
        budget.month_to_date_micro(session, a.agency_id, datetime(2026, 9, 15, tzinfo=UTC)) == 9999
    )
    assert (
        budget.month_to_date_micro(session, a.agency_id, datetime(2026, 11, 2, tzinfo=UTC)) == 7777
    )


def test_the_next_month_wraps_the_year() -> None:
    assert budget.next_month_start(datetime(2026, 12, 5, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )
    assert budget.next_month_start(NOW) == datetime(2026, 11, 1, tzinfo=UTC)


def test_within_budget_boundaries(session: Session) -> None:
    user = agency_with_user(session)
    estimate = pricing.estimate_micro_usd(HAIKU)
    budget.set_limits(
        session,
        user.agency_id,
        budget_usd=Decimal(estimate) / 1_000_000,
        enabled=None,
        default_budget_usd=FIVE,
        now=NOW,
    )
    session.commit()
    # exactly the estimate: it fits ("stay under or at"); one micro-USD already spent: it does not
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE) is True
    log_cost(session, user, 1)
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE) is False


def test_a_disabled_agency_is_never_within_budget(session: Session) -> None:
    user = agency_with_user(session)
    budget.set_limits(
        session, user.agency_id, budget_usd=None, enabled=False, default_budget_usd=FIVE, now=NOW
    )
    session.commit()
    assert budget.agency_enabled(session, user.agency_id) is False
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE) is False


def test_a_zero_budget_refuses_everything(session: Session) -> None:
    user = agency_with_user(session)
    budget.set_limits(
        session,
        user.agency_id,
        budget_usd=Decimal(0),
        enabled=None,
        default_budget_usd=FIVE,
        now=NOW,
    )
    session.commit()
    assert budget.within_budget(session, user.agency_id, NOW, HAIKU, FIVE) is False


def test_set_limits_keeps_what_it_is_not_given(session: Session) -> None:
    user = agency_with_user(session)
    budget.set_limits(
        session,
        user.agency_id,
        budget_usd=Decimal("12.5"),
        enabled=None,
        default_budget_usd=FIVE,
        now=NOW,
    )
    budget.set_limits(
        session, user.agency_id, budget_usd=None, enabled=False, default_budget_usd=FIVE, now=NOW
    )
    session.commit()
    row = session.get(AgencyAiSettings, user.agency_id)
    assert row is not None and (row.enabled, row.monthly_budget_micro_usd) == (False, 12_500_000)


# --- the command -------------------------------------------------------------------------------


@pytest.fixture
def ready(db_url: str) -> str:
    assert main(["init-db", "--db", db_url]) == 0
    assert main(["add-agency", "bruge", "Bruge", "--db", db_url]) == 0
    return db_url


def settings_of(url: str, slug: str = "bruge") -> tuple[AgencyAiSettings | None, int]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            from app.repository import get_agency_by_slug

            agency = get_agency_by_slug(s, slug)
            assert agency is not None
            rebind_tenant(s, agency.id)
            row = s.get(AgencyAiSettings, agency.id)
            if row is not None:
                s.expunge(row)
            return row, agency.id
    finally:
        engine.dispose()


def test_the_command_sets_the_budget(ready: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ai-budget", "bruge", "--usd", "12.50", "--db", ready]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "agency": "bruge",
        "enabled": True,
        "monthly_budget_usd": "12.5",
        "spent_this_month_usd": "0",
    }
    row, _ = settings_of(ready)
    assert row is not None and (row.enabled, row.monthly_budget_micro_usd) == (True, 12_500_000)


def test_the_command_disables_and_enables(ready: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ai-budget", "bruge", "--disable", "--db", ready]) == 0
    row, _ = settings_of(ready)
    assert row is not None and row.enabled is False
    assert row.monthly_budget_micro_usd == 5_000_000  # the default, kept
    assert main(["ai-budget", "bruge", "--usd", "3", "--disable", "--db", ready]) == 0
    row, _ = settings_of(ready)
    assert row is not None and (row.enabled, row.monthly_budget_micro_usd) == (False, 3_000_000)
    assert main(["ai-budget", "bruge", "--enable", "--db", ready]) == 0
    row, _ = settings_of(ready)
    assert row is not None and row.enabled is True
    capsys.readouterr()


def test_the_command_audits_the_change(ready: str) -> None:
    assert main(["ai-budget", "bruge", "--usd", "7", "--db", ready]) == 0
    _, agency_id = settings_of(ready)
    engine = make_engine(ready)
    try:
        with make_session_factory(engine)() as s:
            rebind_tenant(s, agency_id)
            events = list(
                s.scalars(select(AuditEvent).where(AuditEvent.event_type == audit.AI_BUDGET_SET))
            )
    finally:
        engine.dispose()
    assert len(events) == 1
    assert events[0].details["budget_micro_usd"] == 7_000_000 and events[0].details["enabled"]


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["bruge"], id="nothing to do"),
        pytest.param(["nobody", "--usd", "5"], id="unknown agency"),
        pytest.param(["bruge", "--usd", "-1"], id="negative"),
        pytest.param(["bruge", "--usd", "abc"], id="not a number"),
        pytest.param(["bruge", "--usd", "NaN"], id="nan"),
        pytest.param(["bruge", "--usd", "100001"], id="too large"),
        pytest.param(["bruge", "--disable", "--enable"], id="both switches"),
    ],
)
def test_the_command_refuses_bad_input(ready: str, args: list[str]) -> None:
    try:
        code = main(["ai-budget", *args, "--db", ready])
    except SystemExit as exc:  # argparse refuses the command line itself
        code = int(exc.code) if isinstance(exc.code, int) else -1
    assert code == EXIT_USAGE
    row, _ = settings_of(ready)
    assert row is None  # nothing was written


# --- the app builds its client from the settings -------------------------------------------------


def app_for(db_url: str, **kw: Any) -> Any:
    return create_app(Settings(database_url=SecretStr(db_url), **kw))


def test_no_key_no_client(db_url: str) -> None:
    assert app_for(db_url).state.ai_client is None


def test_a_key_builds_the_real_client_without_calling_anything(db_url: str) -> None:
    app = app_for(db_url, anthropic_api_key="sk-test-not-real")
    assert isinstance(app.state.ai_client, AnthropicTranscriber)


def test_ai_enabled_false_wins_over_a_key(db_url: str) -> None:
    app = app_for(db_url, anthropic_api_key="sk-test-not-real", ai_enabled=False)
    assert app.state.ai_client is None


def test_an_injected_client_is_used(db_url: str) -> None:
    fake = FakeTranscribeClient()
    app = create_app(Settings(database_url=SecretStr(db_url)), ai_client=fake)
    assert app.state.ai_client is fake


def test_the_environment_variables_are_read(monkeypatch: pytest.MonkeyPatch, db_url: str) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-not-real")
    monkeypatch.setenv("AI_MONTHLY_BUDGET_USD", "12")
    monkeypatch.setenv("AI_SCREENSHOT_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("AI_FALLBACK_MODEL", "claude-sonnet-5-5")
    s = Settings()
    assert s.ai_active and s.ai_monthly_budget_usd == 12
    assert "sk-env-not-real" not in repr(s)


def test_the_global_multipart_spool_is_left_at_the_library_default(db_url: str) -> None:
    """Only the screenshot route keeps uploads in memory (a parser of its own, after login); an
    anonymous multipart post (the login form) is spooled to disk after 1 MB as usual."""
    from starlette.formparsers import MultiPartParser

    app_for(db_url)
    assert MultiPartParser.spool_max_size == 1024 * 1024
