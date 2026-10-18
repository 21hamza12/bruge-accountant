"""Language and text-direction helpers. Source strings are French; Arabic (RTL) comes later."""

from __future__ import annotations

from jinja2 import Environment

DEFAULT_LANGUAGE = "fr"
SUPPORTED_LANGUAGES = ("fr", "ar")
RTL_LANGUAGES = frozenset({"ar"})


def normalize_language(value: str | None) -> str:
    """Map a user-supplied tag such as ``ar-MR`` to a supported language, else the default."""
    primary = (value or "").lower().split("-")[0]
    return primary if primary in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def text_direction(lang: str) -> str:
    return "rtl" if lang.lower().split("-")[0] in RTL_LANGUAGES else "ltr"


def install_null_translations(env: Environment) -> None:
    """``_()`` returns the French source string until real gettext catalogs exist."""
    env.install_null_translations(newstyle=True)  # type: ignore[attr-defined]
