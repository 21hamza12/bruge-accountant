"""Jinja2 environment, built explicitly with autoescape on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from app.i18n import (
    DEFAULT_LANGUAGE,
    install_null_translations,
    normalize_language,
    text_direction,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def build_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        extensions=["jinja2.ext.i18n"],
    )
    install_null_translations(env)
    return env


ENV = build_environment()


def render(template: str, lang: str = DEFAULT_LANGUAGE, **context: Any) -> str:
    lang = normalize_language(lang)
    return ENV.get_template(template).render(lang=lang, dir=text_direction(lang), **context)


def render_string(source: str, **context: Any) -> str:
    return ENV.from_string(source).render(**context)
