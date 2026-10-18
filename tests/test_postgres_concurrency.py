"""Two connections importing overlapping ticket sets at the same moment, on a real server.

SQLite serialises writers, so this only means something on Postgres and is skipped elsewhere.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import repository
from app.db import Base, make_engine, make_session_factory
from app.models import PRICE_STATUS_AWAITING, Ticket, Upload
from app.parsing.amadeus import ParsedReport, parse_report
from app.repository import create_agency, import_report
from tests.builders import build_workbook, ticket_row
from tests.conftest import SHA, allow_future_price_statuses

ROUNDS = 5
JOIN_TIMEOUT_S = 60


def _report(numbers: list[int]) -> ParsedReport:
    return parse_report(build_workbook([ticket_row(doc=f"{3940000000 + n}") for n in numbers]))


@pytest.fixture
def pg_url(db_url: str) -> str:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    return db_url


def test_overlapping_concurrent_imports(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Both sets span several upsert chunks and share 150 tickets; B is listed in reverse,
    # so only the repository's sort keeps the two writers locking rows in the same order.
    monkeypatch.setattr(repository, "UPSERT_CHUNK", 100)
    set_a = list(range(0, 300))
    set_b = list(reversed(range(150, 450)))
    union = set(set_a) | set(set_b)
    priced = {n for n in union if n % 10 == 0}

    setup = make_engine(pg_url)
    Base.metadata.create_all(setup)
    with make_session_factory(setup)() as s:
        agency_id = create_agency(s, "a", "A").id
        import_report(s, agency_id, _report(sorted(priced)), "seed.xlsx", SHA)
        allow_future_price_statuses(s)
        s.execute(update(Ticket).values(price_status="priced"))
        s.commit()

    reports = (_report(set_a), _report(set_b))
    start_together = threading.Barrier(2)
    real_upsert = repository._upsert_chunk
    first_chunk_done = threading.local()

    def synchronised_upsert(sess: Session, rows: list[dict[str, Any]]) -> None:
        # Both transactions already hold their upload row; release them into the
        # conflicting upserts at the same instant.
        if not getattr(first_chunk_done, "value", False):
            first_chunk_done.value = True
            start_together.wait(timeout=JOIN_TIMEOUT_S)
        real_upsert(sess, rows)

    monkeypatch.setattr(repository, "_upsert_chunk", synchronised_upsert)

    def worker(parsed: ParsedReport, errors: list[BaseException]) -> None:
        first_chunk_done.value = False
        engine = make_engine(pg_url)
        try:
            with make_session_factory(engine)() as sess:
                import_report(sess, agency_id, parsed, "race.xlsx", SHA)
        except BaseException as exc:  # a deadlock surfaces here as an error
            errors.append(exc)
        finally:
            engine.dispose()

    for round_no in range(ROUNDS):
        errors: list[BaseException] = []
        threads = [threading.Thread(target=worker, args=(r, errors)) for r in reports]
        for t in threads:
            t.start()
        for t in threads:
            t.join(JOIN_TIMEOUT_S)
        assert not any(t.is_alive() for t in threads), f"round {round_no}: import hung"
        assert errors == [], f"round {round_no}: {errors!r}"
        start_together.reset()

    with make_session_factory(setup)() as s:
        assert s.scalar(select(func.count()).select_from(Ticket)) == len(union)
        duplicates = s.execute(
            select(Ticket.agency_id, Ticket.ticket_no)
            .group_by(Ticket.agency_id, Ticket.ticket_no)
            .having(func.count() > 1)
        ).all()
        assert duplicates == []
        assert s.scalar(select(func.count()).select_from(Upload)) == 1 + 2 * ROUNDS
        status = dict(s.execute(select(Ticket.ticket_no, Ticket.price_status)).tuples().all())
        expected = {
            f"{3940000000 + n}": "priced" if n in priced else PRICE_STATUS_AWAITING for n in union
        }
        assert status == expected
        # Nothing left waiting on a lock once both imports have returned.
        waiting = s.scalar(text("SELECT count(*) FROM pg_locks WHERE NOT granted"))
        assert waiting == 0
    setup.dispose()
