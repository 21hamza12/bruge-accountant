"""Fixed-window rate limiter kept in the database, so every instance shares one count.

One atomic ``INSERT .. ON CONFLICT (key, window_start) DO UPDATE SET count = count + 1
RETURNING count`` both counts the hit and returns the new total: two requests (or two servers)
can never read the same number and both slip under the limit. ``rate_limit_buckets`` has no
agency, so it has no row-level security; the application role holds SELECT, INSERT, UPDATE and
DELETE on it (``app.db_roles.GRANTS``).

The key is ``<limit name>:<sha256 of the subject>``: the table holds no phone number or address
in clear. The clock is passed in (never read here), and the caller commits: a login attempt must
count even when the rest of the request fails, so the login route commits right after ``hit``.

Known trade-off of any per-phone limit: someone who knows a phone number can use up its five
attempts and delay the real owner by up to 15 minutes. The per-IP limit is what bounds how many
phones one client can do this to.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.models import RateLimitBucket

MAX_WINDOW_SECONDS = 3600  # the longest window below; purge keeps one of them as a margin


@dataclass(frozen=True)
class Limit:
    name: str
    max_hits: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.max_hits <= 0 or self.window_seconds <= 0:
            raise ValueError("a limit needs a positive number of hits and a positive window")


LOGIN_PHONE = Limit("login_phone", 5, 15 * 60)
LOGIN_IP = Limit("login_ip", 20, 15 * 60)
# Defined now, used by the upload route (PR 4).
UPLOAD_AGENCY = Limit("upload_agency", 30, 3600)
# Guesses at the current password of a logged-in account (stolen-session brute force).
PASSWORD_CHANGE_USER = Limit("password_change_user", 5, 15 * 60)


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    count: int
    retry_after: int  # seconds until the window ends


def _check_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("the rate limiter needs a timezone-aware clock")


def window_start(now: datetime, window_seconds: int) -> datetime:
    _check_aware(now)
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, UTC)


def bucket_key(limit: Limit, subject: str) -> str:
    digest = hashlib.sha256(subject.encode("utf-8", "surrogatepass")).hexdigest()
    return f"{limit.name}:{digest}"


def _upsert(dialect_name: str, key: str, start: datetime) -> Any:
    if dialect_name == "postgresql":
        stmt: Any = postgresql.insert(RateLimitBucket)
    elif dialect_name == "sqlite":
        stmt = sqlite.insert(RateLimitBucket)
    else:
        raise NotImplementedError(f"rate limiting is not implemented for {dialect_name!r}")
    stmt = stmt.values(key=key, window_start=start, count=1)
    return stmt.on_conflict_do_update(
        index_elements=["key", "window_start"],
        set_={"count": RateLimitBucket.__table__.c.count + 1},
    ).returning(RateLimitBucket.__table__.c.count)


def hit(session: Session, limit: Limit, subject: str, now: datetime) -> RateDecision:
    """Count one attempt for ``subject`` and say whether it is within the limit."""
    start = window_start(now, limit.window_seconds)
    statement = _upsert(session.get_bind().dialect.name, bucket_key(limit, subject), start)
    count = int(session.execute(statement).scalar_one())
    remaining = (start + timedelta(seconds=limit.window_seconds) - now).total_seconds()
    return RateDecision(
        allowed=count <= limit.max_hits, count=count, retry_after=max(1, int(remaining))
    )


def purge_expired_windows(session: Session, now: datetime) -> int:
    """Remove windows that ended more than ``MAX_WINDOW_SECONDS`` ago; returns how many."""
    _check_aware(now)
    cutoff = now - timedelta(seconds=MAX_WINDOW_SECONDS)
    result = session.execute(delete(RateLimitBucket).where(RateLimitBucket.window_start < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)
