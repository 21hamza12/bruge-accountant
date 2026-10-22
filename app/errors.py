"""One base class for the mistakes a person can correct.

``UserError`` is raised by the services (partners, days, sales) with a French message that is safe
to show; ``str(error)`` is that message. A route catches ``UserError`` once instead of every
service's own class. Anything that is not a ``UserError`` is a bug or an outage, not a mistake.
"""

from __future__ import annotations


class UserError(Exception):
    """A mistake the user can correct; ``str(error)`` is a French message."""
