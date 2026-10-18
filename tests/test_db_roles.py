"""APP_DB_ROLE validation: the role name ends up in DDL, which cannot take bind parameters."""

from __future__ import annotations

from typing import Any

import pytest

from app.db_roles import (
    GRANT_EXEMPT,
    GRANTS,
    describe_privilege_problem,
    grant_statements,
    quote_role,
    revoke_statements,
    validate_role_name,
)


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


# --- the privileged-role guard and the grants map -----------------------------------------


def test_a_plain_role_has_no_privilege_problem() -> None:
    assert describe_privilege_problem("bruge_app", superuser=False, bypassrls=False) is None


@pytest.mark.parametrize(
    ("superuser", "bypassrls", "word"),
    [(True, False, "superuser"), (False, True, "BYPASSRLS"), (True, True, "superuser")],
)
def test_a_privileged_role_is_described(superuser: bool, bypassrls: bool, word: str) -> None:
    problem = describe_privilege_problem("bruge_app", superuser=superuser, bypassrls=bypassrls)
    assert problem is not None
    assert word in problem and "bruge_app" in problem and "row-level security" in problem


def test_every_table_is_in_the_grants_map_or_explicitly_exempt() -> None:
    """A future table without grants must fail CI: add it to GRANTS (or to GRANT_EXEMPT, with
    a reason) in app/db_roles.py."""
    import app.models  # noqa: F401 - registers the tables
    from app.db import Base

    tables = set(Base.metadata.tables) | {"alembic_version"}
    unlisted = tables - set(GRANTS) - GRANT_EXEMPT
    assert unlisted == set(), f"tables with no grants decision: {sorted(unlisted)}"
    assert set(GRANTS) | GRANT_EXEMPT <= tables, "stale entries in GRANTS / GRANT_EXEMPT"
    assert not set(GRANTS) & GRANT_EXEMPT


def test_the_grants_never_include_dangerous_privileges() -> None:
    for table, privileges in GRANTS.items():
        words = {p.strip() for p in privileges.split(",")}
        assert words <= {"SELECT", "INSERT", "UPDATE", "DELETE"}, table
    for table in ("tickets", "uploads", "users", "agencies"):
        assert "DELETE" not in GRANTS[table], table


def test_grant_statements_use_the_given_schema_and_quote_everything() -> None:
    statements = grant_statements("bruge_app", 'my"schema')
    joined = "\n".join(statements)
    assert '"my""schema"."tickets"' in joined  # quoted, with the embedded quote doubled
    assert 'TO "bruge_app"' in joined
    assert "public" not in joined.replace("FROM PUBLIC", "")  # no hard-coded schema name


def test_grant_statements_validate_the_role() -> None:
    with pytest.raises(ValueError, match="APP_DB_ROLE"):
        grant_statements('x"; DROP TABLE users; --', "public")


def test_revoke_statements_mirror_the_grants() -> None:
    joined = "\n".join(revoke_statements("bruge_app", "public"))
    assert 'FROM "bruge_app"' in joined
    assert "GRANT" not in joined
