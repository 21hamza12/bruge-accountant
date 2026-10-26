"""Atomicity, upsert semantics and schema constraints of the storage layer."""

from __future__ import annotations

import dataclasses
import threading
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import (
    String,
    create_engine,
    create_mock_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import DataError, IntegrityError, StatementError
from sqlalchemy.orm import Session

from app import repository
from app.db import Base, make_engine, make_session_factory
from app.models import Agency, AmadeusLine, Ticket, Upload
from app.parsing.amadeus import FIELD_LIMITS, ParsedReport, parse_report
from app.repository import (
    create_agency,
    get_agency_by_slug,
    import_report,
    tickets_for_day,
)
from app.tenancy import rebind_tenant
from tests.builders import build_workbook, ticket_row
from tests.conftest import SHA


def count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Ticket)) or 0


def uploads(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Upload)) or 0


# --- atomicity ----------------------------------------------------------------------


def test_failure_midway_rolls_back_everything(
    session: Session, parsed_fixture: ParsedReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    agency = create_agency(session, "a", "A")
    monkeypatch.setattr(repository, "UPSERT_CHUNK", 5)
    real = repository._upsert_chunk
    calls: list[int] = []

    def flaky(sess: Session, rows: list[dict[str, Any]]) -> None:
        calls.append(len(rows))
        if len(calls) == 3:  # the upload row and 10 tickets are already flushed here
            assert count(sess) == 10
            assert uploads(sess) == 1
            raise RuntimeError("disk on fire")
        real(sess, rows)

    monkeypatch.setattr(repository, "_upsert_chunk", flaky)
    with pytest.raises(RuntimeError, match="disk on fire"):
        import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    assert calls == [5, 5, 5]
    assert count(session) == 0
    assert uploads(session) == 0

    monkeypatch.setattr(repository, "_upsert_chunk", real)
    result = import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    assert (result.inserted, result.updated) == (48, 0)
    assert count(session) == 48


def test_failure_at_commit_rolls_back_everything(
    session: Session, parsed_fixture: ParsedReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    agency = create_agency(session, "a", "A")
    real_commit = session.commit

    def failing_commit() -> None:
        raise RuntimeError("commit refused")

    monkeypatch.setattr(session, "commit", failing_commit)
    with pytest.raises(RuntimeError, match="commit refused"):
        import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    monkeypatch.setattr(session, "commit", real_commit)
    assert count(session) == 0
    assert uploads(session) == 0
    assert import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA).inserted == 48


def test_upsert_failure_on_existing_data_keeps_old_values(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    agency = create_agency(session, "a", "A")
    import_report(session, agency.id, parse_report(build_workbook([ticket_row(cost=1)])), "1", SHA)
    real = repository._upsert_chunk

    def fail_after(sess: Session, rows: list[dict[str, Any]]) -> None:
        real(sess, rows)
        raise RuntimeError("late failure")

    monkeypatch.setattr(repository, "_upsert_chunk", fail_after)
    changed = parse_report(build_workbook([ticket_row(cost=2)]))
    with pytest.raises(RuntimeError, match="late failure"):
        import_report(session, agency.id, changed, "2", SHA)
    ticket = session.scalars(select(Ticket)).one()
    session.refresh(ticket)
    assert ticket.cost == Decimal(1)
    assert uploads(session) == 1


def test_keyboard_interrupt_is_not_swallowed(
    session: Session, parsed_fixture: ParsedReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    agency = create_agency(session, "a", "A")

    def interrupted(sess: Session, rows: list[dict[str, Any]]) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(repository, "_upsert_chunk", interrupted)
    with pytest.raises(KeyboardInterrupt):
        import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)


# --- report_date is fixed at first import ---------------------------------------------


def test_report_date_never_moves_on_reimport(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    day1 = parse_report(
        build_workbook([ticket_row(doc="3940000001"), ticket_row(doc="3940000002")])
    )
    day2 = parse_report(
        build_workbook(
            [ticket_row(doc="3940000001", cost=555), ticket_row(doc="3940000003")],
            range_raw="02OCT",
            current="03-OCT-2026",
        )
    )
    import_report(session, agency.id, day1, "01OCT.xlsx", SHA)
    result = import_report(session, agency.id, day2, "02OCT.xlsx", SHA)
    assert (result.inserted, result.updated) == (1, 1)
    session.expire_all()

    kept = session.scalars(select(Ticket).filter_by(ticket_no="3940000001")).one()
    assert kept.report_date == date(2026, 10, 1)
    assert kept.cost == Decimal(555)  # the other Amadeus fields are refreshed
    assert kept.last_upload_id == result.upload_id
    new = session.scalars(select(Ticket).filter_by(ticket_no="3940000003")).one()
    assert new.report_date == date(2026, 10, 2)

    first_day = tickets_for_day(session, agency.id, date(2026, 10, 1))
    assert {t.ticket_no for t in first_day} == {"3940000001", "3940000002"}
    second_day = tickets_for_day(session, agency.id, date(2026, 10, 2))
    assert {t.ticket_no for t in second_day} == {"3940000003"}


def test_reimport_keeps_created_at_and_first_upload(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    parsed = parse_report(build_workbook([ticket_row()]))
    first = import_report(session, agency.id, parsed, "1.xlsx", SHA)
    before = session.scalars(select(Ticket)).one()
    created, stamp = before.created_at, before.updated_at
    second = import_report(session, agency.id, parsed, "2.xlsx", SHA)
    after = session.scalars(select(Ticket)).one()
    assert after.created_at == created
    assert after.first_upload_id == first.upload_id
    assert after.last_upload_id == second.upload_id
    assert after.updated_at > stamp  # onupdate does not fire for core upserts; we set it


def test_import_refreshes_tickets_already_loaded_in_the_session(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    import_report(session, agency.id, parse_report(build_workbook([ticket_row(cost=1)])), "1", SHA)
    loaded = session.scalars(select(Ticket)).one()
    assert loaded.cost == Decimal(1)
    import_report(session, agency.id, parse_report(build_workbook([ticket_row(cost=2)])), "2", SHA)
    assert loaded.cost == Decimal(2)


def test_duplicate_in_file_flag_is_stored(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    import_report(
        session, agency.id, parse_report(build_workbook([ticket_row(), ticket_row()])), "1", SHA
    )
    assert session.scalars(select(Ticket)).one().flags == ["duplicate_in_file"]


def test_rows_are_upserted_in_ticket_number_order(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    agency = create_agency(session, "a", "A")
    seen: list[str] = []
    real = repository._upsert_chunk

    def spy(sess: Session, rows: list[dict[str, Any]]) -> None:
        seen.extend(r["ticket_no"] for r in rows)
        real(sess, rows)

    monkeypatch.setattr(repository, "_upsert_chunk", spy)
    monkeypatch.setattr(repository, "UPSERT_CHUNK", 2)
    rows = [ticket_row(doc=d) for d in ("3940000005", "3940000001", "3940000003", "3940000002")]
    import_report(session, agency.id, parse_report(build_workbook(rows)), "1", SHA)
    assert seen == sorted(seen) and len(seen) == 4


# --- upsert statement / concurrency -------------------------------------------------------


@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_upsert_statement_leaves_protected_columns_alone(dialect_name: str) -> None:
    dialect = create_mock_engine(f"{dialect_name}://", lambda *a, **k: None).dialect
    rows = [
        {
            "agency_id": 1,
            "ticket_no": "3940000001",
            "price_status": "awaiting_price",
            "first_upload_id": 1,
            "created_at": datetime.now(UTC),
            "report_date": date(2026, 10, 1),
        }
    ]
    sql = str(repository._upsert_statement(dialect.name, rows).compile(dialect=dialect))
    head, _, update = sql.partition("DO UPDATE SET")
    assert "ON CONFLICT (agency_id, ticket_no)" in head
    for protected in ("price_status", "first_upload_id", "created_at", "report_date"):
        assert protected not in update
    for refreshed in ("cost", "staff_sign", "flags", "last_upload_id", "updated_at"):
        assert f"{refreshed} =" in update


def test_unsupported_dialect_is_rejected() -> None:
    with pytest.raises(NotImplementedError, match="mysql"):
        repository._upsert_statement("mysql", [])


def test_concurrent_imports_do_not_collide(tmp_path: Path, parsed_fixture: ParsedReport) -> None:
    url = f"sqlite:///{tmp_path / 'race.db'}"
    setup = make_engine(url)
    Base.metadata.create_all(setup)
    with make_session_factory(setup)() as s:
        agency_id = create_agency(s, "a", "A").id
    errors: list[BaseException] = []
    results: list[tuple[int, int]] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        engine = make_engine(url)
        try:
            with make_session_factory(engine)() as sess:
                barrier.wait()
                res = import_report(sess, agency_id, parsed_fixture, "1OCT.xlsx", SHA)
                results.append((res.inserted, res.updated))
        except BaseException as exc:
            errors.append(exc)
        finally:
            engine.dispose()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with make_session_factory(setup)() as s:
        assert count(s) == 48
        assert uploads(s) == 4
    setup.dispose()
    assert sum(i for i, _ in results) == 48  # SQLite serialises writers, so the counts are exact


# --- schema hardening -----------------------------------------------------------------------


def test_every_constraint_has_an_explicit_name(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    insp = inspect(engine)
    for table in ("agencies", "uploads", "tickets"):
        assert insp.get_pk_constraint(table)["name"] == f"pk_{table}"
        for fk in insp.get_foreign_keys(table):
            assert fk["name"]
            assert fk["options"].get("ondelete") == "RESTRICT"
        for uq in insp.get_unique_constraints(table):
            assert uq["name"]
        for ck in insp.get_check_constraints(table):
            assert ck["name"]
    engine.dispose()


def test_tenant_consistent_foreign_keys_and_indexes(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    insp = inspect(engine)
    composite = {
        (tuple(fk["constrained_columns"]), tuple(fk["referred_columns"]))
        for fk in insp.get_foreign_keys("tickets")
        if fk["referred_table"] == "uploads"
    }
    assert composite == {
        (("agency_id", "first_upload_id"), ("agency_id", "id")),
        (("agency_id", "last_upload_id"), ("agency_id", "id")),
    }
    assert any(
        u["column_names"] == ["agency_id", "id"] for u in insp.get_unique_constraints("uploads")
    )
    tickets_idx = [i["column_names"] for i in insp.get_indexes("tickets")]
    assert ["agency_id", "first_upload_id"] in tickets_idx
    assert ["agency_id", "last_upload_id"] in tickets_idx
    uploads_idx = [i["column_names"] for i in insp.get_indexes("uploads")]
    assert ["agency_id", "sha256"] in uploads_idx
    assert ["agency_id", "report_date"] in uploads_idx
    engine.dispose()


def test_migration_and_models_agree_on_checks(alembic_cfg: tuple[Config, str]) -> None:
    cfg, url = alembic_cfg
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    names = {c["name"] for c in inspect(engine).get_check_constraints("tickets")}
    assert names == {"ck_tickets_price_status", "ck_tickets_pax_type"}
    assert {c["name"] for c in inspect(engine).get_check_constraints("uploads")} == {
        "ck_uploads_ticket_count_nonneg",
        "ck_uploads_inserted_count_nonneg",
        "ck_uploads_updated_count_nonneg",
    }
    engine.dispose()


def _ticket_kwargs(upload_id: int, agency_id: int, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "agency_id": agency_id,
        "first_upload_id": upload_id,
        "last_upload_id": upload_id,
        "report_date": date(2026, 10, 1),
        "ticket_no": "3940000009",
        "seq_no": "1",
        "airline": "199",
        "passenger_name": "X",
        "pax_type": "ADT",
        "staff_sign": "S",
        "transaction_type": "TKTT",
        "booking_ref": "R",
        "flags": [],
        "price_status": "awaiting_price",
    }
    return base | overrides


def test_ticket_cannot_point_at_another_agencys_upload(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    a = create_agency(session, "a", "A")
    b = create_agency(session, "b", "B")
    rebind_tenant(session, a.id)
    upload_a = import_report(session, a.id, parsed_fixture, "1OCT.xlsx", SHA).upload_id
    session.add(Ticket(**_ticket_kwargs(upload_a, b.id)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


@pytest.mark.parametrize(
    "overrides",
    [{"price_status": "free"}, {"pax_type": "ADX"}, {"pax_type": ""}],
    ids=["price_status", "pax_type", "pax_type_empty"],
)
def test_check_constraints_reject_bad_values(session: Session, overrides: dict[str, Any]) -> None:
    agency = create_agency(session, "a", "A")
    parsed = parse_report(build_workbook([ticket_row()]))
    upload = import_report(session, agency.id, parsed, "1.xlsx", SHA).upload_id
    session.add(Ticket(**_ticket_kwargs(upload, agency.id, **overrides)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_parser_field_limits_match_column_lengths() -> None:
    # SQLite ignores VARCHAR lengths; Postgres rejects longer values, so the parser's
    # truncation limits must never drift above the real columns.
    for key, limit in FIELD_LIMITS.items():
        # The optional text columns exist on amadeus_lines only.
        table = Ticket.__table__ if key in Ticket.__table__.c else AmadeusLine.__table__
        column_type = table.c[key].type
        assert isinstance(column_type, String)
        assert column_type.length == limit, key


def test_server_rejects_text_longer_than_its_column(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        pytest.skip("SQLite does not enforce VARCHAR lengths")
    agency = create_agency(session, "a", "A")
    upload = import_report(
        session, agency.id, parse_report(build_workbook([ticket_row()])), "1.xlsx", SHA
    ).upload_id
    session.add(Ticket(**_ticket_kwargs(upload, agency.id, airline="X" * 9)))
    with pytest.raises(DataError, match="too long"):
        session.commit()
    session.rollback()


def test_negative_ticket_count_rejected(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    session.add(
        Upload(
            agency_id=agency.id,
            filename="x",
            sha256=SHA,
            report_range_raw="01OCT",
            report_date=date(2026, 10, 1),
            generated_on=date(2026, 10, 2),
            ticket_count=-1,
            warnings=[],
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_restrict_prevents_deleting_referenced_rows(
    session: Session, parsed_fixture: ParsedReport
) -> None:
    agency = create_agency(session, "a", "A")
    import_report(session, agency.id, parsed_fixture, "1OCT.xlsx", SHA)
    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM agencies"))
    session.rollback()
    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM uploads"))
    session.rollback()


def test_timestamps_are_utc_aware_and_naive_is_rejected(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    session.expire_all()
    assert agency.created_at.utcoffset() == timedelta(0)
    assert abs((datetime.now(UTC) - agency.created_at).total_seconds()) < 60

    agency.created_at = datetime(2026, 1, 1)  # naive on purpose
    with pytest.raises(StatementError, match="naive"):
        session.commit()
    session.rollback()


def test_timestamps_convert_other_offsets_to_utc(session: Session) -> None:
    agency = create_agency(session, "a", "A")
    stamp = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    agency.created_at = stamp
    session.commit()
    session.expire_all()
    assert agency.created_at == stamp
    assert agency.created_at.utcoffset() == timedelta(0)
    assert agency.created_at.hour == 10


def test_server_default_fills_timestamps_on_raw_insert(session: Session) -> None:
    session.execute(
        text("INSERT INTO agencies (slug, name, currency) VALUES ('raw', 'Raw', 'MRU')")
    )
    agency = get_agency_by_slug(session, "raw")
    assert isinstance(agency, Agency)
    assert agency.created_at.utcoffset() == timedelta(0)


def test_long_names_are_stored_in_full(session: Session) -> None:
    # The parser caps names at 200 characters; the storage layer itself must not truncate.
    agency = create_agency(session, "a", "A")
    long_name = "N" * 1000
    parsed = parse_report(build_workbook([ticket_row(name="N")]))
    parsed = dataclasses.replace(
        parsed, tickets=(dataclasses.replace(parsed.tickets[0], passenger_name=long_name),)
    )
    result = import_report(session, agency.id, parsed, "f" * 1000, SHA)
    upload = session.get(Upload, result.upload_id)
    assert upload is not None and upload.filename == "f" * 1000
    assert session.scalars(select(Ticket)).one().passenger_name == long_name


def test_postgres_ddl_uses_bigint_jsonb_and_timestamptz(
    alembic_cfg: tuple[Config, str], capsys: pytest.CaptureFixture[str]
) -> None:
    cfg, _ = alembic_cfg
    cfg.set_main_option("sqlalchemy.url", "postgresql://user:pw@localhost/db")
    command.upgrade(cfg, "head", sql=True)
    ddl = capsys.readouterr().out
    assert "id BIGSERIAL NOT NULL" in ddl
    assert "first_upload_id BIGINT NOT NULL" in ddl
    assert "flags JSONB NOT NULL" in ddl
    assert "warnings JSONB NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl
    assert "REFERENCES uploads (agency_id, id) ON DELETE RESTRICT" in ddl
