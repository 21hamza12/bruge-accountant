"""FastAPI dependencies shared by routes (and replaceable in tests via dependency_overrides)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from fastapi import Request
from sqlalchemy.orm import Session

from app.settings import Settings


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_session(request: Request) -> Iterator[Session]:
    """One session per request: rolled back if the request fails, always closed."""
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
