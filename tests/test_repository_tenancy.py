"""The repository records upload counts and binds the tenant before it touches tenant data.

Cross-engine tests need no RLS; the ones that prove the binding really matters run on Postgres
in both deployment modes (``rls_db``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import repository
from app.auth.passwords import hash_password
from app.db import make_engine, make_session_factory
from app.models import Agency, Ticket, Upload, User
from app.parsing.amadeus import ParsedReport
from app.repository import (
    bootstrap_agency,
    create_agency,
    get_agency_by_slug,
    import_report,
    tickets_for_day,
)
from app.tenancy import TenantBindingError, bind_tenant, bound_tenant, rebind_tenant
from tests.conftest import SHA, RlsDatabase

# --- upload counts and uploader (both engines) ----------------------------------------


def test_upload_records_inserted_and_updated_counts(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    agency = create_agency(session, "a", "A")
    first = import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    second = import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    rows = {u.id: u for u in session.scalars(select(Upload))}
    assert (rows[first.upload_id].inserted_count, rows[first.upload_id].updated_count) == (48, 0)
    assert (rows[second.upload_id].inserted_count, rows[second.upload_id].updated_count) == (0, 48)
    assert rows[first.upload_id].ticket_count == 48


def test_uploaded_by_user_is_recorded(session: Session, parsed_fixture: ParsedReport) -> None:
    agency = create_agency(session, "a", "A")
    user = User(agency_id=agency.id, phone="+22212345678", role="staff")
    session.add(user)
    session.commit()
    done = import_report(
        session, agency.id, parsed_fixture, "f.xlsx", SHA, uploaded_by_user_id=user.id
    )
    assert session.get(Upload, done.upload_id).uploaded_by_user_id == user.id  # type: ignore[union-attr]
    anonymous = import_report(session, agency.id, parsed_fixture, "g.xlsx", SHA)
    assert session.get(Upload, anonymous.upload_id).uploaded_by_user_id is None  # type: ignore[union-attr]


def test_uploader_from_another_agency_is_refused_and_nothing_is_written(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    a, b = create_agency(session, "a", "A"), create_agency(session, "b", "B")
    stranger = User(agency_id=b.id, phone="+22212345678", role="staff")
    session.add(stranger)
    session.commit()
    rebind_tenant(session, a.id)  # create_agency left the session bound to b
    with pytest.raises(IntegrityError):
        import_report(session, a.id, parsed_fixture, "f.xlsx", SHA, uploaded_by_user_id=stranger.id)
    assert session.scalar(select(func.count()).select_from(Upload)) == 0
    assert session.scalar(select(func.count()).select_from(Ticket)) == 0


# --- the tenant is bound before tenant data is touched --------------------------------


def test_repository_functions_bind_the_tenant(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    agency = create_agency(session, "a", "A")
    assert bound_tenant(session) == agency.id  # the new agency's own id, bound to insert it
    other = Session(bind=session.get_bind())
    try:
        assert bound_tenant(other) is None
        import_report(other, agency.id, parsed_fixture, "f.xlsx", SHA)
        assert bound_tenant(other) == agency.id
    finally:
        other.close()
    third = Session(bind=session.get_bind())
    try:
        tickets_for_day(third, agency.id, parsed_fixture.header.report_date)
        assert bound_tenant(third) == agency.id
    finally:
        third.close()


def test_slug_lookup_works_on_an_unbound_session(session: Session) -> None:
    create_agency(session, "a", "A")
    fresh = Session(bind=session.get_bind())
    try:
        found = get_agency_by_slug(fresh, "a")
        assert found is not None and found.slug == "a"
        assert bound_tenant(fresh) is None  # looking an agency up does not bind to it
    finally:
        fresh.close()


# --- no silent re-pointing ---------------------------------------------------------------


def test_tickets_for_day_refuses_another_agency_and_keeps_the_binding(
    session: Session,
) -> None:
    bind_tenant(session, 1)
    with pytest.raises(TenantBindingError):
        tickets_for_day(session, 2, date(2026, 10, 1))
    assert bound_tenant(session) == 1


def test_import_report_refuses_another_agency_keeps_the_binding_and_writes_nothing(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    bind_tenant(session, 1)
    with pytest.raises(TenantBindingError):
        import_report(session, 2, parsed_fixture, "f.xlsx", SHA)
    assert bound_tenant(session) == 1
    assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_the_functions_accept_the_agency_the_session_is_bound_to(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    agency = create_agency(session, "a", "A")
    bind_tenant(session, agency.id)  # same agency again: fine
    import_report(session, agency.id, parsed_fixture, "f.xlsx", SHA)
    found = tickets_for_day(session, agency.id, parsed_fixture.header.report_date)
    assert len(found) == 48


# --- a failed create restores the previous binding, whatever else goes wrong ---------------

NEW_AGENCY_ID = 99


@pytest.fixture
def fake_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make SQLite behave like Postgres here: the new id is known (and bound) before the INSERT."""
    monkeypatch.setattr(repository, "_next_agency_id", lambda _session: NEW_AGENCY_ID)


def _duplicate_slug_create(session: Session) -> Any:
    return create_agency(session, "taken", "Again")


def _bad_phone_bootstrap(session: Session) -> Any:
    return bootstrap_agency(
        session, "fresh", "Fresh", "not-a-phone", "Name", hash_password("x" * 12)
    )


def _duplicate_slug_bootstrap(session: Session) -> Any:
    return bootstrap_agency(session, "taken", "Again", "+22212345678", "N", hash_password("x" * 12))


FAILING_CALLS = [
    pytest.param(_duplicate_slug_create, IntegrityError, id="create_agency duplicate slug"),
    pytest.param(_bad_phone_bootstrap, IntegrityError, id="bootstrap_agency bad phone"),
    pytest.param(
        _duplicate_slug_bootstrap, repository.AlreadyExistsError, id="bootstrap duplicate slug"
    ),
]


@pytest.fixture
def taken_slug(session: Session) -> Session:
    """A session on a database that already has the agency 'taken' (created by someone else)."""
    other = Session(bind=session.get_bind())
    try:
        create_agency(other, "taken", "Taken")
    finally:
        other.close()
    return session


@pytest.mark.parametrize(("call", "error"), FAILING_CALLS)
@pytest.mark.usefixtures("fake_sequence")
def test_a_failure_restores_an_unbound_session(
    taken_slug: Session, call: Callable[[Session], Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        call(taken_slug)
    assert bound_tenant(taken_slug) is None


@pytest.mark.parametrize(("call", "error"), FAILING_CALLS)
@pytest.mark.usefixtures("fake_sequence")
def test_a_failure_restores_the_previous_binding(
    taken_slug: Session, call: Callable[[Session], Any], error: type[Exception]
) -> None:
    bind_tenant(taken_slug, 7)
    with pytest.raises(error):
        call(taken_slug)
    assert bound_tenant(taken_slug) == 7


@pytest.mark.parametrize(("call", "error"), FAILING_CALLS[:2])
@pytest.mark.parametrize("previous", [None, 7])
@pytest.mark.usefixtures("fake_sequence")
def test_a_failing_rollback_never_masks_the_original_error(
    taken_slug: Session,
    monkeypatch: pytest.MonkeyPatch,
    call: Callable[[Session], Any],
    error: type[Exception],
    previous: int | None,
) -> None:
    if previous is not None:
        bind_tenant(taken_slug, previous)

    def broken_rollback() -> None:
        raise RuntimeError("connection lost during rollback")

    monkeypatch.setattr(taken_slug, "rollback", broken_rollback)
    with pytest.raises(error):  # the IntegrityError, not the RuntimeError
        call(taken_slug)
    assert bound_tenant(taken_slug) == previous


# --- Postgres, under row-level security, in both modes --------------------------------


def test_create_agency_works_under_forced_rls(rls_db: RlsDatabase) -> None:
    engine = make_engine(rls_db.owner_url)  # only the owner may insert into agencies
    try:
        with make_session_factory(engine)() as s:
            first = create_agency(s, "first", "First")
            second = create_agency(s, "second", "Second")
            assert first.id != second.id
            assert bound_tenant(s) == second.id
            with pytest.raises(IntegrityError):
                create_agency(s, "first", "Again")
            # the session is usable afterwards, and only the bound agency is visible
            assert s.scalars(select(Agency.slug)).all() == ["second"]
            found = get_agency_by_slug(s, "first")
            assert found is not None and found.id == first.id
    finally:
        engine.dispose()


def test_import_survives_its_commit_under_rls_and_is_isolated(
    rls_db: RlsDatabase, parsed_fixture: ParsedReport
) -> None:
    owner = make_engine(rls_db.owner_url)
    try:
        with make_session_factory(owner)() as s:
            a = create_agency(s, "a", "A").id
            b = create_agency(s, "b", "B").id
    finally:
        owner.dispose()

    app = make_engine(rls_db.app_url)
    try:
        with make_session_factory(app)() as s:
            first = import_report(s, a, parsed_fixture, "1OCT.xlsx", SHA)
            # import_report committed; the next transaction is the proof the listener re-bound it
            assert s.scalar(select(func.count()).select_from(Ticket)) == 48
            assert (first.inserted, first.updated) == (48, 0)
            again = import_report(s, a, parsed_fixture, "1OCT.xlsx", SHA)
            assert (again.inserted, again.updated) == (0, 48)
            assert len(tickets_for_day(s, a, parsed_fixture.header.report_date)) == 48

            # the same tickets in agency B are new rows there: B's lookups cannot see A's
            rebind_tenant(s, b)  # a deliberate switch; import_report itself refuses to re-point
            other = import_report(s, b, parsed_fixture, "1OCT.xlsx", SHA)
            assert (other.inserted, other.updated) == (48, 0)
            assert bound_tenant(s) == b
            assert set(s.scalars(select(Ticket.agency_id))) == {b}
            assert s.scalar(select(func.count()).select_from(Upload)) == 1

        with make_session_factory(app)() as unbound:
            assert unbound.scalar(select(func.count()).select_from(Ticket)) == 0
            assert unbound.scalar(select(func.count()).select_from(Upload)) == 0
            bind_tenant(unbound, a)
            assert unbound.scalar(select(func.count()).select_from(Ticket)) == 48
            assert unbound.scalar(select(func.count()).select_from(Upload)) == 2
    finally:
        app.dispose()


def test_import_for_a_missing_agency_fails_cleanly_under_rls(
    rls_db: RlsDatabase, parsed_fixture: ParsedReport
) -> None:
    engine = make_engine(rls_db.app_url)
    try:
        with make_session_factory(engine)() as s, pytest.raises(LookupError):
            import_report(s, 424242, parsed_fixture, "f.xlsx", SHA)
    finally:
        engine.dispose()
