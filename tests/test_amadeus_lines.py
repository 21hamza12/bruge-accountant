"""B2: every upload stores an append-only copy of every parsed line (``amadeus_lines``)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DatabaseError, IntegrityError

from app import repository
from app.db import make_engine, make_session_factory
from app.ingest import ingest_report
from app.models import AmadeusLine, Ticket, Upload
from app.parsing.amadeus import ParsedReport, ParseError, parse_report
from app.repository import create_agency, lines_upload_for
from app.tenancy import bind_tenant
from tests.builders import amadeus_line, build_amadeus_xlsx
from tests.conftest import migrate
from tests.fixtures import synthetic_20oct as fx
from tests.web_support import Seeded, WebDb, owner_session, seed


@pytest.fixture
def seeded(web_db: WebDb) -> Seeded:
    return seed(web_db.owner_url)


def lines_of(web_db: WebDb, agency_id: int, upload_id: int | None = None) -> list[AmadeusLine]:
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, agency_id)
        query = select(AmadeusLine).where(AmadeusLine.agency_id == agency_id)
        if upload_id is not None:
            query = query.where(AmadeusLine.upload_id == upload_id)
        return list(s.scalars(query.order_by(AmadeusLine.upload_id, AmadeusLine.seq_order)))


def ingest(web_db: WebDb, agency_id: int, data: bytes, **kw: Any) -> int:
    with owner_session(web_db.owner_url) as s:
        return ingest_report(s, agency_id, data, "r.xlsx", **kw).upload_id


def test_upload_stores_one_line_per_ticket_in_file_order_with_all_fields(
    web_db: WebDb, seeded: Seeded
) -> None:
    data = build_amadeus_xlsx(
        [
            amadeus_line(
                seq="000002", doc="1990000002", total="150.5", name="B/B (CHD)", rloc="BBB"
            ),
            amadeus_line(
                seq="000001",
                doc="1990000001",
                total=1000,
                name="A/A",
                staff="0006AC",
                airline="147",
                trnc="CANX",
                TAX=20,
                COMM="5.25",
                FP="CA",
                CONFIRMED="OK",
            ),
            amadeus_line(doc="1990000003", total=None, rloc=None),
        ]
    )
    upload_id = ingest(web_db, seeded.agency_a, data)
    rows = lines_of(web_db, seeded.agency_a, upload_id)
    assert [r.doc_number for r in rows] == ["1990000002", "1990000001", "1990000003"]
    assert [r.seq_order for r in rows] == [1, 2, 3]
    assert [r.source_row for r in rows] == [6, 7, 8]
    first, second, third = rows
    assert (first.seq_no, first.airline, first.total_doc) == ("000002", "199", Decimal("150.50"))
    assert (first.pax_name, first.pax_type, first.rloc) == ("B/B", "CHD", "BBB")
    assert (first.tax, first.comm, first.form_of_payment, first.confirmed) == (
        None,
        None,
        None,
        None,
    )
    assert (second.seq_no, second.airline, second.staff_sign, second.trnc) == (
        "000001",
        "147",
        "0006AC",
        "CANX",
    )
    assert (second.total_doc, second.tax, second.comm) == (
        Decimal("1000.00"),
        Decimal("20.00"),
        Decimal("5.25"),
    )
    assert (second.form_of_payment, second.confirmed) == ("CA", "OK")
    assert second.flags == ["cancelled"]
    assert third.total_doc is None
    assert third.flags == ["missing_booking_ref", "missing_cost"]
    assert all(r.agency_id == seeded.agency_a and r.created_at is not None for r in rows)


def test_the_synthetic_day_is_stored_completely(web_db: WebDb, seeded: Seeded) -> None:
    upload_id = ingest(web_db, seeded.agency_a, fx.build_synthetic_20oct())
    rows = lines_of(web_db, seeded.agency_a, upload_id)
    assert len(rows) == 43
    assert sum((r.total_doc or 0) for r in rows if r.trnc == "TKTT") == fx.TKTT_TOTAL
    assert [r.seq_no for r in rows] == [f"{n:06d}" for n in range(1, 44)]


def lines_upload(web_db: WebDb, agency_id: int, upload_id: int) -> int | None:
    with owner_session(web_db.owner_url) as s:
        return lines_upload_for(s, agency_id, upload_id)


def test_an_identical_reupload_stores_no_second_line_set(web_db: WebDb, seeded: Seeded) -> None:
    data = fx.build_synthetic_20oct()
    first = ingest(web_db, seeded.agency_a, data)
    with owner_session(web_db.owner_url) as s:
        again = ingest_report(s, seeded.agency_a, data, "r.xlsx")
    assert (again.inserted, again.updated) == (0, 43)  # the tickets behave as before
    assert again.upload_id != first
    assert len(lines_of(web_db, seeded.agency_a, first)) == 43
    assert lines_of(web_db, seeded.agency_a, again.upload_id) == []
    assert lines_upload(web_db, seeded.agency_a, first) == first
    assert lines_upload(web_db, seeded.agency_a, again.upload_id) == first
    third = ingest(web_db, seeded.agency_a, data)
    assert lines_upload(web_db, seeded.agency_a, third) == first  # always the earliest set
    assert len(lines_of(web_db, seeded.agency_a)) == 43


def test_a_different_file_for_the_same_day_adds_a_new_line_set(
    web_db: WebDb, seeded: Seeded
) -> None:
    first = ingest(web_db, seeded.agency_a, fx.build_synthetic_20oct())
    other = build_amadeus_xlsx(
        list(fx.LINES[:5]), range_raw=fx.REPORT_RANGE, current=fx.CURRENT_DATE
    )
    second = ingest(web_db, seeded.agency_a, other)
    assert len(lines_of(web_db, seeded.agency_a, first)) == 43
    assert len(lines_of(web_db, seeded.agency_a, second)) == 5
    assert lines_upload(web_db, seeded.agency_a, second) == second
    assert lines_upload(web_db, seeded.agency_a, first) == first


def test_the_same_bytes_for_another_report_date_store_their_own_lines(
    web_db: WebDb, seeded: Seeded
) -> None:
    data = fx.build_synthetic_20oct()
    first = ingest(web_db, seeded.agency_a, data)
    second = ingest(web_db, seeded.agency_a, data, report_date_override=date(2026, 10, 22))
    assert len(lines_of(web_db, seeded.agency_a, second)) == 43
    assert lines_upload(web_db, seeded.agency_a, second) == second
    assert first != second


def test_another_agency_with_the_same_file_stores_its_own_lines(
    web_db: WebDb, seeded: Seeded
) -> None:
    data = fx.build_synthetic_20oct()
    ingest(web_db, seeded.agency_a, data)
    b = ingest(web_db, seeded.agency_b, data)
    assert len(lines_of(web_db, seeded.agency_b, b)) == 43


def test_lines_upload_for_an_unknown_upload_is_none(web_db: WebDb, seeded: Seeded) -> None:
    assert lines_upload(web_db, seeded.agency_a, 999) is None


def test_duplicate_doc_numbers_reaching_the_store_are_refused() -> None:
    ticket = parse_report(build_amadeus_xlsx([amadeus_line()])).tickets[0]
    with pytest.raises(ValueError, match="duplicate DOC NUMBER"):
        repository._store_amadeus_lines(None, 1, 1, [ticket, ticket])  # type: ignore[arg-type]


def test_a_changed_reupload_keeps_the_original_values(web_db: WebDb, seeded: Seeded) -> None:
    first = ingest(web_db, seeded.agency_a, build_amadeus_xlsx([amadeus_line(total=1000)]))
    ingest(web_db, seeded.agency_a, build_amadeus_xlsx([amadeus_line(total=900)]))
    (original,) = lines_of(web_db, seeded.agency_a, first)
    assert original.total_doc == Decimal("1000.00")
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, seeded.agency_a)
        assert s.scalars(select(Ticket.cost)).one() == Decimal("900.00")


def test_a_duplicate_doc_number_in_a_file_keeps_the_first_line(
    web_db: WebDb, seeded: Seeded
) -> None:
    data = build_amadeus_xlsx(
        [
            amadeus_line(seq="000001", total=100),
            amadeus_line(seq="000002", doc="1990000002"),
            amadeus_line(seq="000003", total=999),
        ]
    )
    rows = lines_of(web_db, seeded.agency_a, ingest(web_db, seeded.agency_a, data))
    assert [(r.doc_number, r.total_doc, r.seq_order) for r in rows] == [
        ("1990000001", Decimal("100.00"), 1),
        ("1990000002", Decimal("1000.00"), 2),
    ]
    assert rows[0].flags == ["duplicate_in_file"]


def test_a_failed_parse_stores_no_lines(web_db: WebDb, seeded: Seeded) -> None:
    def broken(_: bytes) -> ParsedReport:
        raise ParseError("bad")

    with owner_session(web_db.owner_url) as s, pytest.raises(ParseError):
        ingest_report(s, seeded.agency_a, fx.build_synthetic_20oct(), "x.xlsx", parse=broken)
    assert lines_of(web_db, seeded.agency_a) == []


def test_a_failure_after_the_lines_were_written_rolls_them_back(
    web_db: WebDb, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_: object, **__: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(repository, "_upsert_chunk", boom)
    with owner_session(web_db.owner_url) as s, pytest.raises(RuntimeError):
        ingest_report(s, seeded.agency_a, fx.build_synthetic_20oct(), "x.xlsx")
    assert lines_of(web_db, seeded.agency_a) == []
    with owner_session(web_db.owner_url) as s:
        bind_tenant(s, seeded.agency_a)
        assert s.scalar(select(func.count()).select_from(Upload)) == 0


def test_lines_are_isolated_between_agencies(web_db: WebDb, seeded: Seeded) -> None:
    ingest(web_db, seeded.agency_a, fx.build_synthetic_20oct())
    ingest(web_db, seeded.agency_b, build_amadeus_xlsx([amadeus_line()]))
    assert len(lines_of(web_db, seeded.agency_a)) == 43
    assert len(lines_of(web_db, seeded.agency_b)) == 1
    if web_db.mode != "sqlite":  # SQLite has no row-level security: only the filter above
        with owner_session(web_db.owner_url) as s:
            bind_tenant(s, seeded.agency_b)
            assert s.scalar(select(func.count()).select_from(AmadeusLine)) == 1


def test_more_lines_than_one_chunk_keep_their_order(
    web_db: WebDb, seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "UPSERT_CHUNK", 7)
    data = build_amadeus_xlsx([amadeus_line(doc=f"199{n:07d}", seq=f"{n:06d}") for n in range(20)])
    rows = lines_of(web_db, seeded.agency_a, ingest(web_db, seeded.agency_a, data))
    assert [r.seq_order for r in rows] == list(range(1, 21))
    assert [r.seq_no for r in rows] == [f"{n:06d}" for n in range(20)]


def test_the_report_date_override_does_not_touch_the_lines(web_db: WebDb, seeded: Seeded) -> None:
    upload_id = ingest(
        web_db,
        seeded.agency_a,
        fx.build_synthetic_20oct(),
        report_date_override=date(2026, 10, 22),
    )
    assert len(lines_of(web_db, seeded.agency_a, upload_id)) == 43


def test_the_8_column_report_still_imports_with_empty_optional_columns(
    web_db: WebDb, seeded: Seeded, parsed_fixture: ParsedReport, fixture_path: Any
) -> None:
    rows = lines_of(
        web_db, seeded.agency_a, ingest(web_db, seeded.agency_a, fixture_path.read_bytes())
    )
    assert len(rows) == len(parsed_fixture.tickets)
    assert all(r.tax is None and r.form_of_payment is None for r in rows)


# --- the table itself (SQLite built by the migrations: triggers; Postgres is in test_rls.py) ----


def _sqlite_only(url: str) -> None:
    if not url.startswith("sqlite"):
        pytest.skip("SQLite triggers; the Postgres policies are tested in test_rls.py")


@pytest.fixture
def migrated(db_url: str) -> str:
    _sqlite_only(db_url)
    migrate(db_url)
    return db_url


def _ingest_one(url: str) -> tuple[int, int]:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as s:
            agency = create_agency(s, "agence-x", "Agence X").id
            result = ingest_report(s, agency, build_amadeus_xlsx([amadeus_line()]), "r.xlsx")
            return agency, result.upload_id
    finally:
        engine.dispose()


def test_sqlite_triggers_refuse_update_and_delete(migrated: str) -> None:
    _ingest_one(migrated)
    engine = create_engine(migrated)
    try:
        with engine.begin() as conn:
            assert conn.execute(text("SELECT count(*) FROM amadeus_lines")).scalar() == 1
        for statement in ("UPDATE amadeus_lines SET total_doc = 1", "DELETE FROM amadeus_lines"):
            with pytest.raises(DatabaseError, match="append-only"), engine.begin() as conn:
                conn.execute(text(statement))
        with engine.begin() as conn:
            assert conn.execute(text("SELECT total_doc FROM amadeus_lines")).scalar() == 1000
    finally:
        engine.dispose()


def test_the_composite_foreign_key_and_unique_key_hold(migrated: str) -> None:
    agency, upload = _ingest_one(migrated)
    engine = make_engine(migrated)
    insert = (
        "INSERT INTO amadeus_lines (agency_id, upload_id, seq_no, seq_order, doc_number, airline,"
        " pax_name, pax_type, staff_sign, rloc, trnc, flags, source_row) VALUES "
        "(:a, :u, '1', 1, :d, '199', 'X', 'ADT', 'S', 'R', 'TKTT', '[]', 6)"
    )
    try:
        for params in (
            {"a": agency + 1, "u": upload, "d": "2"},  # another agency's (or no) upload
            {"a": agency, "u": upload + 1, "d": "2"},  # an upload that does not exist
            {"a": agency, "u": upload, "d": "1990000001"},  # the same DOC NUMBER again
        ):
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(text(insert), params)
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text("DELETE FROM uploads"))  # RESTRICT: lines still point at it
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "bad",
    [{"seq_order": 0}, {"source_row": 0}, {"doc_number": ""}, {"pax_type": "XXX"}],
)
def test_check_constraints_refuse_bad_rows(migrated: str, bad: dict[str, Any]) -> None:
    agency, upload = _ingest_one(migrated)
    values: dict[str, Any] = {
        "a": agency,
        "u": upload,
        "seq_order": 2,
        "source_row": 7,
        "doc_number": "1990000099",
        "pax_type": "ADT",
    }
    values.update(bad)
    insert = (
        "INSERT INTO amadeus_lines (agency_id, upload_id, seq_no, seq_order, doc_number, airline,"
        " pax_name, pax_type, staff_sign, rloc, trnc, flags, source_row) VALUES "
        "(:a, :u, '1', :seq_order, :doc_number, '199', 'X', :pax_type, 'S', 'R', 'TKTT', '[]',"
        " :source_row)"
    )
    engine = make_engine(migrated)
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(text(insert), values)
    finally:
        engine.dispose()
