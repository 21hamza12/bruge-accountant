"""The fixtures of the chat tests, loaded with ``pytest_plugins = ["tests.chat_fixtures"]``: one
database with the whole world, built once for a test module and copied for each test."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from tests.chat_support import ChatWorld


@pytest.fixture(scope="module")
def chat_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One SQLite file with the whole world in it, built once for the test module."""
    path = tmp_path_factory.mktemp("chat") / "world.db"
    engine = make_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as session:
        ChatWorld(session)
    engine.dispose()
    return path


@pytest.fixture
def w(chat_template: Path, tmp_path: Path) -> Iterator[ChatWorld]:
    """A private copy of that database, so a test may change it freely."""
    copy = tmp_path / "world.db"
    shutil.copy(chat_template, copy)
    engine = make_engine(f"sqlite:///{copy}")
    with make_session_factory(engine)() as session:
        yield ChatWorld.attach(session)
    engine.dispose()
