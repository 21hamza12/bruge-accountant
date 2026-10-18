from __future__ import annotations

import io
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

import app.cli
from app.cli import main
from tests.builders import build_workbook, ticket_row


@pytest.fixture
def ready_db(db_url: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["init-db", "--db", db_url]) == 0
    assert main(["add-agency", "bruge-voyages", "Bruge Voyages", "--db", db_url]) == 0
    capsys.readouterr()
    return db_url


def run_import(
    capsys: pytest.CaptureFixture[str], file: Path, db: str, *extra: str
) -> dict[str, Any]:
    code = main(["import", str(file), "--agency", "bruge-voyages", "--db", db, *extra])
    assert code == 0
    out: dict[str, Any] = json.loads(capsys.readouterr().out)
    return out


def test_import_fixture_prints_json(
    ready_db: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_import(capsys, fixture_path, ready_db)
    assert out["agency"] == "bruge-voyages"
    assert out["report_date"] == "2026-10-01"
    assert (out["inserted"], out["updated"]) == (48, 0)
    assert out["warnings"] == []
    assert isinstance(out["upload_id"], int)
    sv, er = out["staff"]
    assert (sv["staff_sign"], sv["ticket_count"], sv["group_count"]) == ("0004SV", 34, 25)
    assert sv["total_cost"] == "746423"
    assert (er["staff_sign"], er["ticket_count"], er["group_count"]) == ("0009ER", 14, 8)
    assert er["total_cost"] == "372434"


def test_import_staff_question_shape(
    ready_db: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_import(capsys, fixture_path, ready_db)
    er = out["staff"][1]
    q = next(q for q in er["questions"] if q["booking_ref"] == "QA0030")
    assert q == {
        "booking_ref": "QA0030",
        "airlines": ["495"],
        "ticket_count": 3,
        "total_cost": "37773",
        "known_cost_total": "37773",
        "missing_cost_count": 0,
        "passengers": [
            {
                "name": "PAX03/GIVEN03",
                "pax_type": "CHD",
                "ticket_no": "9990000154",
                "airline": "495",
                "cost": "17444",
            },
            {
                "name": "PAX14/GIVEN14",
                "pax_type": "ADT",
                "ticket_no": "9990000155",
                "airline": "495",
                "cost": "19428",
            },
            {
                "name": "PAX02/GIVEN02",
                "pax_type": "INF",
                "ticket_no": "9990000156",
                "airline": "495",
                "cost": "901",
            },
        ],
        "flags": [],
    }


def test_import_twice(
    ready_db: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = run_import(capsys, fixture_path, ready_db)
    second = run_import(capsys, fixture_path, ready_db)
    assert (second["inserted"], second["updated"]) == (0, 48)
    assert second["upload_id"] != first["upload_id"]
    assert second["staff"] == first["staff"]


def test_db_from_environment(
    db_url: str,
    fixture_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    assert main(["init-db"]) == 0
    assert main(["add-agency", "bruge-voyages", "Bruge Voyages"]) == 0
    capsys.readouterr()
    assert main(["import", str(fixture_path), "--agency", "bruge-voyages"]) == 0
    assert json.loads(capsys.readouterr().out)["inserted"] == 48


def test_unknown_agency_exits_2_and_creates_nothing(
    ready_db: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["import", str(fixture_path), "--agency", "bruge-voyges", "--db", ready_db])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "bruge-voyges" in captured.err
    # a correct import still reports 48 inserted: nothing was stored by the failed run
    assert run_import(capsys, fixture_path, ready_db)["inserted"] == 48


def test_parse_error_exits_1_without_traceback(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not an excel file")
    code = main(["import", str(bad), "--agency", "bruge-voyages", "--db", ready_db])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.strip() != ""
    assert "Traceback" not in captured.err


def test_missing_file_exits_1(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["import", str(tmp_path / "nope.xlsx"), "--agency", "bruge-voyages", "--db", ready_db]
    )
    assert code == 1
    assert "cannot read" in capsys.readouterr().err


def test_oversize_file_exits_1(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = tmp_path / "big.xlsx"
    big.write_bytes(b"0" * (10 * 1024 * 1024 + 1))
    assert main(["import", str(big), "--agency", "bruge-voyages", "--db", ready_db]) == 1
    assert "10 MB" in capsys.readouterr().err


def test_date_override(
    ready_db: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_import(capsys, fixture_path, ready_db, "--date", "2026-10-05")
    assert out["report_date"] == "2026-10-05"
    assert len(out["staff"]) == 2
    assert sum(s["ticket_count"] for s in out["staff"]) == 48


def test_invalid_date_is_usage_error(ready_db: str, fixture_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["import", str(fixture_path), "--agency", "bruge-voyages", "--date", "10/05/2026"])
    assert exc.value.code == 2


def test_uninitialised_database_exits_1(
    db_url: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["import", str(fixture_path), "--agency", "bruge-voyages", "--db", db_url])
    assert code == 1
    assert "init-db" in capsys.readouterr().err


def test_add_agency_duplicate_and_bad_slug(
    ready_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["add-agency", "bruge-voyages", "Again", "--db", ready_db]) == 2
    assert "already exists" in capsys.readouterr().err
    assert main(["add-agency", "Bad Slug!", "X", "--db", ready_db]) == 2
    assert "slug" in capsys.readouterr().err


def test_add_agency_prints_json(db_url: str, capsys: pytest.CaptureFixture[str]) -> None:
    main(["init-db", "--db", db_url])
    capsys.readouterr()
    assert main(["add-agency", "bruge-voyages", "Bruge Voyages", "--db", db_url]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["slug"] == "bruge-voyages"
    assert out["currency"] == "MRU"


def test_init_db_is_repeatable(db_url: str) -> None:
    assert main(["init-db", "--db", db_url]) == 0
    assert main(["init-db", "--db", db_url]) == 0


def test_unicode_and_warnings_in_output(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [ticket_row(name="ÉLODIE/BÉBÉ", doc=3940000001), ticket_row(cost=None, doc="3940000002")]
    path = tmp_path / "u.xlsx"
    path.write_bytes(build_workbook(rows))
    out = run_import(capsys, path, ready_db)
    assert len(out["warnings"]) == 1
    names = [p["name"] for p in out["staff"][0]["questions"][0]["passengers"]]
    assert "ÉLODIE/BÉBÉ" in names
    q = out["staff"][0]["questions"][0]
    assert q["total_cost"] is None
    assert "missing_cost" in q["flags"]


def test_fractional_cost_is_string(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "f.xlsx"
    path.write_bytes(build_workbook([ticket_row(cost=1234.5)]))
    out = run_import(capsys, path, ready_db)
    assert out["staff"][0]["total_cost"] == "1234.50"


def test_no_subcommand_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_python_dash_m_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["app", "--help"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("app", run_name="__main__")
    assert exc.value.code == 0


def test_money_is_always_a_string(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "m.xlsx"
    path.write_bytes(
        build_workbook(
            [
                ticket_row(doc="3940000001", cost=746423, rloc="AAAAAA"),
                ticket_row(doc="3940000002", cost=1234.5, rloc="BBBBBB"),
                ticket_row(doc="3940000003", cost=None, rloc="CCCCCC"),
            ]
        )
    )
    out = run_import(capsys, path, ready_db)
    questions = {q["booking_ref"]: q for q in out["staff"][0]["questions"]}
    assert questions["AAAAAA"]["total_cost"] == "746423"
    assert questions["BBBBBB"]["total_cost"] == "1234.50"
    assert questions["BBBBBB"]["passengers"][0]["cost"] == "1234.50"
    assert questions["CCCCCC"]["total_cost"] is None
    assert questions["CCCCCC"]["known_cost_total"] == "0"
    assert questions["CCCCCC"]["missing_cost_count"] == 1
    assert questions["CCCCCC"]["passengers"][0]["cost"] is None
    staff = out["staff"][0]
    assert staff["total_cost"] is None
    assert staff["known_cost_total"] == "747657.50"
    assert staff["missing_cost_count"] == 1


def test_blank_booking_refs_become_separate_questions(
    ready_db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "b.xlsx"
    path.write_bytes(
        build_workbook(
            [ticket_row(doc="3940000001", rloc=None), ticket_row(doc="3940000002", rloc=None)]
        )
    )
    out = run_import(capsys, path, ready_db)
    assert out["staff"][0]["group_count"] == 2
    assert [q["booking_ref"] for q in out["staff"][0]["questions"]] == ["", ""]


def test_arabic_output_on_a_legacy_codepage_stream(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    assert main(["init-db", "--db", db_url]) == 0
    assert main(["add-agency", "bruge", "بروج للسفر", "--db", db_url]) == 0
    stream.flush()
    text = raw.getvalue().decode("utf-8")
    assert json.loads(text[text.index("{") :])["name"] == "بروج للسفر"


def test_main_survives_streams_without_reconfigure(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Plain(io.StringIO):
        reconfigure = None

    monkeypatch.setattr(sys, "stdout", Plain())
    monkeypatch.setattr(sys, "stderr", Plain())
    assert main(["init-db", "--db", db_url]) == 0


def test_other_operational_error_shows_real_message(
    tmp_path: Path, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = f"sqlite:///{tmp_path / 'missing-dir' / 'x.db'}"
    code = main(["import", str(fixture_path), "--agency", "bruge-voyages", "--db", url])
    err = capsys.readouterr().err
    assert code == 1
    assert "unable to open database file" in err
    assert "init-db" not in err
    assert "Traceback" not in err


def test_init_db_failure_is_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = f"sqlite:///{tmp_path / 'missing-dir' / 'x.db'}"
    assert main(["init-db", "--db", url]) == 1
    err = capsys.readouterr().err
    assert "unable to open database file" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("command", ["init-db", "add-agency", "import"])
def test_invalid_database_url_is_clean(
    command: str, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = {
        "init-db": ["init-db"],
        "add-agency": ["add-agency", "x", "X"],
        "import": ["import", str(fixture_path), "--agency", "x"],
    }[command]
    assert main([*argv, "--db", "not a url"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "Traceback" not in err


@pytest.mark.parametrize(
    "exc",
    [
        IntegrityError("INSERT", {}, Exception("constraint failed: uq_x")),
        DBAPIError("INSERT", {}, Exception("driver exploded")),
    ],
    ids=["integrity", "dbapi"],
)
def test_database_errors_during_import_are_clean(
    exc: DBAPIError,
    ready_db: str,
    fixture_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(app.cli, "import_report", boom)
    code = main(["import", str(fixture_path), "--agency", "bruge-voyages", "--db", ready_db])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert str(exc.orig) in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("message", "ready"),
    [
        ("no such table: agencies", True),
        ('relation "agencies" does not exist', True),
        ("unable to open database file", False),
        ("could not connect to server", False),
    ],
)
def test_not_ready_message_only_for_missing_tables(
    message: str,
    ready: bool,
    ready_db: str,
    fixture_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT", {}, Exception(message))

    monkeypatch.setattr(app.cli, "get_agency_by_slug", boom)
    assert main(["import", str(fixture_path), "--agency", "x", "--db", ready_db]) == 1
    err = capsys.readouterr().err
    assert ("init-db" in err) is ready
    if not ready:
        assert message in err


def test_db_option_documented_as_credential_risk(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["import", "--help"])
    out = capsys.readouterr().out
    assert "DATABASE_URL" in out
    assert "history" in out
