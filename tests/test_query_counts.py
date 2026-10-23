"""The home and questions pages send the same number of SQL statements whatever the data's shape.

A page that asks the database once per staff sign (or per booking) is fine with two signs and slow
with twenty; this pins that the number of statements does not grow with the number of signs, for a
manager and for a staff member who owns all of them. Every statement the engine executes is
counted (the tenant setting included), after one warm-up request.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from tests.builders import build_workbook, ticket_row
from tests.page_support import DAY, Env
from tests.web_support import WebDb

FEW, MANY = 2, 20
CEILING = 12  # today 7 to 9 per page (session, user, agency, signs, days, counts, tickets, ...)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


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


def statements(env: Env, client: TestClient, path: str) -> list[str]:
    engine: Engine = env.factory.app.state.engine  # type: ignore[attr-defined]
    assert client.get(path).status_code == 200  # warm-up: caches, first-use statements
    with counting(engine) as seen:
        response = client.get(path)
    assert response.status_code == 200
    return list(seen)


def more_signs(env: Env, first: int, last: int) -> list[str]:
    """Tickets of the signs ``first`` to ``last - 1``, same day (3 tickets, 2 bookings each)."""
    signs = [f"{n:04d}XX" for n in range(first, last)]
    rows = []
    for n, sign in enumerate(signs):
        base = 3_950_000_000 + n * 10
        rows += [
            ticket_row(doc=str(base + 1), staff=sign, rloc=f"R{n:04d}A", name=f"ONE/{n}"),
            ticket_row(doc=str(base + 2), staff=sign, rloc=f"R{n:04d}A", name=f"TWO/{n}"),
            ticket_row(doc=str(base + 3), staff=sign, rloc=f"R{n:04d}B", name=f"THREE/{n}"),
        ]
    env.load(data=build_workbook(rows))
    return signs


PAGES = [
    "/",
    f"/?date={DAY}",
    "/questions",
    f"/questions?date={DAY}&status=all&limit=1000",
    "/chat",
]


def check(env: Env, client: TestClient, grow: Callable[[], None]) -> None:
    for path in PAGES:
        few = statements(env, client, path)
        assert 3 <= len(few) <= CEILING, (path, len(few), few)
    before = {path: len(statements(env, client, path)) for path in PAGES}
    grow()
    for path in PAGES:
        many = statements(env, client, path)
        assert len(many) == before[path], (path, before[path], len(many))


def test_the_manager_pages_send_a_constant_number_of_statements(env: Env) -> None:
    env.load()  # two signs
    with env.manager_a() as c:

        def grow() -> None:
            more_signs(env, 1, MANY - FEW + 1)

        check(env, c, grow)
        html = c.get("/questions?limit=1000").text
    assert html.count('class="sign"') == MANY


def test_the_staff_pages_send_a_constant_number_of_statements(env: Env) -> None:
    env.load()
    env.link(env.seeded.staff_a, "0004SV")
    env.link(env.seeded.staff_a, "0009ER")
    with env.staff_a() as c:

        def grow() -> None:
            for sign in more_signs(env, 1, MANY - FEW + 1):
                env.link(env.seeded.staff_a, sign)

        check(env, c, grow)
        html = c.get("/questions?limit=1000").text
    assert html.count('class="sign"') == MANY
