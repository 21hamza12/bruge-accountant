from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from app.cli import main
from tests.builders import build_workbook, ticket_row


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'cli.db'}"


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
    assert sv["total_cost"] == 746423
    assert (er["staff_sign"], er["ticket_count"], er["group_count"]) == ("0009ER", 14, 8)
    assert er["total_cost"] == 372434


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
        "total_cost": 37773,
        "passengers": [
            {
                "name": "PAX03/GIVEN03",
                "pax_type": "CHD",
                "ticket_no": "9990000154",
                "airline": "495",
                "cost": 17444,
            },
            {
                "name": "PAX14/GIVEN14",
                "pax_type": "ADT",
                "ticket_no": "9990000155",
                "airline": "495",
                "cost": 19428,
            },
            {
                "name": "PAX02/GIVEN02",
                "pax_type": "INF",
                "ticket_no": "9990000156",
                "airline": "495",
                "cost": 901,
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
