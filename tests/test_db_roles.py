"""APP_DB_ROLE validation: the role name ends up in DDL, which cannot take bind parameters."""

from __future__ import annotations

from typing import Any

import pytest

from app.db_roles import quote_role, validate_role_name


@pytest.mark.parametrize("name", ["bruge_app", "_x", "a", "app2", "a" * 63, "user", "select"])
def test_valid_names_are_returned_unchanged(name: str) -> None:
    assert validate_role_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "Bruge",
        "1app",
        "a" * 64,
        "bruge-app",
        "bruge app",
        'x"; DROP TABLE users; --',
        "x;",
        "app\n",
        "\napp",
        "app\x00",
        "appé",
        "app$",
        "pg_x.y",
    ],
)
def test_invalid_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="APP_DB_ROLE"):
        validate_role_name(name)


@pytest.mark.parametrize("bad", [None, 5, b"x", ["a"]])
def test_non_strings_are_refused(bad: Any) -> None:
    with pytest.raises(TypeError):
        validate_role_name(bad)


def test_the_error_does_not_echo_a_hostile_value() -> None:
    with pytest.raises(ValueError, match="APP_DB_ROLE") as info:
        validate_role_name('x"; DROP TABLE users; --')
    assert "DROP" not in str(info.value)


def test_quote_role_always_quotes_the_identifier() -> None:
    assert quote_role("bruge_app") == '"bruge_app"'
    assert quote_role("user") == '"user"'  # a reserved word is safe once quoted


def test_quote_role_validates_first() -> None:
    with pytest.raises(ValueError, match="APP_DB_ROLE"):
        quote_role('a"b')
