"""The command line against Postgres row-level security, in both deployment modes.

``add-agency`` needs the owner connection (the app role may not insert agencies); ``import`` runs
with the connection the application uses: the app role, or the owner in single-role mode.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main
from tests.conftest import RlsDatabase


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def sqlite_reference_output(tmp_path: Path, fixture_path: Path) -> str:
    """What the same commands print on SQLite: the contract the output must keep."""
    url = f"sqlite:///{tmp_path / 'reference.db'}"
    assert main(["init-db", "--db", url]) == 0
    assert main(["add-agency", "bruge-voyages", "Bruge Voyages", "--db", url]) == 0
    return _import_output(fixture_path, url)


def _import_output(fixture_path: Path, url: str) -> str:
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(["import", str(fixture_path), "--agency", "bruge-voyages", "--db", url])
    assert code == 0
    return buffer.getvalue()


def test_import_output_is_byte_identical_to_sqlite(
    rls_db: RlsDatabase,
    fixture_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = sqlite_reference_output(tmp_path, fixture_path)
    capsys.readouterr()
    code, out, _ = run(
        capsys, "add-agency", "bruge-voyages", "Bruge Voyages", "--db", rls_db.owner_url
    )
    assert code == 0 and json.loads(out)["slug"] == "bruge-voyages"
    code, out, err = run(
        capsys, "import", str(fixture_path), "--agency", "bruge-voyages", "--db", rls_db.app_url
    )
    assert (code, err) == (0, "")
    assert out == expected


def test_importing_twice_through_the_app_connection_updates(
    rls_db: RlsDatabase, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["add-agency", "x-agency", "X", "--db", rls_db.owner_url]) == 0
    capsys.readouterr()
    args = ["import", str(fixture_path), "--agency", "x-agency", "--db", rls_db.app_url]
    _, first, _ = run(capsys, *args)
    _, second, _ = run(capsys, *args)
    assert (json.loads(first)["inserted"], json.loads(first)["updated"]) == (48, 0)
    assert (json.loads(second)["inserted"], json.loads(second)["updated"]) == (0, 48)


def test_unknown_agency_is_a_usage_error_under_rls(
    rls_db: RlsDatabase, fixture_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        capsys, "import", str(fixture_path), "--agency", "ghost", "--db", rls_db.app_url
    )
    assert (code, out) == (2, "")
    assert "unknown agency" in err


def test_add_agency_duplicate_under_rls_is_a_usage_error(
    rls_db: RlsDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["add-agency", "twice", "Twice", "--db", rls_db.owner_url]) == 0
    capsys.readouterr()
    code, out, err = run(capsys, "add-agency", "twice", "Again", "--db", rls_db.owner_url)
    assert (code, out) == (2, "")
    assert "already exists" in err


def test_the_app_role_cannot_add_agencies_and_is_told_which_connection_to_use(
    rls_db: RlsDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    if rls_db.mode != "app-role":
        pytest.skip("the owner is the app connection in single-role mode")
    code, out, err = run(capsys, "add-agency", "nope", "Nope", "--db", rls_db.app_url)
    assert (code, out) == (1, "")
    assert "permission denied" in err
    assert "MIGRATION_DATABASE_URL" in err
    assert "Traceback" not in err
