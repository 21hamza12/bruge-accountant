"""Step 16: the Postgres-backed fixed-window rate limiter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.auth import ratelimit
from app.auth.ratelimit import LOGIN_IP, LOGIN_PHONE, UPLOAD_AGENCY, Limit
from app.db import Base, make_engine, make_session_factory
from app.models import RateLimitBucket

T0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def limiter_url(app_db_url: str) -> str:
    """A database with the schema, reachable as the application role."""
    if app_db_url.startswith("sqlite"):
        engine = make_engine(app_db_url)
        Base.metadata.create_all(engine)
        engine.dispose()
    return app_db_url


def test_the_documented_limits() -> None:
    assert (LOGIN_PHONE.max_hits, LOGIN_PHONE.window_seconds) == (5, 15 * 60)
    assert (LOGIN_IP.max_hits, LOGIN_IP.window_seconds) == (100, 15 * 60)  # a coarse flood guard
    assert (UPLOAD_AGENCY.max_hits, UPLOAD_AGENCY.window_seconds) == (30, 3600)


def test_allows_up_to_the_limit_then_blocks(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        results = [ratelimit.hit(s, LOGIN_PHONE, "+22212345678", T0) for _ in range(6)]
        s.commit()
    engine.dispose()
    assert [r.allowed for r in results] == [True] * 5 + [False]
    assert [r.count for r in results] == [1, 2, 3, 4, 5, 6]
    assert results[-1].retry_after == 15 * 60  # window starts at an exact multiple of 15 min


def test_retry_after_counts_down_inside_the_window(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        for _ in range(5):
            ratelimit.hit(s, LOGIN_PHONE, "k", T0 + timedelta(minutes=4))
        blocked = ratelimit.hit(s, LOGIN_PHONE, "k", T0 + timedelta(minutes=4))
        s.commit()
    engine.dispose()
    assert not blocked.allowed
    assert blocked.retry_after == 11 * 60


def test_a_new_window_starts_fresh(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        for _ in range(6):
            ratelimit.hit(s, LOGIN_PHONE, "k", T0)
        later = ratelimit.hit(s, LOGIN_PHONE, "k", T0 + timedelta(minutes=15))
        s.commit()
    engine.dispose()
    assert later.allowed
    assert later.count == 1


def test_subjects_and_limits_do_not_share_counters(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        for _ in range(6):
            ratelimit.hit(s, LOGIN_PHONE, "a", T0)
        other_subject = ratelimit.hit(s, LOGIN_PHONE, "b", T0)
        other_limit = ratelimit.hit(s, LOGIN_IP, "a", T0)
        s.commit()
    engine.dispose()
    assert other_subject.allowed
    assert other_limit.allowed


def test_first_rejection_is_flagged_once_per_window(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        results = [ratelimit.hit(s, LOGIN_PHONE, "k", T0) for _ in range(8)]
        s.commit()
    engine.dispose()
    assert [r.first_rejection for r in results] == [False] * 5 + [True] + [False] * 2


def test_refund_takes_one_hit_back_and_never_goes_below_zero(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        ratelimit.hit(s, LOGIN_IP, "k", T0)
        ratelimit.hit(s, LOGIN_IP, "k", T0)
        ratelimit.refund(s, LOGIN_IP, "k", T0)
        assert ratelimit.hit(s, LOGIN_IP, "k", T0).count == 2
        ratelimit.refund(s, LOGIN_IP, "k", T0)
        ratelimit.refund(s, LOGIN_IP, "k", T0)
        ratelimit.refund(s, LOGIN_IP, "k", T0)  # already empty: nothing to give back
        ratelimit.refund(s, LOGIN_IP, "unseen", T0)  # no bucket at all: a no-op
        assert ratelimit.hit(s, LOGIN_IP, "k", T0).count == 1
        s.commit()
    engine.dispose()


def test_reset_forgets_the_subject_in_every_window_and_only_that_subject(
    limiter_url: str,
) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        ratelimit.hit(s, LOGIN_PHONE, "a", T0)
        ratelimit.hit(s, LOGIN_PHONE, "a", T0 + timedelta(minutes=16))
        ratelimit.hit(s, LOGIN_PHONE, "b", T0)
        ratelimit.reset(s, LOGIN_PHONE, "a")
        s.commit()
        assert s.scalar(select(func.count()).select_from(RateLimitBucket)) == 1
    engine.dispose()


def test_purge_deletes_in_chunks(limiter_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import event

    monkeypatch.setattr(ratelimit, "PURGE_CHUNK", 3)
    engine = make_engine(limiter_url)
    deletes: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _spy(_c: object, _cur: object, statement: str, *_a: object) -> None:
        if statement.lstrip().upper().startswith("DELETE"):
            deletes.append(statement)

    with make_session_factory(engine)() as s:
        for n in range(10):
            ratelimit.hit(s, LOGIN_PHONE, f"old-{n}", T0)
        ratelimit.hit(s, UPLOAD_AGENCY, "recent", T0 + timedelta(hours=5))
        s.commit()
        removed = ratelimit.purge_expired_windows(s, T0 + timedelta(hours=3))
        left = s.scalar(select(func.count()).select_from(RateLimitBucket))
    engine.dispose()
    assert removed == 10
    assert left == 1
    assert len(deletes) == 4  # 3 + 3 + 3 + 1


def test_two_separate_engines_share_one_limit(limiter_url: str) -> None:
    first, second = make_engine(limiter_url), make_engine(limiter_url)
    try:
        with make_session_factory(first)() as a, make_session_factory(second)() as b:
            for _ in range(3):
                ratelimit.hit(a, LOGIN_PHONE, "shared", T0)
                a.commit()
            for _ in range(2):
                ratelimit.hit(b, LOGIN_PHONE, "shared", T0)
                b.commit()
            from_a = ratelimit.hit(a, LOGIN_PHONE, "shared", T0)
            a.commit()
            from_b = ratelimit.hit(b, LOGIN_PHONE, "shared", T0)
            b.commit()
    finally:
        first.dispose()
        second.dispose()
    assert from_a.count == 6
    assert not from_a.allowed
    assert not from_b.allowed
    assert from_b.count == 7


def test_the_key_holds_no_readable_subject(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        ratelimit.hit(s, LOGIN_PHONE, "+22212345678", T0)
        s.commit()
        keys = list(s.scalars(select(RateLimitBucket.key)))
    engine.dispose()
    assert len(keys) == 1
    assert "22212345678" not in keys[0]
    assert keys[0].startswith("login_phone:")


def test_purge_removes_only_windows_that_are_over(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s:
        ratelimit.hit(s, LOGIN_PHONE, "old", T0)
        ratelimit.hit(s, UPLOAD_AGENCY, "recent", T0 + timedelta(hours=2))
        s.commit()
        removed = ratelimit.purge_expired_windows(s, T0 + timedelta(hours=2, minutes=10))
        s.commit()
        left = s.scalar(select(func.count()).select_from(RateLimitBucket))
    engine.dispose()
    assert removed == 1
    assert left == 1


def test_naive_clock_is_rejected(limiter_url: str) -> None:
    engine = make_engine(limiter_url)
    with make_session_factory(engine)() as s, pytest.raises(ValueError, match="timezone"):
        ratelimit.hit(s, LOGIN_PHONE, "k", datetime(2026, 1, 1, 0, 0, 0))
    engine.dispose()


def test_limit_validates_its_numbers() -> None:
    with pytest.raises(ValueError, match="positive"):
        Limit("x", 0, 60)
