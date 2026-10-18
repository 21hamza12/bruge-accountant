"""Validation and quoting of the application's database role name (``APP_DB_ROLE``).

Migration 0003 grants privileges to that role. DDL cannot take bind parameters, so the name is
checked against a strict pattern and then always double-quoted as an identifier. The pattern
forbids quotes and every other special character, so quoting can never be broken out of.
"""

from __future__ import annotations

import re

ROLE_ENV_VAR = "APP_DB_ROLE"
_ROLE_RE = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def validate_role_name(name: str) -> str:
    """Return ``name`` if it is a plain lowercase Postgres role name, else raise ValueError."""
    if not isinstance(name, str):
        raise TypeError("the role name must be a str")
    if _ROLE_RE.fullmatch(name) is None:
        # The value itself is not echoed: it may be hostile, or a secret typed in the wrong box.
        raise ValueError(
            f"{ROLE_ENV_VAR} must be 1-63 characters: lowercase letters, digits and underscores, "
            "not starting with a digit"
        )
    return name


def quote_role(name: str) -> str:
    """The role name as a double-quoted SQL identifier (validated first)."""
    return '"' + validate_role_name(name) + '"'
