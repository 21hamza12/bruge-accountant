"""Command line interface.

``python -m app <command>`` with command one of: init-db, add-agency, bootstrap-agency,
reset-password, grant-app-role, purge-expired, import, seed-demo.

Exit codes: 0 success, 1 failure (database, file, parse), 2 usage or refusal (bad arguments, a
name already taken, a role that may not be used), 130 interrupted at the password prompt.
"""

import argparse
import dataclasses
import getpass
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, NoReturn

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from app import db_roles
from app.auth.passwords import hash_password, password_policy_error
from app.auth.phone import InvalidPhoneError, mask_phone, normalize_phone
from app.auth.ratelimit import purge_expired_windows
from app.auth.temp_passwords import TEMP_PASSWORD_TTL
from app.clock import utc_now
from app.db import default_database_url, make_engine, make_session_factory
from app.demo import (
    ALL_PHONES,
    OVERRIDE_FLAG,
    DemoCollisionError,
    demo_target_refusal,
    seed_demo,
)
from app.formatting import money
from app.ingest import IngestResult, check_bytes, ingest_report, sanitize_filename
from app.parsing.amadeus import MAX_FILE_BYTES, ParseError, parse_report
from app.questions import build_questions
from app.repository import (
    AlreadyExistsError,
    ResetRefusedError,
    bootstrap_agency,
    create_agency,
    get_agency_by_slug,
    purge_expired,
    reset_user_password,
    tickets_awaiting_price,
)
from app.settings import Settings, environment_is_production
from app.tenancy import bind_tenant

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130  # the shell convention for Ctrl-C

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_NAME_LENGTH = 200  # agencies.name is VARCHAR(200); the manager name is capped alike
SLUG_RULE = "slug must be lowercase letters, digits and hyphens (max 63)"


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


class _Parser(argparse.ArgumentParser):
    """argparse echoes the offending text ("unrecognized arguments: --pa hunter2"), which would
    put a password typed in the wrong place on the screen and in the terminal scrollback. So a
    command line error prints the usage and a generic message, never the tokens. Sub-parsers
    are built from the same class."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(
            EXIT_USAGE,
            f"error: invalid command line (exit code {EXIT_USAGE}). Run with --help for details.\n",
        )


def _build_parser() -> argparse.ArgumentParser:
    db_option = _Parser(add_help=False)
    db_option.add_argument(
        "--db",
        help=(
            "database URL (default: $DATABASE_URL, else sqlite:///bruge.db). Prefer "
            "DATABASE_URL: a URL typed here can contain a password and ends up in your "
            "shell history and in process listings"
        ),
    )

    parser = _Parser(prog="python -m app", description="Bruge Accountant")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", parents=[db_option], help="create or upgrade the database schema")

    add = sub.add_parser("add-agency", parents=[db_option], help="register an agency")
    add.add_argument("slug", help="short unique id, e.g. bruge-voyages")
    add.add_argument("name", help="display name")

    boot = sub.add_parser(
        "bootstrap-agency",
        parents=[db_option],
        allow_abbrev=False,
        help="create an agency and its first manager (needs the owner database connection)",
        description=(
            "Create an agency and its first manager in one transaction. The password is read "
            "from a prompt (asked twice) or, with --password-stdin, from the first line of "
            "standard input. It is never accepted as an argument. --password-stdin is the "
            "non-interactive path, and the only one that works from a pipe or a script on "
            "Windows: the prompt reads the console keyboard and ignores redirected input."
        ),
    )
    boot.add_argument("slug", help="short unique id, e.g. bruge-voyages")
    boot.add_argument("name", help="agency display name")
    boot.add_argument("--manager-phone", required=True, help="the manager's login phone number")
    boot.add_argument("--manager-name", required=True, help="the manager's display name")
    boot.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from the first line of standard input instead of prompting",
    )

    reset = sub.add_parser(
        "reset-password",
        parents=[db_option],
        allow_abbrev=False,
        help="issue a temporary password to any user, a manager included (owner connection)",
        description=(
            "Issue a temporary password to the active user with this phone number, whatever "
            "their role, revoke their sessions and audit it. This is how a locked-out manager "
            "gets back in: a manager can only reset staff from the web. The temporary password "
            "is printed ONCE on standard output (valid 30 minutes, usable once, the user must "
            "change it at login); it is never logged or stored in clear. Run it with the owner "
            'connection like bootstrap-agency: DATABASE_URL="$MIGRATION_DATABASE_URL".'
        ),
    )
    reset.add_argument("--phone", required=True, help="the user's login phone number")

    grant = sub.add_parser(
        "grant-app-role",
        parents=[db_option],
        help="(re)apply the app role's privileges (needs the owner database connection)",
        description=(
            "Grant the application role exactly its privileges (app.db_roles.GRANTS), e.g. for "
            "a role created after migration 0003 ran. Safe to repeat. Refuses a role that is a "
            "superuser or has BYPASSRLS."
        ),
    )
    grant.add_argument("role", help="the role name (lowercase letters, digits, underscore)")

    sub.add_parser(
        "purge-expired",
        parents=[db_option],
        help="delete expired sessions and temporary passwords and audit events past retention",
    )

    seed = sub.add_parser(
        "seed-demo",
        parents=[db_option],
        allow_abbrev=False,
        help="create two demo agencies with managers and staff (never in production)",
        description=(
            "Create the demo agencies demo-a and demo-b, each with a manager; demo-a also gets "
            "two staff linked to the signs 0004SV and 0009ER, and demo-b receives the fixture. "
            "One password for all four accounts is read from the first line of standard input "
            "(--password-stdin is required); it is never an argument and never printed. "
            "Everything is created in one transaction (all or nothing). Refuses when "
            "ENV=production, when any demo slug or phone is already taken (it names each one), "
            "and when the database is neither SQLite nor on this machine unless the override "
            "flag is given."
        ),
    )
    seed.add_argument(
        OVERRIDE_FLAG,
        action="store_true",
        dest="allow_remote",
        help="allow a database that is neither SQLite nor localhost (never ENV=production)",
    )
    seed.add_argument("--fixture", required=True, type=Path, help="an Amadeus report (.xlsx)")
    seed.add_argument(
        "--password-stdin",
        action="store_true",
        required=True,
        help="read the shared demo password from the first line of standard input",
    )

    imp = sub.add_parser("import", parents=[db_option], help="import an Amadeus daily report")
    imp.add_argument("file", type=Path, help="Amadeus report (.xlsx)")
    imp.add_argument("--agency", required=True, help="agency slug (must already exist)")
    imp.add_argument(
        "--date", type=_iso_date, help="override the report date (YYYY-MM-DD) for this import"
    )
    return parser


def _json_default(value: object) -> Any:
    if isinstance(value, Decimal):
        return money(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2))


def _is_unique_violation(exc: IntegrityError) -> bool:
    """Only a UNIQUE violation means "already exists". A CHECK, NOT NULL or foreign key failure
    is a bug or bad data, and calling it a duplicate would hide it."""
    orig = exc.orig
    if getattr(orig, "sqlstate", None) == "23505":  # psycopg: unique_violation
        return True
    return isinstance(orig, sqlite3.IntegrityError) and "UNIQUE constraint failed" in str(orig)


def _fail(message: str, code: int) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def _init_db(url: str) -> int:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    try:
        command.upgrade(cfg, "head")
    except CommandError as exc:
        return _fail(f"migration failed: {exc}", EXIT_FAILURE)
    print("database ready")
    return EXIT_OK


def _add_agency(url: str, slug: str, name: str) -> int:
    if not _SLUG_RE.match(slug):
        return _fail(SLUG_RULE, EXIT_USAGE)
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            if get_agency_by_slug(session, slug) is not None:
                return _fail(f"agency {slug!r} already exists", EXIT_USAGE)
            agency = create_agency(session, slug, name)
            _emit(
                {
                    "id": agency.id,
                    "slug": agency.slug,
                    "name": agency.name,
                    "currency": agency.currency,
                }
            )
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        return _fail(f"agency {slug!r} already exists", EXIT_USAGE)
    finally:
        engine.dispose()
    return EXIT_OK


class _PromptEnded(Exception):
    """The password prompt ended without a password (end of input, or Ctrl-C)."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _read_password(from_stdin: bool) -> str:
    """The password from stdin or from two hidden prompts; ValueError messages hold no secret."""
    if from_stdin:
        line = sys.stdin.readline()
        password = line.removesuffix("\n").removesuffix("\r")
        if not password:
            raise ValueError("no password received on standard input")
        return password
    try:
        password = getpass.getpass("Password: ")
        confirmation = getpass.getpass("Confirm password: ")
    except EOFError:
        raise _PromptEnded(
            "no password could be read (end of input). Without a terminal, e.g. in a script or "
            "on Windows from a pipe, use --password-stdin",
            EXIT_USAGE,
        ) from None
    except KeyboardInterrupt:
        raise _PromptEnded(
            "cancelled; nothing was created (--password-stdin skips the prompt)", EXIT_INTERRUPTED
        ) from None
    if confirmation != password:
        raise ValueError("the two passwords do not match")
    return password


def _operator() -> str:
    """Who is at the keyboard, for the audit trail (the OS user name; never a secret)."""
    try:
        return getpass.getuser()
    except (OSError, KeyError, ImportError):
        return "unknown"


def _bootstrap(url: str, args: argparse.Namespace) -> int:
    slug, name, manager = args.slug, args.name.strip(), args.manager_name.strip()
    if not _SLUG_RE.match(slug):
        return _fail(SLUG_RULE, EXIT_USAGE)
    if not name or len(name) > MAX_NAME_LENGTH:
        return _fail(f"agency name must be 1 to {MAX_NAME_LENGTH} characters", EXIT_USAGE)
    if not manager or len(manager) > MAX_NAME_LENGTH:
        return _fail(f"manager name must be 1 to {MAX_NAME_LENGTH} characters", EXIT_USAGE)
    try:
        phone = normalize_phone(args.manager_phone)
        password = _read_password(args.password_stdin)
    except _PromptEnded as exc:
        print(file=sys.stderr)  # the prompt left the cursor mid-line
        return _fail(str(exc), exc.code)
    except (InvalidPhoneError, ValueError) as exc:
        return _fail(str(exc), EXIT_USAGE)
    problem = password_policy_error(password, phone)
    if problem is not None:
        return _fail(problem, EXIT_USAGE)
    password_hash = hash_password(password)
    del password  # only the hash goes any further
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            created = bootstrap_agency(
                session, slug, name, phone, manager, password_hash, operator=_operator()
            )
    except AlreadyExistsError as exc:
        what = f"agency {slug!r}" if exc.what == "slug" else "a user with this phone number"
        return _fail(f"{what} already exists", EXIT_USAGE)
    except IntegrityError as exc:
        if not _is_unique_violation(exc):
            raise
        # Lost a race against a concurrent creation of the same slug or phone number.
        return _fail("the agency slug or the manager's phone number already exists", EXIT_USAGE)
    finally:
        engine.dispose()
    _emit({"agency_id": created.agency_id, "user_id": created.user_id})
    return EXIT_OK


def _reset_password(url: str, args: argparse.Namespace) -> int:
    try:
        phone = normalize_phone(args.phone)
    except InvalidPhoneError as exc:
        return _fail(str(exc), EXIT_USAGE)
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            result = reset_user_password(session, phone, utc_now(), operator=_operator())
    except ResetRefusedError as exc:
        if exc.reason == "inactive":
            return _fail("this account is deactivated; nothing was changed", EXIT_USAGE)
        return _fail("no user has this phone number; nothing was changed", EXIT_USAGE)
    finally:
        engine.dispose()
    print(
        "The temporary password below is shown ONCE. Give it to the user now; it is valid for "
        f"{int(TEMP_PASSWORD_TTL.total_seconds() // 60)} minutes and works once, and they must "
        "choose a new password at login. Their other sessions were revoked.",
        file=sys.stderr,
    )
    _emit(
        {
            "user_id": result.user_id,
            "agency_id": result.agency_id,
            "role": result.role,
            "phone": mask_phone(phone),
            "valid_minutes": int(TEMP_PASSWORD_TTL.total_seconds() // 60),
            "temporary_password": result.temporary_password,
        }
    )
    return EXIT_OK


def _grant_app_role(url: str, role: str) -> int:
    try:
        db_roles.validate_role_name(role)
    except ValueError as exc:
        return _fail(str(exc), EXIT_USAGE)
    engine = make_engine(url)
    try:
        if engine.dialect.name != "postgresql":
            return _fail("grants only exist on PostgreSQL; there is nothing to do here", EXIT_USAGE)
        try:
            with engine.begin() as connection:
                db_roles.apply_grants(connection, role)
        except RuntimeError as exc:  # includes PrivilegedRoleError
            return _fail(str(exc), EXIT_USAGE)
    finally:
        engine.dispose()
    _emit({"role": role, "tables": sorted(db_roles.GRANTS)})
    return EXIT_OK


def _purge_expired(url: str) -> int:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            counts = purge_expired(session)
            windows = purge_expired_windows(session, utc_now())
            session.commit()
    finally:
        engine.dispose()
    _emit(
        {
            **dataclasses.asdict(counts),
            "rate_limit_buckets": windows,
            "total": counts.total + windows,
        }
    )
    return EXIT_OK


def _seed_demo(url: str, args: argparse.Namespace) -> int:
    refusal = demo_target_refusal(url, allow_remote=args.allow_remote)
    if refusal is not None:
        return _fail(refusal, EXIT_USAGE)
    try:
        password = _read_password(True)
    except ValueError as exc:
        return _fail(str(exc), EXIT_USAGE)
    for phone in ALL_PHONES:
        problem = password_policy_error(password, phone)
        if problem is not None:
            return _fail(problem, EXIT_USAGE)
    fixture: Path = args.fixture
    try:
        with fixture.open("rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        check_bytes(data)
        parsed = parse_report(data)  # a bad fixture is refused before anything is written
    except OSError as exc:
        return _fail(f"cannot read {fixture}: {exc.strerror or exc}", EXIT_FAILURE)
    except ParseError as exc:
        return _fail(str(exc), EXIT_FAILURE)
    engine = make_engine(url)
    try:
        seeded = seed_demo(
            make_session_factory(engine),
            parsed,
            data,
            sanitize_filename(fixture.name),
            password,
            _operator(),
        )
    except DemoCollisionError as exc:
        return _fail(
            f"the demo accounts already exist ({', '.join(exc.fields)} already taken); "
            "nothing was changed",
            EXIT_USAGE,
        )
    finally:
        engine.dispose()
    _emit(
        {
            "demo-a": {
                "agency_id": seeded.agency_a,
                "manager_id": seeded.manager_a,
                "staff_ids": [seeded.staff_sv, seeded.staff_er],
            },
            "demo-b": {
                "agency_id": seeded.agency_b,
                "manager_id": seeded.manager_b,
                "upload_id": seeded.upload_b,
            },
        }
    )
    return EXIT_OK


def _password_argument_error(argv: Sequence[str]) -> str | None:
    """A password typed on the command line lands in shell history and process listings.

    argparse would echo the offending text back ("unrecognized arguments: --password x"), so
    anything that looks like a password option is refused here, before argparse sees it.
    """
    for token in argv:
        flag = token.split("=", 1)[0]
        long_form = flag.startswith("--pass") and flag != "--password-stdin"
        short_form = flag.startswith("-p") and not flag.startswith("--")  # -p x, -pX, -p=x
        if long_form or short_form:
            return (
                "the password is never accepted as an argument; use --password-stdin or the prompt"
            )
    return None


def _date_warning(result: IngestResult) -> str | None:
    """A sentence when some tickets of this import belong to a day other than ``report_date``."""
    if not result.dates_differ:
        return None
    days = ", ".join(f"{day.isoformat()} ({count})" for day, count in result.ticket_dates)
    return (
        f"the report date is {result.report_date.isoformat()}, but tickets that already existed "
        f"keep their original day: the tickets of this import belong to {days}"
    )


def _import(url: str, file: Path, slug: str, override: date | None) -> int:
    engine = make_engine(url)
    try:
        with make_session_factory(engine)() as session:
            agency = get_agency_by_slug(session, slug)
            if agency is None:
                return _fail(
                    f"unknown agency {slug!r}; create it first with: add-agency {slug} <name>",
                    EXIT_USAGE,
                )
            try:
                with file.open("rb") as fh:
                    data = fh.read(MAX_FILE_BYTES + 1)
            except OSError as exc:
                return _fail(f"cannot read {file}: {exc.strerror or exc}", EXIT_FAILURE)
            bind_tenant(session, agency.id)  # row-level security: everything below is this agency
            result = ingest_report(
                session, agency.id, data, file.name, report_date_override=override
            )
            report_date = result.report_date
            # Tickets that already existed keep the day they were first stored on, so the
            # questions are those of the days the tickets really belong to (normally just
            # ``report_date``).
            days = [day for day, _count in result.ticket_dates] or [report_date]
            awaiting = [
                ticket for day in days for ticket in tickets_awaiting_price(session, agency.id, day)
            ]
            staff = build_questions(awaiting)
            _emit(
                {
                    "agency": agency.slug,
                    "report_date": report_date,
                    "ticket_dates": {day.isoformat(): count for day, count in result.ticket_dates},
                    "date_warning": _date_warning(result),
                    "upload_id": result.upload_id,
                    "inserted": result.inserted,
                    "updated": result.updated,
                    "warnings": list(result.warnings),
                    "staff": [dataclasses.asdict(s) for s in staff],
                }
            )
    except ParseError as exc:
        return _fail(str(exc), EXIT_FAILURE)
    finally:
        engine.dispose()
    return EXIT_OK


def _database_failure(exc: SQLAlchemyError) -> int:
    if not isinstance(exc, DBAPIError):
        return _fail("invalid database URL or configuration", EXIT_FAILURE)
    detail = str(exc.orig)
    lowered = detail.lower()
    if "no such table" in lowered or "does not exist" in lowered:
        return _fail("database is not ready; run: init-db", EXIT_FAILURE)
    if "permission denied" in lowered:
        return _fail(
            f"database error: {detail}\nAdministrative commands (add-agency, bootstrap-agency) "
            "need the owner connection, not the web application's role: run them with "
            "DATABASE_URL set to the value of MIGRATION_DATABASE_URL.",
            EXIT_FAILURE,
        )
    return _fail(f"database error: {detail}", EXIT_FAILURE)


def _use_utf8_output() -> None:
    # Names may be Arabic/French; never crash on a legacy console codepage.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


OWNER_COMMANDS = frozenset({"init-db", "grant-app-role"})


def _default_url_for(command_name: str) -> str:
    """init-db and grant-app-role act as the owner role when MIGRATION_DATABASE_URL is set."""
    settings = Settings()
    if command_name in OWNER_COMMANDS and settings.migration_database_url is not None:
        owner_url = settings.migration_database_url.get_secret_value()
        if owner_url:
            return owner_url
    return settings.database_url.get_secret_value() or default_database_url()


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_output()
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if raw_argv[:1] == ["seed-demo"] and environment_is_production():
        return _fail("seed-demo never runs in production (ENV=production)", EXIT_USAGE)
    if raw_argv[:1] in (["bootstrap-agency"], ["seed-demo"]):
        refusal = _password_argument_error(raw_argv)
        if refusal is not None:
            return _fail(refusal, EXIT_USAGE)
    args = _build_parser().parse_args(argv)
    try:
        url: str = args.db or _default_url_for(args.command)
    except ValidationError as exc:
        # Messages only: the offending values (URLs with passwords) are never echoed.
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return _fail(f"invalid configuration: {problems}", EXIT_FAILURE)
    try:
        if args.command == "init-db":
            return _init_db(url)
        if args.command == "add-agency":
            return _add_agency(url, args.slug, args.name)
        if args.command == "bootstrap-agency":
            return _bootstrap(url, args)
        if args.command == "reset-password":
            return _reset_password(url, args)
        if args.command == "grant-app-role":
            return _grant_app_role(url, args.role)
        if args.command == "purge-expired":
            return _purge_expired(url)
        if args.command == "seed-demo":
            return _seed_demo(url, args)
        return _import(url, args.file, args.agency, args.date)
    except SQLAlchemyError as exc:
        return _database_failure(exc)
