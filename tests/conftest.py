from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.db import Base, make_engine, make_session_factory
from app.parsing.amadeus import ParsedReport, parse_report

FIXTURE = Path(__file__).parent / "fixtures" / "1OCT.xlsx"
SHA = "a" * 64


@pytest.fixture(scope="session")
def fixture_path() -> Path:
    return FIXTURE


@pytest.fixture(scope="session")
def parsed_fixture() -> ParsedReport:
    return parse_report(FIXTURE)


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()
