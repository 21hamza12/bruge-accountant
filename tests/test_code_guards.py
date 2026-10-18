"""Static guards: raw SQL and unescaped HTML must not creep into the application code.

All SQL goes through the ORM / SQL expression layer (bound parameters), and all HTML through
autoescaped Jinja. Migrations live outside ``app/`` and are exempt.

Python is checked on its AST, not with regexes, so spacing (``sa.text ('x')``), aliases
(``from sqlalchemy import text as t``) and attribute access (``sa.literal_column``) cannot slip
past. Limits, stated plainly: the guard cannot follow a SQL string built in one statement and
passed to ``execute`` through a variable, or code that imports dynamically (``getattr``,
``importlib``). Review those by eye; the first-argument rules below catch the usual shapes.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Names that build raw SQL or bypass HTML escaping, matched by the *imported* name so that
# ``import ... as alias`` does not hide them.
BANNED_NAMES = frozenset({"text", "TextClause", "literal_column", "Markup"})
# Attributes banned wherever they appear (``sa.literal_column``, ``markupsafe.Markup``).
BANNED_ATTRIBUTES = frozenset({"TextClause", "literal_column", "Markup", "exec_driver_sql"})
SQL_MODULES = ("sqlalchemy",)
TEMPLATE_BYPASS = {
    "|safe": re.compile(r"\|\s*safe\b"),
    "autoescape false": re.compile(r"autoescape\s+false\b", re.IGNORECASE),
}


def _module_root(name: str | None) -> str:
    return (name or "").split(".")[0]


def _sql_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to sqlalchemy itself or to anything imported from it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_root(alias.name) in SQL_MODULES:
                    names.add(alias.asname or _module_root(alias.name))
        elif isinstance(node, ast.ImportFrom) and _module_root(node.module) in SQL_MODULES:
            names.update(a.asname or a.name for a in node.names)
    return names


def _root_name(node: ast.expr) -> str | None:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _is_dynamic_sql(arg: ast.expr) -> bool:
    if isinstance(arg, ast.JoinedStr):  # f"... {x}"
        return True
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add | ast.Mod):  # "a" + b, "a" % b
        return True
    return (
        isinstance(arg, ast.Call)
        and isinstance(arg.func, ast.Attribute)
        and arg.func.attr == "format"
    )


def python_violations(source: str) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    sql_names = _sql_aliases(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in BANNED_NAMES:
                    found.append((node.lineno, f"import of {alias.name}"))
                if alias.name == "*" and _module_root(node.module) in {*SQL_MODULES, "markupsafe"}:
                    found.append((node.lineno, f"star import from {node.module}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] in BANNED_NAMES:
                    found.append((node.lineno, f"import of {alias.name}"))
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRIBUTES:
            found.append((node.lineno, f"use of .{node.attr}"))
        elif isinstance(node, ast.Call):
            func = node.func
            method = func.attr if isinstance(func, ast.Attribute) else None
            if method == "text" and _root_name(func) in sql_names:
                found.append((node.lineno, "call of text()"))
            if method == "execute" and node.args and _is_dynamic_sql(node.args[0]):
                found.append((node.lineno, "execute() with a built string"))
            for keyword in node.keywords:
                if (
                    keyword.arg == "autoescape"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                ):
                    found.append((node.lineno, "autoescape=False"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Template source embedded in Python (render_string and friends).
            for label, pattern in TEMPLATE_BYPASS.items():
                if pattern.search(node.value):
                    found.append((node.lineno, label))
    return found


def template_violations(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for label, pattern in TEMPLATE_BYPASS.items():
            if pattern.search(line):
                found.append((lineno, label))
    return found


def find_violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "migrations" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.suffix == ".py":
            hits = python_violations(path.read_text(encoding="utf-8"))
        elif "templates" in relative.parts[:-1]:
            # Every file under a templates directory, whatever its suffix.
            hits = template_violations(path.read_text(encoding="utf-8", errors="replace"))
        else:
            continue
        found.extend(f"{relative.as_posix()}:{lineno}: {label}" for lineno, label in hits)
    return found


def test_app_has_no_raw_sql_or_unescaped_html() -> None:
    assert find_violations(APP_DIR) == []


def test_scanner_is_not_vacuous() -> None:
    assert list(APP_DIR.rglob("*.py")), "scanner would see no files"
    assert list(APP_DIR.rglob("*.html")), "scanner would see no templates"
    assert (APP_DIR / "web" / "templates" / "base.html").is_file()


PY_BYPASSES = [
    # the shapes the original regex guard missed
    ("import sqlalchemy as sa\nsa.text ('select 1')\n", "text()"),
    ("import sqlalchemy as sa\nsa . text('select 1')\n", "text()"),
    ("from markupsafe import Markup as M\nM(x)\n", "import of Markup"),
    ("from sqlalchemy import text as t\n", "import of text"),
    ("from sqlalchemy import literal_column\n", "import of literal_column"),
    ("import sqlalchemy as sa\nsa.literal_column('x')\n", "use of .literal_column"),
    ("conn.execute(f'select * from t where a = {x}')\n", "execute() with a built string"),
    # the original shapes
    ("from sqlalchemy import text\nconn.execute(text('select 1'))\n", "import of text"),
    ("import sqlalchemy\nsqlalchemy.text('x')\n", "text()"),
    ("from sqlalchemy import sql\nsql.text('x')\n", "text()"),
    ("conn.exec_driver_sql('x')\n", "use of .exec_driver_sql"),
    ("from jinja2 import Markup\n", "import of Markup"),
    ("import markupsafe\nmarkupsafe.Markup(x)\n", "use of .Markup"),
    ("from sqlalchemy.sql import TextClause\n", "import of TextClause"),
    ("from sqlalchemy.sql.expression import TextClause as T\n", "import of TextClause"),
    ("from sqlalchemy import *\n", "star import from sqlalchemy"),
    # string building handed to execute
    ("conn.execute('select ' + col)\n", "execute() with a built string"),
    ("conn.execute('select %s' % col)\n", "execute() with a built string"),
    ("session.execute('select {}'.format(col))\n", "execute() with a built string"),
    # template bypasses hiding in Python
    ("from jinja2 import Environment\nEnvironment(autoescape=False)\n", "autoescape=False"),
    ("render_string('{{ x|safe }}')\n", "|safe"),
    ("render_string('{% autoescape false %}{{ x }}{% endautoescape %}')\n", "autoescape false"),
]


@pytest.mark.parametrize(("source", "label"), PY_BYPASSES)
def test_guard_catches_each_python_bypass(tmp_path: Path, source: str, label: str) -> None:
    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    violations = find_violations(tmp_path)
    assert any(v.endswith(label) for v in violations), violations


TEMPLATE_BYPASSES = [
    ("{{ name|safe }}\n", "|safe"),
    ("{{ name | safe }}\n", "|safe"),
    ("{{ name |   safe }}\n", "|safe"),
    ("{% set x = y|safe %}\n", "|safe"),
    ("{% autoescape false %}{{ x }}{% endautoescape %}\n", "autoescape false"),
    ("{%- autoescape   False -%}\n", "autoescape false"),
]


@pytest.mark.parametrize(
    "suffix", [".html", ".jinja", ".j2", ".jinja2", ".txt", ".xml", ".svg", ""]
)
@pytest.mark.parametrize(("content", "label"), TEMPLATE_BYPASSES)
def test_guard_scans_every_template_suffix(
    tmp_path: Path, suffix: str, content: str, label: str
) -> None:
    folder = tmp_path / "web" / "templates"
    folder.mkdir(parents=True)
    (folder / f"page{suffix}").write_text(content, encoding="utf-8")
    assert any(v.endswith(label) for v in find_violations(tmp_path))


def test_guard_ignores_migrations_and_pycache(tmp_path: Path) -> None:
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "a.py").write_text("import sqlalchemy as sa\nsa.text('x')\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "b.py").write_text("conn.exec_driver_sql('x')\n")
    assert find_violations(tmp_path) == []


@pytest.mark.parametrize(
    "source",
    [
        "context(1)\nrender_text(2)\nsafe = 1\n",
        "text = 'abc'\nprint(text)\n",
        "resp = get()\nbody = resp.text\n",
        "from sqlalchemy import select\nsession.execute(select(User).where(User.id == 1))\n",
        "session.execute(stmt, {'id': 1})\n",
        "Environment(autoescape=True)\n",
        "msg = 'a ' + 'b'\nlog.info('x %s' % msg)\n",
        "def safe(x): return x | 1\n",
    ],
)
def test_guard_ignores_lookalikes(tmp_path: Path, source: str) -> None:
    (tmp_path / "ok.py").write_text(source, encoding="utf-8")
    assert find_violations(tmp_path) == []


def test_guard_ignores_harmless_template_text(tmp_path: Path) -> None:
    folder = tmp_path / "templates"
    folder.mkdir()
    (folder / "ok.html").write_text(
        "{{ x|safely }} {{ y | e }} autoescape true {% autoescape true %}\n", encoding="utf-8"
    )
    assert find_violations(tmp_path) == []
