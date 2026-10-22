"""app/days.py: report days, their status changes and the events that record them (SQLite)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import days
from app.days import DayClosed, DayError
from app.models import (
    DAY_CLOSED,
    DAY_COMPARED,
    DAY_OPEN,
    AuditEvent,
    DayEvent,
    ReportDay,
    Upload,
    User,
)
from app.repository import create_agency

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 22)


class World:
    def __init__(self, session: Session) -> None:
        self.db = session
        self.agency_id = create_agency(session, "agence-d", "Agence D").id
        self.manager = self._user("+22212350001", "manager")
        self.accountant = self._user("+22212350002", "accountant")
        self.staff = self._user("+22212350003", "staff")
        other = create_agency(session, "agence-e", "Agence E").id
        self.other_agency_id = other
        self.other_manager = self._user("+22212350004", "manager", other)

    def _user(self, phone: str, role: str, agency_id: int | None = None) -> User:
        user = User(agency_id=agency_id or self.agency_id, phone=phone, role=role)
        self.db.add(user)
        self.db.commit()
        return user

    def open(self, day: date = TODAY, actor: User | None = None) -> ReportDay:
        return days.get_or_open_day(self.db, actor or self.staff, day, now=NOW)

    def events(self, report_day: ReportDay) -> list[DayEvent]:
        query = select(DayEvent).where(DayEvent.report_day_id == report_day.id)
        return list(self.db.scalars(query.order_by(DayEvent.id)))

    def audit(self, event_type: str) -> list[AuditEvent]:
        query = select(AuditEvent).where(AuditEvent.event_type == event_type)
        return list(self.db.scalars(query.order_by(AuditEvent.id)))


@pytest.fixture
def w(session: Session) -> World:
    return World(session)


# --- opening --------------------------------------------------------------------------------


def test_get_or_open_creates_an_open_day_with_an_open_event_and_an_audit_row(w: World) -> None:
    day = w.open()
    assert (day.agency_id, day.day, day.status) == (w.agency_id, TODAY, DAY_OPEN)
    events = w.events(day)
    assert [(e.kind, e.from_status, e.to_status, e.actor_user_id) for e in events] == [
        ("open", None, "open", w.staff.id)
    ]
    audit_rows = w.audit("day_opened")
    assert len(audit_rows) == 1
    assert audit_rows[0].details == {"report_day_id": day.id, "day": "2026-10-22"}


def test_get_or_open_is_idempotent(w: World) -> None:
    first = w.open()
    second = w.open(actor=w.manager)
    assert first.id == second.id
    assert w.db.scalar(select(func.count()).select_from(ReportDay)) == 1
    assert len(w.events(first)) == 1
    assert len(w.audit("day_opened")) == 1


def test_each_agency_has_its_own_day(w: World) -> None:
    mine = w.open()
    theirs = w.open(actor=w.other_manager)
    assert mine.id != theirs.id
    assert theirs.agency_id == w.other_agency_id
    assert days.get_day(w.db, w.agency_id, TODAY) is mine
    assert days.get_day(w.db, w.other_agency_id, TODAY) is theirs
    assert days.get_day(w.db, w.agency_id, TODAY - timedelta(days=1)) is None


def test_a_future_day_cannot_be_opened(w: World) -> None:
    with pytest.raises(DayError, match="futur"):
        days.get_or_open_day(w.db, w.manager, TODAY + timedelta(days=1), now=NOW)


def test_a_day_must_be_a_date(w: World) -> None:
    with pytest.raises(DayError):
        days.get_or_open_day(w.db, w.manager, NOW, now=NOW)  # a datetime is not a day
    with pytest.raises(DayError):
        days.get_or_open_day(w.db, w.manager, "2026-10-22", now=NOW)  # type: ignore[arg-type]


# --- writable -------------------------------------------------------------------------------


def test_require_writable_refuses_a_closed_day_only(w: World) -> None:
    day = w.open()
    days.require_writable(day)
    day.status = DAY_COMPARED
    days.require_writable(day)
    day.status = DAY_CLOSED
    with pytest.raises(DayClosed, match="clôtur"):
        days.require_writable(day)


# --- closing --------------------------------------------------------------------------------


def test_staff_cannot_close_a_day(w: World) -> None:
    day = w.open()
    with pytest.raises(DayError, match="clôturer"):
        days.close_day(w.db, w.staff, day, now=NOW)
    assert day.status == DAY_OPEN
    assert [e.kind for e in w.events(day)] == ["open"]


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_manager_and_accountant_close_a_day(w: World, who: str) -> None:
    day = w.open()
    actor = getattr(w, who)
    closed = days.close_day(w.db, actor, day, now=NOW)
    assert closed.status == DAY_CLOSED
    assert closed.closed_at == NOW
    assert closed.closed_by_user_id == actor.id
    event = w.events(day)[-1]
    assert (event.kind, event.from_status, event.to_status) == ("close", "open", "closed")
    assert w.audit("day_closed")[0].details == {"report_day_id": day.id, "day": "2026-10-22"}


def test_closing_twice_is_refused(w: World) -> None:
    day = w.open()
    days.close_day(w.db, w.manager, day, now=NOW)
    with pytest.raises(DayClosed):
        days.close_day(w.db, w.manager, day, now=NOW)
    assert [e.kind for e in w.events(day)] == ["open", "close"]


def test_another_agencys_day_is_not_found(w: World) -> None:
    theirs = w.open(actor=w.other_manager)
    with pytest.raises(DayError, match="introuvable"):
        days.close_day(w.db, w.manager, theirs, now=NOW)
    with pytest.raises(DayError, match="introuvable"):
        days.reopen_day(w.db, w.manager, theirs, "une raison valable", now=NOW)


# --- reopening ------------------------------------------------------------------------------


def test_only_the_manager_reopens_with_a_reason(w: World) -> None:
    day = w.open()
    days.close_day(w.db, w.accountant, day, now=NOW)
    for actor in (w.staff, w.accountant):
        with pytest.raises(DayError, match="gérant"):
            days.reopen_day(w.db, actor, day, "erreur de saisie", now=NOW)
    assert day.status == DAY_CLOSED
    reopened = days.reopen_day(w.db, w.manager, day, "  erreur de saisie  ", now=NOW)
    assert reopened.status == DAY_OPEN
    assert reopened.closed_at is None and reopened.closed_by_user_id is None
    event = w.events(day)[-1]
    assert (event.kind, event.from_status, event.to_status) == ("reopen", "closed", "open")
    assert event.reason == "erreur de saisie"
    assert event.actor_user_id == w.manager.id
    assert w.audit("day_reopened")[0].details == {"report_day_id": day.id, "day": "2026-10-22"}


@pytest.mark.parametrize("reason", [None, "", "    ", "abcd", " ab "])
def test_reopening_needs_a_reason_of_five_characters(w: World, reason: str | None) -> None:
    day = w.open()
    days.close_day(w.db, w.manager, day, now=NOW)
    with pytest.raises(DayError, match="motif"):
        days.reopen_day(w.db, w.manager, day, reason, now=NOW)
    assert day.status == DAY_CLOSED


def test_an_open_day_cannot_be_reopened(w: World) -> None:
    day = w.open()
    with pytest.raises(DayError, match="clôtur"):
        days.reopen_day(w.db, w.manager, day, "pas clôturée", now=NOW)


def test_reopening_goes_back_to_compared_when_an_upload_was_compared(w: World) -> None:
    upload = Upload(
        agency_id=w.agency_id,
        filename="x.xlsx",
        sha256="a" * 64,
        report_range_raw="22OCT",
        report_date=TODAY,
        generated_on=TODAY,
        ticket_count=0,
        warnings=[],
    )
    w.db.add(upload)
    w.db.commit()
    day = w.open()
    day.compared_upload_id = upload.id
    w.db.commit()
    days.close_day(w.db, w.manager, day, now=NOW)
    reopened = days.reopen_day(w.db, w.manager, day, "on revérifie", now=NOW)
    assert reopened.status == DAY_COMPARED
    assert w.events(day)[-1].to_status == DAY_COMPARED


def test_the_whole_history_is_kept_in_order(w: World) -> None:
    day = w.open()
    days.close_day(w.db, w.manager, day, now=NOW)
    days.reopen_day(w.db, w.manager, day, "premier motif", now=NOW)
    days.close_day(w.db, w.accountant, day, now=NOW)
    assert [(e.kind, e.to_status) for e in w.events(day)] == [
        ("open", "open"),
        ("close", "closed"),
        ("reopen", "open"),
        ("close", "closed"),
    ]
    assert [e.kind for e in days.day_events(w.db, w.agency_id, day.id)] == [
        "open",
        "close",
        "reopen",
        "close",
    ]
    assert days.day_events(w.db, w.other_agency_id, day.id) == []
