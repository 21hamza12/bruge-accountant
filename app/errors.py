"""One base class for the mistakes a person can correct.

``UserError`` is raised by the services (partners, days, sales) with a French message that is safe
to show; ``str(error)`` is that message. A route catches ``UserError`` once instead of every
service's own class. Anything that is not a ``UserError`` is a bug or an outage, not a mistake.

``is_lock_conflict`` recognises Postgres refusing a transaction because of another one running at
the same time (a deadlock or a serialization failure): nothing is wrong with the request, so a
route answers ``RETRY_LATER`` instead of a 500.
"""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError

# A deadlock (40P01) or a serialization failure (40001).
LOCK_CONFLICT_SQLSTATES = frozenset({"40P01", "40001"})
RETRY_LATER = "Une autre opération était en cours au même moment : rien n'a été changé, réessayez."


class UserError(Exception):
    """A mistake the user can correct; ``str(error)`` is a French message."""


def is_lock_conflict(exc: BaseException) -> bool:
    """``exc`` is the database's deadlock or serialization refusal: a ``DBAPIError`` whose driver
    error carries SQLSTATE 40P01 or 40001."""
    if not isinstance(exc, DBAPIError):
        return False
    original = exc.orig
    code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return code in LOCK_CONFLICT_SQLSTATES
