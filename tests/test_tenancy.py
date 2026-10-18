"""Tenant binding: ``app.agency_id`` is set per transaction on Postgres and ignored on SQLite.

The Postgres tests need TEST_DATABASE_URL (they run in CI). The rest use a spy in place of the
database call, so the transaction logic is checked everywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.orm import Session

from app import tenancy
from app.db import make_engine, make_session_factory
from app.tenancy import LOOKUP_KEYS, bind_tenant, bound_tenant, lookup_scope

# --- statement shape (no database needed) -------------------------------------------


def test_set_config_statement_is_transaction_local_and_parameterised() -> None:
    stmt = tenancy.set_config_statement("app.agency_id", "7")
    compiled = stmt.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    assert str(compiled).startswith("SELECT set_config(")
    assert set(compiled.params.values()) == {"app.agency_id", "7", True}


def test_lookup_keys_are_exactly_the_two_documented_ones() -> None:
    assert frozenset({"app.login_phone", "app.session_token_hash"}) == LOOKUP_KEYS


# --- SQLite: no-ops -----------------------------------------------------------------


@pytest.fixture
def sqlite_session(tmp_path: Any) -> Iterator[Session]:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def test_sqlite_binding_runs_no_statements(sqlite_session: Session) -> None:
    seen: list[str] = []
    engine = sqlite_session.get_bind()
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: seen.append(stmt))
    bind_tenant(sqlite_session, 5)
    lookup_scope(sqlite_session, "app.login_phone", "+22212345678")
    sqlite_session.commit()
    assert seen == []
    assert bound_tenant(sqlite_session) == 5


def test_sqlite_queries_still_work_after_binding(sqlite_session: Session) -> None:
    bind_tenant(sqlite_session, 1)
    assert sqlite_session.scalar(select(func.count()).select_from(select(1).subquery())) == 1
    sqlite_session.commit()
    assert sqlite_session.scalar(select(1)) == 1


def test_unbound_session_has_no_tenant(sqlite_session: Session) -> None:
    assert bound_tenant(sqlite_session) is None


@pytest.mark.parametrize("bad", [0, -1, True, "1", None, 1.5])
def test_agency_id_must_be_a_positive_int(sqlite_session: Session, bad: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        bind_tenant(sqlite_session, bad)
    assert bound_tenant(sqlite_session) is None


@pytest.mark.parametrize(
    "key", ["app.agency_id", "app.other", "", "search_path", "APP.LOGIN_PHONE"]
)
def test_lookup_scope_rejects_unknown_keys_on_every_engine(
    sqlite_session: Session, key: str
) -> None:
    with pytest.raises(ValueError, match="lookup key"):
        lookup_scope(sqlite_session, key, "x")


def test_lookup_scope_rejects_non_string_values(sqlite_session: Session) -> None:
    with pytest.raises(TypeError):
        lookup_scope(sqlite_session, "app.login_phone", 123)  # type: ignore[arg-type]


# --- the transaction logic, with the database call spied on -------------------------


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    def record(connection: Connection, key: str, value: str) -> None:
        calls.append((key, value))

    monkeypatch.setattr(tenancy, "_is_postgres", lambda _bind: True)
    monkeypatch.setattr(tenancy, "_set_config", record)
    return calls


def test_bind_applies_immediately_and_again_in_every_new_transaction(
    sqlite_session: Session, spy: list[tuple[str, str]]
) -> None:
    bind_tenant(sqlite_session, 3)
    assert spy == [("app.agency_id", "3")]  # applied to the transaction bind_tenant opened
    sqlite_session.commit()
    spy.clear()
    sqlite_session.execute(select(1))  # a new transaction after commit
    assert spy == [("app.agency_id", "3")]
    sqlite_session.rollback()
    spy.clear()
    sqlite_session.execute(select(1))  # and after a rollback
    assert spy == [("app.agency_id", "3")]


def test_unbound_sessions_set_nothing(sqlite_session: Session, spy: list[tuple[str, str]]) -> None:
    sqlite_session.execute(select(1))
    sqlite_session.commit()
    sqlite_session.execute(select(1))
    assert spy == []


def test_rebinding_replaces_the_tenant(sqlite_session: Session, spy: list[tuple[str, str]]) -> None:
    bind_tenant(sqlite_session, 3)
    bind_tenant(sqlite_session, 4)
    sqlite_session.commit()
    spy.clear()
    sqlite_session.execute(select(1))
    assert spy == [("app.agency_id", "4")]


def test_lookup_scope_applies_once_and_is_not_reapplied(
    sqlite_session: Session, spy: list[tuple[str, str]]
) -> None:
    lookup_scope(sqlite_session, "app.session_token_hash", "ab" * 32)
    assert spy == [("app.session_token_hash", "ab" * 32)]
    sqlite_session.commit()
    sqlite_session.execute(select(1))
    assert len(spy) == 1


def test_two_sessions_do_not_share_a_tenant(spy: list[tuple[str, str]], tmp_path: Any) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'two.db'}")
    factory = make_session_factory(engine)
    with factory() as one, factory() as two:
        bind_tenant(one, 1)
        spy.clear()
        two.execute(select(1))
        assert spy == []
    engine.dispose()


# --- Postgres ------------------------------------------------------------------------


@pytest.fixture
def pg_session(db_url: str) -> Iterator[Session]:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    engine = make_engine(db_url)
    with make_session_factory(engine)() as s:
        yield s
    engine.dispose()


def current(session: Session, key: str) -> str | None:
    return session.scalar(select(func.current_setting(key, True)))


def test_pg_setting_is_visible_in_the_bound_transaction(pg_session: Session) -> None:
    bind_tenant(pg_session, 42)
    assert current(pg_session, "app.agency_id") == "42"


def test_pg_setting_is_reapplied_after_commit_and_rollback(pg_session: Session) -> None:
    bind_tenant(pg_session, 42)
    pg_session.commit()
    assert current(pg_session, "app.agency_id") == "42"
    pg_session.rollback()
    assert current(pg_session, "app.agency_id") == "42"


def test_pg_unbound_session_sees_no_tenant(pg_session: Session) -> None:
    assert current(pg_session, "app.agency_id") in (None, "")


def test_pg_returned_connection_starts_clean(db_url: str) -> None:
    if make_url(db_url).get_backend_name() != "postgresql":
        pytest.skip("needs a real Postgres server: set TEST_DATABASE_URL")
    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    with factory() as first:
        bind_tenant(first, 42)
        lookup_scope(first, "app.login_phone", "+22212345678")
        pid = first.scalar(select(func.pg_backend_pid()))
        assert current(first, "app.login_phone") == "+22212345678"
    with factory() as second:  # the pool hands the same connection back
        assert second.scalar(select(func.pg_backend_pid())) == pid
        assert current(second, "app.agency_id") in (None, "")
        assert current(second, "app.login_phone") in (None, "")
    engine.dispose()


def test_pg_lookup_scope_is_transaction_local(pg_session: Session) -> None:
    lookup_scope(pg_session, "app.session_token_hash", "ab" * 32)
    assert current(pg_session, "app.session_token_hash") == "ab" * 32
    pg_session.commit()
    assert current(pg_session, "app.session_token_hash") in (None, "")


def test_pg_lookup_scope_does_not_touch_the_tenant(pg_session: Session) -> None:
    bind_tenant(pg_session, 9)
    lookup_scope(pg_session, "app.login_phone", "+22212345678")
    assert current(pg_session, "app.agency_id") == "9"


def test_pg_lookup_scope_rejects_unknown_keys(pg_session: Session) -> None:
    with pytest.raises(ValueError, match="lookup key"):
        lookup_scope(pg_session, "app.agency_id", "1")
    assert current(pg_session, "app.agency_id") in (None, "")


def test_pg_value_with_quotes_is_data_not_sql(pg_session: Session) -> None:
    nasty = "x'; DROP TABLE agencies; --"
    lookup_scope(pg_session, "app.login_phone", nasty)
    assert current(pg_session, "app.login_phone") == nasty
