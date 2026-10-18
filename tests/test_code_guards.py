"""Static guards: raw SQL and unescaped HTML must not creep into the application code.

All SQL goes through the ORM / SQL expression layer (bound parameters), and all HTML through
autoescaped Jinja. Migrations live outside ``app/`` and are exempt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
SCANNED_SUFFIXES = {".py", ".html", ".jinja", ".j2", ".txt"}

BANNED = {
    "text(": re.compile(r"(?<![A-Za-z0-9_])text\("),
    "exec_driver_sql": re.compile(r"exec_driver_sql"),
    "|safe": re.compile(r"\|\s*safe\b"),
    "Markup(": re.compile(r"(?<![A-Za-z0-9_])Markup\("),
}


def find_violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCANNED_SUFFIXES:
            continue
        if "migrations" in path.relative_to(root).parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for label, pattern in BANNED.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(root)}:{lineno}: {label}")
    return found


def test_app_has_no_raw_sql_or_unescaped_html() -> None:
    assert find_violations(APP_DIR) == []


def test_scanner_is_not_vacuous() -> None:
    assert list(APP_DIR.rglob("*.py")), "scanner would see no files"
    assert list(APP_DIR.rglob("*.html")), "scanner would see no templates"


@pytest.mark.parametrize(
    ("filename", "content", "label"),
    [
        ("a.py", "conn.execute(text('select 1'))\n", "text("),
        ("a.py", "sa.text('x')\n", "text("),
        ("a.py", "conn.exec_driver_sql('x')\n", "exec_driver_sql"),
        ("a.html", "{{ name|safe }}\n", "|safe"),
        ("a.html", "{{ name | safe }}\n", "|safe"),
        ("a.py", "return Markup(x)\n", "Markup("),
    ],
)
def test_scanner_detects_each_banned_pattern(
    tmp_path: Path, filename: str, content: str, label: str
) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")
    assert [v for v in find_violations(tmp_path) if v.endswith(label)]


def test_scanner_ignores_lookalikes(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("context(1)\nrender_text(2)\nsafe = 1\n", encoding="utf-8")
    assert find_violations(tmp_path) == []
