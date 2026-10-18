"""Command line interface: ``python -m app <init-db|add-agency|bootstrap-agency|import>``."""

import argparse
import dataclasses
import getpass
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from app.auth.passwords import hash_password, password_policy_error
from app.auth.phone import InvalidPhoneError, normalize_phone
from app.db import default_database_url, make_engine, make_session_factory
from app.parsing.amadeus import MAX_FILE_BYTES, ParseError, parse_report
from app.questions import build_questions
from app.repository import (
    AlreadyExistsError,
    bootstrap_agency,
    create_agency,
    get_agency_by_slug,
    import_report,
    tickets_awaiting_price,
)
from app.settings import Settings
from app.tenancy import bind_tenant

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
MAX_NAME_LENGTH = 200  # agencies.name is VARCHAR(200); the manager name is capped alike
SLUG_RULE = "slug must be lowercase letters, digits and hyphens (max 63)"


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not a YYYY-MM-DD date") from exc


def _build_parser() -> argparse.ArgumentParser:
    db_option = argparse.ArgumentParser(add_help=False)
    db_option.add_argument(
        "--db",
        help=(
            "database URL (default: $DATABASE_URL, else sqlite:///bruge.db). Prefer "
            "DATABASE_URL: a URL typed here can contain a password and ends up in your "
            "shell history and in process listings"
        ),
    )

    parser = argparse.ArgumentParser(prog="python -m app", description="Bruge Accountant")
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
            "standard input. It is never accepted as an argument."
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

    imp = sub.add_parser("import", parents=[db_option], help="import an Amadeus daily report")
    imp.add_argument("file", type=Path, help="Amadeus report (.xlsx)")
    imp.add_argument("--agency", required=True, help="agency slug (must already exist)")
    imp.add_argument(
        "--date", type=_iso_date, help="override the report date (YYYY-MM-DD) for this import"
    )
    return parser


def _money(value: Decimal) -> str:
    """Stable text form: whole amounts without decimals ("746423"), otherwise 2 ("1234.50")."""
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.quantize(Decimal("0.01")), "f")


def _json_default(value: object) -> Any:
    if isinstance(value, Decimal):
        return _money(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2))


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
    except IntegrityError:
        return _fail(f"agency {slug!r} already exists", EXIT_USAGE)
    finally:
        engine.dispose()
    return EXIT_OK


def _read_password(from_stdin: bool) -> str:
    """The password from stdin or from two hidden prompts; ValueError messages hold no secret."""
    if from_stdin:
        line = sys.stdin.readline()
        password = line.removesuffix("\n").removesuffix("\r")
        if not password:
            raise ValueError("no password received on standard input")
        return password
    password = getpass.getpass("Password: ")
    if getpass.getpass("Confirm password: ") != password:
        raise ValueError("the two passwords do not match")
    return password


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
            created = bootstrap_agency(session, slug, name, phone, manager, password_hash)
    except AlreadyExistsError as exc:
        what = f"agency {slug!r}" if exc.what == "slug" else "a user with this phone number"
        return _fail(f"{what} already exists", EXIT_USAGE)
    except IntegrityError:
        # Lost a race against a concurrent creation of the same slug or phone number.
        return _fail("the agency slug or the manager's phone number already exists", EXIT_USAGE)
    finally:
        engine.dispose()
    _emit({"agency_id": created.agency_id, "user_id": created.user_id})
    return EXIT_OK


def _password_argument_error(argv: Sequence[str]) -> str | None:
    """A password typed on the command line lands in shell history and process listings.

    argparse would echo the offending text back ("unrecognized arguments: --password x"), so
    anything that looks like a password option is refused here, before argparse sees it.
    """
    for token in argv:
        flag = token.split("=", 1)[0]
        if flag.startswith("--pass") and flag != "--password-stdin":
            return (
                "the password is never accepted as an argument; use --password-stdin or the prompt"
            )
    return None


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
            parsed = parse_report(data)
            if override is not None:
                parsed = dataclasses.replace(
                    parsed, header=dataclasses.replace(parsed.header, report_date=override)
                )
            result = import_report(
                session, agency.id, parsed, file.name, hashlib.sha256(data).hexdigest()
            )
            report_date = parsed.header.report_date
            staff = build_questions(tickets_awaiting_price(session, agency.id, report_date))
            _emit(
                {
                    "agency": agency.slug,
                    "report_date": report_date,
                    "upload_id": result.upload_id,
                    "inserted": result.inserted,
                    "updated": result.updated,
                    "warnings": list(parsed.warnings),
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


def _default_url_for(command_name: str) -> str:
    """init-db migrates as the owner role when MIGRATION_DATABASE_URL is set."""
    settings = Settings()
    if command_name == "init-db" and settings.migration_database_url is not None:
        owner_url = settings.migration_database_url.get_secret_value()
        if owner_url:
            return owner_url
    return settings.database_url.get_secret_value() or default_database_url()


def main(argv: Sequence[str] | None = None) -> int:
    _use_utf8_output()
    raw_argv = sys.argv[1:] if argv is None else list(argv)
    if raw_argv[:1] == ["bootstrap-agency"]:
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
        return _import(url, args.file, args.agency, args.date)
    except SQLAlchemyError as exc:
        return _database_failure(exc)
