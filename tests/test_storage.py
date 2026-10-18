from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base, default_database_url
from app.models import Ticket, Upload
from app.parsing.amadeus import ParsedReport, parse_report
from app.repository import (
    create_agency,
    get_agency_by_slug,
    import_report,
    tickets_awaiting_price,
)
from tests.builders import build_workbook, ticket_row
from tests.conftest import SHA, allow_future_price_statuses


def count(session: Session, **filters: object) -> int:
    stmt = select(func.count()).select_from(Ticket).filter_by(**filters)
    return session.scalar(stmt) or 0


def test_agency_defaults_and_lookup(session: Session) -> None:
    agency = create_agency(session, "bruge-voyages", "Bruge Voyages")
    assert agency.currency == "MRU"
    assert agency.created_at is not None
    found = get_agency_by_slug(session, "bruge-voyages")
    assert found is not None and found.id == agency.id
    assert get_agency_by_slug(session, "nope") is None


def test_agency_slug_unique(session: Session) -> None:
    create_agency(session, "a", "A")
    with pytest.raises(IntegrityError):
        create_agency(session, "a", "Again")


def test_import_fixture(session: Session, parsed_fixture: ParsedReport) -> None:
    agency = create_agency(session, "bruge-voyages", "Bruge Voyages")
    result = import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    assert (result.inserted, result.updated) == (48, 0)

    tickets = session.scalars(select(Ticket)).all()
    assert len(tickets) == 48
    assert {t.price_status for t in tickets} == {"awaiting_price"}
    assert {t.agency_id for t in tickets} == {agency.id}
    assert {t.first_upload_id for t in tickets} == {result.upload_id}
    assert sum((t.cost or Decimal(0)) for t in tickets) == Decimal(1118857)

    upload = session.get(Upload, result.upload_id)
    assert upload is not None
    assert upload.agency_id == agency.id
    assert upload.filename == "1OCT.xlsx"
    assert upload.sha256 == SHA
    assert upload.report_range_raw == "01OCT"
    assert upload.report_date == date(2026, 10, 1)
    assert upload.generated_on == date(2026, 10, 2)
    assert upload.ticket_count == 48
    assert upload.warnings == []


def test_ticket_fields_roundtrip(session: Session, parsed_fixture: ParsedReport) -> None:
    agency = create_agency(session, "bruge-voyages", "Bruge Voyages")
    import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    t = session.scalars(select(Ticket).filter_by(ticket_no="9990000156")).one()
    assert t.seq_no == "081370"
    assert t.passenger_name == "PAX02/GIVEN02"
    assert t.pax_type == "INF"
    assert t.cost == Decimal(901)
    assert t.staff_sign == "0009ER"
    assert t.booking_ref == "QA0030"
    assert t.report_date == date(2026, 10, 1)
    assert t.flags == []
    assert t.created_at is not None and t.updated_at is not None


def test_flags_warnings_and_null_cost_persist(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    data = build_workbook(
        [ticket_row(cost=None, trnc="RFND"), ticket_row(doc=None)],
    )
    parsed = parse_report(data)
    result = import_report(session, agency.id, parsed, "x.xlsx", SHA)
    t = session.scalars(select(Ticket)).one()
    assert t.cost is None
    assert t.flags == ["missing_cost", "unknown_trnc"]
    assert session.get(Upload, result.upload_id).warnings == list(parsed.warnings)  # type: ignore[union-attr]
    assert len(parsed.warnings) == 1


def test_import_twice_is_idempotent(session: Session, parsed_fixture: ParsedReport) -> None:
    agency = create_agency(session, "bruge-voyages", "Bruge Voyages")
    first = import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    second = import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    assert (second.inserted, second.updated) == (0, 48)
    assert count(session) == 48
    assert session.scalar(select(func.count()).select_from(Upload)) == 2
    assert first.upload_id != second.upload_id
    tickets = session.scalars(select(Ticket)).all()
    assert {t.first_upload_id for t in tickets} == {first.upload_id}
    assert {t.last_upload_id for t in tickets} == {second.upload_id}


def test_two_agencies_are_isolated(session: Session, parsed_fixture: ParsedReport) -> None:
    a = create_agency(session, "a", "A")
    b = create_agency(session, "b", "B")
    import_report(session, a.id, parsed_fixture, "1OCT.xlsx", SHA)
    result = import_report(session, b.id, parsed_fixture, "1OCT.xlsx", SHA)
    assert result.inserted == 48
    assert count(session) == 96
    assert count(session, agency_id=a.id) == 48
    assert count(session, agency_id=b.id) == 48

    # Re-importing for A must not touch B's tickets.
    b_uploads = {
        t.last_upload_id for t in session.scalars(select(Ticket).filter_by(agency_id=b.id))
    }
    import_report(session, a.id, parsed_fixture, "1OCT.xlsx", SHA)
    session.expire_all()
    assert {
        t.last_upload_id for t in session.scalars(select(Ticket).filter_by(agency_id=b.id))
    } == b_uploads


def test_reimport_never_touches_price_status(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    agency = create_agency(session, "a", "A")
    import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    allow_future_price_statuses(session)
    ticket = session.scalars(select(Ticket).filter_by(ticket_no="9990000110")).one()
    ticket.price_status = "priced"
    session.commit()
    import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    session.expire_all()
    ticket = session.scalars(select(Ticket).filter_by(ticket_no="9990000110")).one()
    assert ticket.price_status == "priced"
    assert count(session, price_status="awaiting_price") == 47


def test_reimport_updates_amadeus_fields(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    first = parse_report(build_workbook([ticket_row(cost=1000, name="DOE/JOHN")]))
    import_report(session, agency.id, first, "1.xlsx", SHA)
    created = session.scalars(select(Ticket)).one().updated_at

    second = parse_report(
        build_workbook([ticket_row(cost=None, name="DOE/JOHN (INF)", staff="0009ER")])
    )
    result = import_report(session, agency.id, second, "2.xlsx", SHA)
    assert (result.inserted, result.updated) == (0, 1)
    session.expire_all()
    t = session.scalars(select(Ticket)).one()
    assert t.cost is None
    assert t.pax_type == "INF"
    assert t.staff_sign == "0009ER"
    assert t.flags == ["missing_cost"]
    assert t.updated_at >= created


def test_unique_constraint_agency_ticket(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    parsed = parse_report(build_workbook([ticket_row()]))
    import_report(session, agency.id, parsed, "1.xlsx", SHA)
    existing = session.scalars(select(Ticket)).one()
    session.add(
        Ticket(
            agency_id=agency.id,
            first_upload_id=existing.first_upload_id,
            last_upload_id=existing.last_upload_id,
            report_date=existing.report_date,
            ticket_no=existing.ticket_no,
            seq_no="1",
            airline="1",
            passenger_name="X",
            pax_type="ADT",
            staff_sign="S",
            transaction_type="TKTT",
            booking_ref="R",
            flags=[],
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_unknown_agency_writes_nothing(session: Session, parsed_fixture: ParsedReport) -> None:
    with pytest.raises(LookupError):
        import_report(session, 999, parsed_fixture, "1OCT.xlsx", SHA)
    assert count(session) == 0
    assert session.scalar(select(func.count()).select_from(Upload)) == 0


def test_large_import_spans_lookup_chunks(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    rows = [ticket_row(doc=f"{3940000000 + i}") for i in range(1200)]
    parsed = parse_report(build_workbook(rows))
    assert import_report(session, agency.id, parsed, "big.xlsx", SHA).inserted == 1200
    again = import_report(session, agency.id, parsed, "big.xlsx", SHA)
    assert (again.inserted, again.updated) == (0, 1200)


def test_tickets_awaiting_price_filters(session: Session, parsed_fixture: ParsedReport) -> None:
    a = create_agency(session, "a", "A")
    b = create_agency(session, "b", "B")
    import_report(session, a.id, parsed_fixture, "1OCT.xlsx", SHA)
    import_report(session, b.id, parsed_fixture, "1OCT.xlsx", SHA)
    day = date(2026, 10, 1)
    assert len(tickets_awaiting_price(session, a.id, day)) == 48
    assert tickets_awaiting_price(session, a.id, date(2026, 10, 2)) == []
    assert tickets_awaiting_price(session, 999, day) == []
    allow_future_price_statuses(session)
    victim = tickets_awaiting_price(session, a.id, day)[0]
    victim.price_status = "priced"
    session.commit()
    result = tickets_awaiting_price(session, a.id, day)
    assert len(result) == 47
    assert {t.agency_id for t in result} == {a.id}


def test_default_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert default_database_url() == "sqlite:///bruge.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///other.db")
    assert default_database_url() == "sqlite:///other.db"


# --- Alembic ------------------------------------------------------------------------


def test_alembic_upgrade_creates_schema(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    insp = inspect(engine)
    assert {"agencies", "uploads", "tickets"} <= set(insp.get_table_names())

    uniques = insp.get_unique_constraints("tickets")
    assert any(u["column_names"] == ["agency_id", "ticket_no"] for u in uniques)
    index_cols = [i["column_names"] for i in insp.get_indexes("tickets")]
    assert ["agency_id", "report_date", "staff_sign"] in index_cols
    assert ["agency_id", "booking_ref"] in index_cols
    assert any(
        u["column_names"] == ["slug"] for u in insp.get_unique_constraints("agencies")
    ) or any(i["column_names"] == ["slug"] and i["unique"] for i in insp.get_indexes("agencies"))
    fk_targets = {fk["referred_table"] for fk in insp.get_foreign_keys("tickets")}
    assert fk_targets == {"agencies", "uploads"}
    assert not any(
        "price" in c["name"] and c["name"] != "price_status" for c in insp.get_columns("tickets")
    )
    assert "agency_id" in {c["name"] for c in insp.get_columns("uploads")}
    ticket_cols = {c["name"]: c for c in insp.get_columns("tickets")}
    assert ticket_cols["agency_id"]["nullable"] is False
    assert ticket_cols["cost"]["nullable"] is True
    engine.dispose()


def test_alembic_matches_models(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    engine.dispose()
    assert diff == []


def test_alembic_downgrade_removes_tables(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def test_alembic_env_defaults_to_local_sqlite(
    alembic_cfg: tuple[Config, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg, _ = alembic_cfg
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.chdir(tmp_path)
    command.upgrade(cfg, "head")
    assert os.path.exists(tmp_path / "bruge.db")
