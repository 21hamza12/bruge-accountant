from __future__ import annotations

import re
from pathlib import Path

import pytest
from babel.messages.frontend import CommandLineInterface

from app.i18n import DEFAULT_LANGUAGE, normalize_language, text_direction
from app.web.templating import render

ROOT = Path(__file__).resolve().parent.parent


def test_default_language_is_french() -> None:
    assert DEFAULT_LANGUAGE == "fr"
    html = render("errors/404.html", ref="ABCD1234")
    assert '<html lang="fr" dir="ltr">' in html


def test_arabic_renders_right_to_left() -> None:
    html = render("errors/404.html", ref="ABCD1234", lang="ar")
    assert '<html lang="ar" dir="rtl">' in html


@pytest.mark.parametrize(
    ("lang", "direction"), [("fr", "ltr"), ("ar", "rtl"), ("en", "ltr"), ("AR", "rtl")]
)
def test_text_direction(lang: str, direction: str) -> None:
    assert text_direction(lang) == direction


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, "fr"),
        ("", "fr"),
        ("xx", "fr"),
        ("ar", "ar"),
        ("AR", "ar"),
        ("fr-FR", "fr"),
        ("ar-MR", "ar"),
    ],
)
def test_normalize_language(given: str | None, expected: str) -> None:
    assert normalize_language(given) == expected


def test_babel_extracts_the_french_source_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (ROOT / "babel.cfg").is_file()
    monkeypatch.chdir(ROOT)
    out = tmp_path / "messages.pot"
    CommandLineInterface().run(  # type: ignore[no-untyped-call]
        ["pybabel", "extract", "-F", "babel.cfg", "-o", str(out), "app"]
    )
    pot = out.read_text(encoding="utf-8")
    for msgid in ("Page introuvable", "Vous êtes hors ligne", "Référence"):
        assert re.search(rf'msgid "{msgid}"', pot), msgid
