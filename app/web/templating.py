"""Jinja2 environment, built explicitly with autoescape on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        extensions=["jinja2.ext.i18n"],
    )
    # Source strings are French; Arabic arrives later through real gettext catalogs.
    env.install_null_translations(newstyle=True)  # type: ignore[attr-defined]
    return env


ENV = build_environment()


def render(template: str, **context: Any) -> str:
    return ENV.get_template(template).render(**context)


def render_string(source: str, **context: Any) -> str:
    return ENV.from_string(source).render(**context)
