"""Signed issue dates: what binds a ticket's issue date to what the server itself read.

The day a sale is filed under is its tickets' issue date, which travels through the form in a
hidden field. A staff member could post any earlier open day there, so the app signs every date it
proposes itself (the paste reader, the screenshot reader, the "not sent" flag form):

    issue_sig = HMAC-SHA256(key, "agency_id|user_id|doc_number|issue_date_iso")

carried in a hidden ``issue_sig`` field beside the date through the read-back and the confirm. For
staff a date counts only when its signature verifies (constant-time compare); otherwise the date
is ignored, as if the ticket had none (the sale goes to today). It is never an error and nothing is
logged. A manager or accountant choose their day freely and need no signature.

The key is ``Settings.issue_key()``: the ``ISSUE_SIGNING_KEY`` secret of the server when set, else
a random key made at start-up. A per-session key derived from the session's CSRF token was
rejected: that token is in the page the staff member holds, so they could compute the signature
themselves. With the random fallback a restart (or a second instance) only makes older forms lose
their dates (the sale then goes to today), never accept a forged one.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import date

from app.models import User
from app.settings import Settings


def doc_key(raw: str) -> str:
    """The ticket number as signed: spaces, "-" and "/" dropped; the last ten digits of a
    13-digit number (airline code first), as ``sales_rules.ticket_number`` stores it."""
    compact = "".join(raw.replace("-", "").replace("/", "").split())
    return compact[-10:] if len(compact) == 13 else compact


class IssueSigner:
    """Signs and verifies the issue dates of one user's forms (their agency and id are signed)."""

    def __init__(self, key: bytes, agency_id: int, user_id: int) -> None:
        self._key = key
        self._agency_id = agency_id
        self._user_id = user_id

    def _mac(self, doc: str, day: date) -> str:
        message = f"{self._agency_id}|{self._user_id}|{doc_key(doc)}|{day.isoformat()}"
        return hmac.new(self._key, message.encode(), hashlib.sha256).hexdigest()

    def sign(self, doc: str, day: date) -> str:
        return self._mac(doc, day)

    def verify(self, doc: str, day: date, signature: str) -> bool:
        return hmac.compare_digest(self._mac(doc, day).encode(), signature.strip().lower().encode())


def signer_for(settings: Settings, user: User) -> IssueSigner:
    return IssueSigner(settings.issue_key(), user.agency_id, user.id)
