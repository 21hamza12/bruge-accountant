"""The proposals of a multi-sale paste, carried from page to page so the agent never pastes twice.

A booking of several passengers gives several proposed sales (« Vente 1 », « Vente 2 », ...). The
agent opens one, confirms it, and comes back to the list of the same paste: the sales already
saved are « déjà saisie », the others keep their button. The pasted text (or the screenshot) is
NOT kept for that (docs/design/02-open-decisions.md, decision 3): what travels is the list of
proposed forms the server already sent to the browser, in ONE hidden field (``paste_batch``) of
the proposals page (once for the page: each « Saisir cette vente » button posts it with its
proposal's index to ``POST /sales/paste/open``, which re-derives that form from the verified list),
the sale form and the read-back, and nowhere else:

* never a URL or query string, never the database, never a log line, never a server-side store:
  the browser already holds every value of it in the proposals page, so carrying it adds nothing
  the agent does not already have, and it is gone when the tab is closed;
* **signed**, ``base64url(json) "." HMAC-SHA256(issue key, "paste-batch|agency|user|" + body)``
  with the server's issue-signing key (``Settings.issue_key()``): only a list this server made for
  this very user is shown again. A list that is tampered with, garbled, another user's, made on an
  earlier (UTC) day, older than ``MAX_AGE`` or larger than ``MAX_TOKEN_BYTES`` is dropped
  (``decode`` returns None; a list too large to make is not made, and each proposal is then
  posted on its own, as before the list existed). It is
  integrity only, not secrecy: the values are the agent's own page;
* never believed: a proposal opened from it is read with ``sale_forms.read`` like any posted form
  (issue dates re-verified for staff with ``app/issue_sig.py``) and validated again by the service
  when confirmed; « déjà saisie » is decided by the server from the ticket numbers stored in an
  active sale (``sales.active_ticket_lookup``), never by a flag of the page.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from starlette.datastructures import FormData, UploadFile

from app.issue_sig import IssueSigner
from app.web import sale_forms
from app.web.sale_forms import SaleForm

FIELD = sale_forms.BATCH_FIELD
MAX_PROPOSALS = 30  # far more passengers than one 16 KB booking can hold
MAX_TOKEN_BYTES = sale_forms.MAX_BATCH_CHARS
MAX_PAIRS = 400  # per proposal: 20 tickets of 9 fields, the sale fields and the notices
MAX_AGE = timedelta(hours=8)  # a working day; after that the agent pastes again
_VERSION = 1


@dataclass(frozen=True)
class Batch:
    """The proposals of one paste, as the server made them, and the token that carries them."""

    token: str
    forms: list[SaleForm]
    reader_warnings: list[str]


def _mac(key: bytes, agency_id: int, user_id: int, body: str) -> str:
    message = f"paste-batch|{agency_id}|{user_id}|{body}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def encode(
    key: bytes,
    agency_id: int,
    user_id: int,
    now: datetime,
    forms: list[SaleForm],
    reader_warnings: list[str],
) -> str:
    """The signed token of these proposals, or "" when there is nothing to carry (one proposal,
    or a list too large to carry: the agent then pastes again, as before)."""
    if len(forms) < 2 or len(forms) > MAX_PROPOSALS:
        return ""
    payload = {
        "v": _VERSION,
        "t": int(now.timestamp()),
        "w": [w[: sale_forms.HIDDEN_CLIP] for w in reader_warnings][: sale_forms.MAX_NOTICES],
        "p": [[[name, value] for name, value in form.hidden_pairs] for form in forms],
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    token = f"{body}.{_mac(key, agency_id, user_id, body)}"
    return token if len(token) <= MAX_TOKEN_BYTES else ""


def _pairs(raw: object) -> list[tuple[str, str]] | None:
    if not isinstance(raw, list) or len(raw) > MAX_PAIRS:
        return None
    found: list[tuple[str, str]] = []
    for item in raw:
        if not (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], str)
        ):
            return None
        if item[0] == FIELD:  # a list never carries a list
            return None
        found.append((item[0], item[1]))
    return found


def decode(
    key: bytes,
    agency_id: int,
    user_id: int,
    now: datetime,
    token: object,
    signer: IssueSigner | None,
) -> Batch | None:
    """The proposals of a token this server made for this user, read back as posted forms (with
    ``signer``, staff's issue dates are verified again); None for anything else."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES:
        return None
    body, dot, mac = token.partition(".")
    if not dot or not body:
        return None
    expected = _mac(key, agency_id, user_id, body)
    if not hmac.compare_digest(expected.encode(), mac.strip().lower().encode()):
        return None
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(raw.decode())
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("v") != _VERSION:
        return None
    stamp, warnings, proposals = payload.get("t"), payload.get("w"), payload.get("p")
    if not isinstance(stamp, int) or not isinstance(warnings, list):
        return None
    try:
        made = datetime.fromtimestamp(stamp, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    age = now - made
    if age > MAX_AGE or age < -timedelta(minutes=5):
        return None
    if made.date() < now.astimezone(UTC).date():
        return None  # made on an earlier day: its "today" (the old tickets dropped) is stale
    if not isinstance(proposals, list) or not 2 <= len(proposals) <= MAX_PROPOSALS:
        return None
    forms: list[SaleForm] = []
    for item in proposals:
        pairs = _pairs(item)
        if pairs is None:
            return None
        fields: list[tuple[str, str | UploadFile]] = list(pairs)
        form = sale_forms.read(FormData(fields), signer=signer)
        if not form.from_paste:
            return None
        form.batch = token
        forms.append(form)
    texts = [str(w)[: sale_forms.HIDDEN_CLIP] for w in warnings if isinstance(w, str)]
    return Batch(token, forms, texts[: sale_forms.MAX_NOTICES])
