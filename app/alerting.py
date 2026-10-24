"""Error alerts: Sentry behind ``SENTRY_DSN``, with nothing personal in what leaves.

Off when the DSN is empty (development, tests, any job without it): no client is created and
nothing is sent. When on, only errors are sent: no traces, no profiles, no sessions, no log
records, no breadcrumbs, no local variables or source lines, no request bodies, cookies, headers
or query strings, no user, no host name. ``scrub_event`` is the last step before an event leaves
and masks anything the log redaction rules know (passwords, tokens, CSRF, API keys) plus phone
numbers and e-mails.
What stays is the exception type, its (scrubbed) message, the stack of file names and lines, the
route template, the release (git commit) and the environment.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.atexit import AtexitIntegration
from sentry_sdk.integrations.dedupe import DedupeIntegration
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Hint

from app.observability import REDACTED, SENSITIVE_KEY, scrub_text
from app.settings import Settings

log = logging.getLogger("app.alerting")

FLUSH_TIMEOUT_S = 5.0
MAX_DEPTH = 20
# Values under these keys are identifiers made of digits and letters, not text: leave them alone.
_ID_KEYS = frozenset(
    {"event_id", "release", "timestamp", "start_timestamp", "trace_id", "span_id", "parent_span_id"}
)
# SQLAlchemy and drivers append the statement and its parameters to the error text.
_SQL_TAIL = re.compile(r"\s*\[(?:SQL|parameters|parameters? of the statement)\b.*", re.DOTALL)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# An ISO date is kept; otherwise a run of digits with phone-like separators is a number to mask
# when it holds 8 digits or more (a phone number, a ticket number, an account number).
_NUMBER = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})|(?P<num>\+?\(?\d[\d ().\-  ]{6,}\d)")
MIN_NUMBER_DIGITS = 8
PHONE_MASK = "[NUMBER]"


def default_transport() -> Transport | None:
    """The transport to use; ``None`` means the SDK's own (HTTPS to Sentry). Tests replace it."""
    return None


def _mask_number(match: re.Match[str]) -> str:
    if match.group("date") is not None:
        return match.group(0)
    digits = sum(c.isdigit() for c in match.group("num"))
    return PHONE_MASK if digits >= MIN_NUMBER_DIGITS else match.group(0)


def scrub_string(text: str) -> str:
    """Mask credentials, API keys, e-mail addresses and phone-like numbers in free text."""
    text = scrub_text(text)
    text = _API_KEY.sub(REDACTED, text)
    text = _EMAIL.sub(REDACTED, text)
    return _NUMBER.sub(_mask_number, text)


def _walk(value: Any, key: str | None = None, depth: int = 0) -> Any:
    if key is not None and SENSITIVE_KEY.search(key):
        return REDACTED
    if depth > MAX_DEPTH:
        return "<truncated>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if key in _ID_KEYS else scrub_string(value)
    if isinstance(value, dict):
        return {str(k): _walk(v, str(k), depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_walk(v, None, depth + 1) for v in value]
    return f"<{type(value).__name__}>"


def _strip_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    return url.split("?", 1)[0].split("#", 1)[0]


def _cut_sql(event: dict[str, Any]) -> None:
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, dict) else None
    for item in values if isinstance(values, list) else []:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            item["value"] = _SQL_TAIL.sub("", item["value"])


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """``before_send``: the last pass over an event. Never raises (an error here would lose the
    alert); on an unexpected shape it keeps only the exception type."""
    try:
        data: dict[str, Any] = dict(event)
        _cut_sql(data)
        request = data.get("request")
        if isinstance(request, dict):
            kept: dict[str, Any] = {}
            if isinstance(request.get("method"), str):
                kept["method"] = request["method"]
            url = _strip_url(request.get("url"))
            if url:
                kept["url"] = url
            data["request"] = kept
        if "user" in data:
            data["user"] = {}
        data.pop("breadcrumbs", None)
        data.pop("server_name", None)
        scrubbed: Event = _walk(data)
        return scrubbed
    except Exception:  # pragma: no cover - defensive
        return {"message": "alert scrubbing failed", "level": "error"}


def init_alerting(settings: Settings, *, transport: Transport | None = None) -> bool:
    """Start the Sentry client when ``SENTRY_DSN`` is set; return whether it was started."""
    dsn = settings.sentry_dsn.get_secret_value().strip() if settings.sentry_dsn else ""
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        transport=transport or default_transport(),
        release=settings.version,
        environment=settings.env,
        send_default_pii=False,
        include_local_variables=False,
        include_source_context=False,  # file, function and line say enough with the release
        max_request_body_size="never",
        max_breadcrumbs=0,
        auto_session_tracking=False,
        traces_sample_rate=None,
        profiles_sample_rate=None,
        # Explicit list: the default set adds sys.argv (a --db URL), the module list and logging
        # breadcrumbs; the auto-enabled ones add SQL statements and HTTP calls.
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[
            StarletteIntegration(transaction_style="url"),
            FastApiIntegration(transaction_style="url"),
            DedupeIntegration(),
            ExcepthookIntegration(always_run=False),
            AtexitIntegration(callback=None),
        ],
        before_send=scrub_event,
        shutdown_timeout=int(FLUSH_TIMEOUT_S),
    )
    return True


def capture_exception(exc: BaseException, **tags: str) -> None:
    """Report a caught exception. A no-op without a client; the same exception is sent once."""
    if not sentry_sdk.get_client().is_active():
        return
    if tags:
        with sentry_sdk.new_scope() as scope:
            for name, value in tags.items():
                scope.set_tag(name, value)
            sentry_sdk.capture_exception(exc)
    else:
        sentry_sdk.capture_exception(exc)


def flush() -> None:
    """Wait (briefly) for queued events; a short-lived job must call this before it exits."""
    if sentry_sdk.get_client().is_active():
        sentry_sdk.flush(timeout=FLUSH_TIMEOUT_S)
