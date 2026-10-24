"""Error alerts: Sentry behind ``SENTRY_DSN``, with nothing personal in what leaves.

Off when the DSN is empty (development, tests, any job without it): no client is created and
nothing is sent. When on, only errors are sent: no traces, no profiles, no sessions, no log
records, no breadcrumbs, no local variables or source lines, no request bodies, cookies, headers
or query strings, no user, no host name.

``scrub_event`` is the last step before an event leaves and works as an allow-list: it builds a
new event from the few fields that are kept (event id, time, level, release, environment, route
template, method, URL without query, the ``job`` tag, and per exception its type, a sanitised
value and the stack as file, function and line) and drops everything else. The value of an
exception is the risky part, because drivers and validators quote the data (``DETAIL: Key
(...)=(3, Jane Doe)``, ``input_value='Jane Doe'``). So a database or validation error is reduced to
its constraint name and SQLSTATE, or to its field locations and error types, and any other
message is cut at those markers and then scrubbed for credentials, API keys, e-mail addresses and
long numbers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import sentry_sdk
from pydantic_core import ValidationError as PydanticValidationError
from sentry_sdk.integrations.atexit import AtexitIntegration
from sentry_sdk.integrations.dedupe import DedupeIntegration
from sentry_sdk.integrations.excepthook import ExcepthookIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport
from sentry_sdk.types import Event, Hint
from sqlalchemy.exc import SQLAlchemyError

from app.observability import REDACTED, scrub_text
from app.settings import Settings

log = logging.getLogger("app.alerting")

FLUSH_TIMEOUT_S = 5.0
ALLOWED_TAGS = frozenset({"job"})
MAX_LINENO = 1_000_000
# Exception modules whose messages quote data: only a summary is sent for these.
_DATA_QUOTING_MODULES = (
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "sqlite3",
    "pydantic",
    "pydantic_core",
)
# HTTP exceptions carry free text (``detail``) written for the client.
_HTTP_MODULES = ("starlette.exceptions", "fastapi.exceptions")
# SQLAlchemy and drivers append the statement and its parameters to the error text; Postgres adds
# the offending values in DETAIL and the whole row in "Failing row contains"; pydantic quotes the
# input. Each is cut to the end of the text (or of the quoted part).
_SQL_TAIL = re.compile(r"\s*\[(?:SQL|parameters)\b.*", re.DOTALL)
_DETAIL = re.compile(r"\s*DETAIL:.*", re.DOTALL)
_FAILING_ROW = re.compile(r"\s*Failing row contains.*", re.DOTALL)
_INPUT_VALUE = re.compile(r"input_value=.*?(?=, input_type=|\]|$)", re.DOTALL)
_API_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{10,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# An ISO date is kept; otherwise a run of digits with phone-like separators is a number to mask
# when it holds 8 digits or more (a phone number, a ticket number, an account number).
_NUMBER = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})|(?P<num>\+?\(?\d[\d ().\-  ]{6,}\d)")
MIN_NUMBER_DIGITS = 8
PHONE_MASK = "[NUMBER]"
_SAFE_WORD = re.compile(r"[^\w.:-]")


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


def scrub_exception_text(text: str) -> str:
    """The message of an exception: statements, parameters, DETAIL, failing rows and quoted
    inputs are cut first, then the rest is scrubbed."""
    text = _SQL_TAIL.sub("", text)
    text = _DETAIL.sub("", text)
    text = _FAILING_ROW.sub("", text)
    text = _INPUT_VALUE.sub("input_value=" + REDACTED, text)
    return scrub_string(text)


def _word(value: object) -> str:
    return _SAFE_WORD.sub("", str(value))[:100]


def _db_summary(exc: BaseException) -> str:
    """Constraint name and SQLSTATE of a database error: identifiers, never data."""
    orig = getattr(exc, "orig", None) or exc
    parts: list[str] = []
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
    if isinstance(constraint, str) and constraint:
        parts.append(f"constraint={_word(constraint)}")
    sqlstate = getattr(orig, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        parts.append(f"sqlstate={_word(sqlstate)}")
    return " ".join(parts)


def _validation_summary(exc: PydanticValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return ", ".join(f"{_word('.'.join(map(str, e['loc'])))}: {_word(e['type'])}" for e in errors)


def _safe_value(exc: BaseException | None, type_name: str, module: str, value: object) -> str:
    """The text sent for one exception of the chain."""
    if isinstance(exc, PydanticValidationError):
        return _validation_summary(exc)
    if isinstance(exc, SQLAlchemyError) or (
        exc is not None and type(exc).__module__.split(".")[0] in _DATA_QUOTING_MODULES
    ):
        return _db_summary(exc)
    if exc is None and module.split(".")[0] in _DATA_QUOTING_MODULES:
        return ""  # no object to summarise (a hand-made or re-built event): send no text
    if module in _HTTP_MODULES or type_name == "HTTPException":
        return ""
    return scrub_exception_text(value) if isinstance(value, str) else ""


def _chain(exc: BaseException | None) -> list[BaseException]:
    """Root cause first, the order the SDK uses for ``exception.values``."""
    out: list[BaseException] = []
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        out.append(exc)
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return out[::-1]


def _frames(stacktrace: object) -> list[dict[str, Any]]:
    frames = stacktrace.get("frames") if isinstance(stacktrace, dict) else None
    out: list[dict[str, Any]] = []
    for frame in frames if isinstance(frames, list) else []:
        if not isinstance(frame, dict):
            continue
        kept: dict[str, Any] = {}
        for key in ("filename", "function", "module"):
            if isinstance(frame.get(key), str):
                kept[key] = scrub_string(frame[key])
        lineno = frame.get("lineno")
        if isinstance(lineno, int) and not isinstance(lineno, bool) and 0 < lineno <= MAX_LINENO:
            kept["lineno"] = lineno
        if isinstance(frame.get("in_app"), bool):
            kept["in_app"] = frame["in_app"]
        out.append(kept)
    return out


def _exceptions(event: dict[str, Any], hint: Hint) -> dict[str, Any] | None:
    block = event.get("exception")
    values = block.get("values") if isinstance(block, dict) else None
    if not isinstance(values, list):
        return None
    exc_info = hint.get("exc_info") if isinstance(hint, dict) else None
    top = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) == 3 else None
    chain = _chain(top) if isinstance(top, BaseException) else []
    mapped = chain if len(chain) == len(values) else [None] * len(values)
    out: list[dict[str, Any]] = []
    for item, exc in zip(values, mapped, strict=True):
        if not isinstance(item, dict):
            continue
        raw_type, raw_module = item.get("type"), item.get("module")
        type_name = raw_type if isinstance(raw_type, str) else "Exception"
        module = raw_module if isinstance(raw_module, str) else ""
        entry: dict[str, Any] = {
            "type": scrub_string(type_name),
            "value": _safe_value(exc, type_name, module, item.get("value")),
        }
        if module:
            entry["module"] = scrub_string(module)
        mechanism = item.get("mechanism")
        if isinstance(mechanism, dict):
            entry["mechanism"] = {
                key: mechanism[key]
                for key in ("type", "handled")
                if isinstance(mechanism.get(key), str | bool)
            }
        frames = _frames(item.get("stacktrace"))
        if frames:
            entry["stacktrace"] = {"frames": frames}
        out.append(entry)
    return {"values": out}


def _strip_url(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    return scrub_string(url.split("?", 1)[0].split("#", 1)[0])


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """``before_send``: build the event that leaves from the allow-list. Never raises (an error
    here would lose the alert); on an unexpected shape what is kept is simply less."""
    try:
        data: dict[str, Any] = dict(event)
        out: dict[str, Any] = {}
        for key in ("event_id", "timestamp", "release"):  # ids and times: digits, not text
            if isinstance(data.get(key), str):
                out[key] = data[key]
        for key in ("level", "platform", "environment", "transaction"):
            if isinstance(data.get(key), str):
                out[key] = scrub_string(data[key])
        request = data.get("request")
        if isinstance(request, dict):
            kept: dict[str, Any] = {}
            if isinstance(request.get("method"), str):
                kept["method"] = scrub_string(request["method"])
            url = _strip_url(request.get("url"))
            if url:
                kept["url"] = url
            out["request"] = kept
        tags = data.get("tags")
        if isinstance(tags, dict):
            safe_tags = {
                k: scrub_string(v)
                for k, v in tags.items()
                if k in ALLOWED_TAGS and isinstance(v, str)
            }
            if safe_tags:
                out["tags"] = safe_tags
        exceptions = _exceptions(data, hint)
        if exceptions is not None:
            out["exception"] = exceptions
        scrubbed: Event = out  # type: ignore[assignment]
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
            # No event for a handled HTTP error (a 503 "busy" with free text in its detail): the
            # request log has it. Only exceptions nobody handled are alerts.
            StarletteIntegration(transaction_style="url", failed_request_status_codes=set()),
            FastApiIntegration(transaction_style="url", failed_request_status_codes=set()),
            DedupeIntegration(),
            ExcepthookIntegration(always_run=False),
            AtexitIntegration(callback=None),
        ],
        before_send=scrub_event,
        shutdown_timeout=int(FLUSH_TIMEOUT_S),
    )
    return True


def capture_exception(exc: BaseException, **tags: str) -> None:
    """Report a caught exception. A no-op without a client; the same exception is sent once.
    Only the tags in ``ALLOWED_TAGS`` are accepted; any other is dropped."""
    if not sentry_sdk.get_client().is_active():
        return
    allowed = {name: value for name, value in tags.items() if name in ALLOWED_TAGS}
    if allowed:
        with sentry_sdk.new_scope() as scope:
            for name, value in allowed.items():
                scope.set_tag(name, value)
            sentry_sdk.capture_exception(exc)
    else:
        sentry_sdk.capture_exception(exc)


def flush() -> None:
    """Wait (briefly) for queued events; a short-lived job must call this before it exits."""
    if sentry_sdk.get_client().is_active():
        sentry_sdk.flush(timeout=FLUSH_TIMEOUT_S)
