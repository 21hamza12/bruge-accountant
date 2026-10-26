"""Database access. Every query is scoped by ``agency_id``."""

import logging
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    CursorResult,
    String,
    column,
    delete,
    func,
    insert,
    or_,
    select,
    table,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.auth.temp_passwords import issue_temporary_password
from app.models import (
    PRICE_STATUS_AWAITING,
    ROLE_MANAGER,
    Agency,
    AmadeusLine,
    AuditEvent,
    StaffSignLink,
    TemporaryPassword,
    Ticket,
    Upload,
    User,
    UserSession,
)
from app.parsing.amadeus import ParsedReport, ParsedTicket
from app.tenancy import (
    TenantBindingError,
    bind_tenant,
    bound_tenant,
    lookup_scope,
    rebind_tenant,
    restore_binding,
)

log = logging.getLogger("app.repository")
UPSERT_CHUNK = 200
PURGE_CHUNK = 10_000  # rows per DELETE statement where the statement can be limited (see below)

# Amadeus-owned columns refreshed on re-import. Deliberately absent: price_status (staff
# owned), first_upload_id and created_at (history), and report_date (a ticket belongs to the
# day it first appeared; a later report never moves it to another day).
_REFRESHED_COLUMNS = (
    "seq_no",
    "airline",
    "cost",
    "passenger_name",
    "pax_type",
    "staff_sign",
    "transaction_type",
    "booking_ref",
    "flags",
    "last_upload_id",
    "updated_at",
)


@dataclass(frozen=True)
class ImportResult:
    upload_id: int
    inserted: int
    updated: int


def _next_agency_id(session: Session) -> int | None:
    """Postgres: the next id from the sequence; SQLite: None (the insert picks the id).

    Under row-level security a new agency row is only accepted when its id equals the tenant
    setting, and ``INSERT .. RETURNING`` needs the row to be readable. So the id is taken first,
    the tenant is bound to it, and only then is the row written.
    """
    if session.get_bind().dialect.name != "postgresql":
        return None
    sequence = func.pg_get_serial_sequence("agencies", "id")
    return int(session.scalar(select(func.nextval(sequence))) or 0) or None


def create_agency(session: Session, slug: str, name: str, currency: str = "MRU") -> Agency:
    """Insert an agency and leave the session bound to it. Works under FORCEd row-level security."""
    previous = bound_tenant(session)
    agency_id = _next_agency_id(session)
    try:
        if agency_id is not None:
            rebind_tenant(session, agency_id)  # before the INSERT: its WITH CHECK compares the id
        agency = Agency(slug=slug, name=name, currency=currency)
        if agency_id is not None:
            agency.id = agency_id
        session.add(agency)
        session.commit()
    except Exception:
        _abandon(session, previous)
        raise
    rebind_tenant(session, agency.id)
    return agency


def _abandon(session: Session, previous: int | None) -> None:
    """After a failure: restore the previous binding first (it cannot fail), then roll back.
    A rollback that fails too must not replace the error being handled, so it is only logged."""
    restore_binding(session, previous)
    try:
        session.rollback()
    except Exception:
        log.warning("rollback failed while handling an earlier error", exc_info=True)


class AlreadyExistsError(Exception):
    """The agency slug or the manager's phone is taken. ``what`` is ``"slug"`` or ``"phone"``."""

    def __init__(self, what: str) -> None:
        super().__init__(f"the {what} is already taken")
        self.what = what


@dataclass(frozen=True)
class BootstrapResult:
    agency_id: int
    user_id: int


def add_agency_with_manager(
    session: Session,
    slug: str,
    name: str,
    phone: str,
    display_name: str,
    password_hash: str,
    operator: str | None = None,
) -> BootstrapResult:
    """Add an agency, its first manager and the ``agency_bootstrapped`` audit event to the
    CALLER's open transaction (nothing is committed), and leave the session bound to the new
    agency. ``bootstrap_agency`` wraps this in its own transaction; ``seed-demo`` calls it twice
    inside one, so it is all or nothing. On error the caller rolls back and restores the binding.

    ``phone`` is E.164 and ``password_hash`` an Argon2 hash; validating and hashing is the
    caller's job. Works under FORCEd row-level security: both lookups use the narrow policies, the
    id is taken from the sequence and bound as the tenant before the first INSERT.
    """
    if get_agency_by_slug(session, slug) is not None:
        raise AlreadyExistsError("slug")
    lookup_scope(session, "app.login_phone", phone)
    if session.scalars(select(User.id).where(User.phone == phone)).first() is not None:
        raise AlreadyExistsError("phone")
    agency_id = _next_agency_id(session)
    if agency_id is not None:
        rebind_tenant(session, agency_id)
    agency = Agency(slug=slug, name=name)
    if agency_id is not None:
        agency.id = agency_id
    session.add(agency)
    session.flush()
    rebind_tenant(session, agency.id)
    user = User(
        agency_id=agency.id,
        phone=phone,
        role=ROLE_MANAGER,
        display_name=display_name,
        password_hash=password_hash,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    session.add(
        AuditEvent(
            agency_id=agency.id,
            actor_user_id=None,
            event_type="agency_bootstrapped",
            details={
                "agency_slug": slug,
                "manager_user_id": user.id,
                "operator": operator or "unknown",
            },
        )
    )
    session.flush()
    return BootstrapResult(agency_id=agency.id, user_id=user.id)


def bootstrap_agency(
    session: Session,
    slug: str,
    name: str,
    phone: str,
    display_name: str,
    password_hash: str,
    operator: str | None = None,
) -> BootstrapResult:
    """Create an agency and its first manager in ONE transaction (all or nothing).

    The manager does not have to change the password (the operator chose it).

    An ``agency_bootstrapped`` audit event is written in the same transaction: the agency, the
    new manager's id and the ``operator`` (the OS user who ran the command). Nobody is logged in
    at that point, so there is no actor user; the event holds no password, hash or phone number.
    """
    previous = bound_tenant(session)
    try:
        result = add_agency_with_manager(
            session, slug, name, phone, display_name, password_hash, operator
        )
        session.commit()
    except Exception:
        _abandon(session, previous)
        raise
    return result


class ResetRefusedError(Exception):
    """No password can be reset: ``reason`` is ``"unknown"`` (no such phone) or ``"inactive"``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResetResult:
    user_id: int
    agency_id: int
    role: str
    # Shown once by the command that asked for it; never part of a repr, a log or the database.
    temporary_password: str = field(repr=False)


def reset_user_password(
    session: Session, phone: str, now: datetime, operator: str | None = None
) -> ResetResult:
    """Issue a temporary password to ANY active user (a manager included) and revoke their
    sessions. For the platform owner's ``reset-password`` command; the web only lets a manager
    reset staff.

    ``phone`` is E.164. The user is found with the narrow phone lookup, then the tenant is bound
    and the row locked ``FOR UPDATE``. The ``temp_password_issued`` audit event has no actor (it
    was the owner, not a logged-in user) and names the ``operator`` (the OS user who ran it).
    Commits; raises :class:`ResetRefusedError` and leaves nothing behind otherwise.
    """
    previous = bound_tenant(session)
    try:
        lookup_scope(session, "app.login_phone", phone)
        found = session.scalars(select(User).where(User.phone == phone)).one_or_none()
        if found is None:
            raise ResetRefusedError("unknown")
        rebind_tenant(session, found.agency_id)
        user = session.scalars(
            select(User)
            .where(User.agency_id == found.agency_id, User.id == found.id)
            .with_for_update()
        ).one()
        if not user.is_active:
            raise ResetRefusedError("inactive")
        plaintext = issue_temporary_password(
            session,
            user,
            now,
            actor_user_id=None,
            audit_details={"via": "cli", "operator": operator or "unknown"},
        )
        result = ResetResult(user.id, user.agency_id, user.role, plaintext)
        session.commit()
    except Exception:
        _abandon(session, previous)
        raise
    return result


def get_agency_by_slug(session: Session, slug: str) -> Agency | None:
    """Find one agency by its exact slug, before any tenant is bound (narrow lookup policy)."""
    lookup_scope(session, "app.agency_slug", slug)
    return session.scalars(select(Agency).where(Agency.slug == slug)).one_or_none()


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _upsert_statement(dialect_name: str, rows: list[dict[str, Any]]) -> Any:
    """One atomic INSERT .. ON CONFLICT DO UPDATE for a chunk of rows."""
    if dialect_name == "postgresql":
        stmt: Any = postgresql.insert(Ticket).values(rows)
    elif dialect_name == "sqlite":
        stmt = sqlite.insert(Ticket).values(rows)
    else:
        raise NotImplementedError(f"upsert is not implemented for dialect {dialect_name!r}")
    return stmt.on_conflict_do_update(
        index_elements=["agency_id", "ticket_no"],
        set_={name: getattr(stmt.excluded, name) for name in _REFRESHED_COLUMNS},
    )


def _upsert_chunk(session: Session, rows: list[dict[str, Any]]) -> None:
    session.execute(_upsert_statement(session.get_bind().dialect.name, rows))


def _row(
    parsed: ParsedTicket, agency_id: int, report_date: date, upload_id: int, now: datetime
) -> dict[str, Any]:
    return {
        "agency_id": agency_id,
        "ticket_no": parsed.ticket_no,
        "first_upload_id": upload_id,
        "last_upload_id": upload_id,
        "report_date": report_date,
        "seq_no": parsed.seq_no,
        "airline": parsed.airline,
        "cost": parsed.cost,
        "passenger_name": parsed.passenger_name,
        "pax_type": parsed.pax_type,
        "staff_sign": parsed.staff_sign,
        "transaction_type": parsed.transaction_type,
        "booking_ref": parsed.booking_ref,
        "flags": list(parsed.flags),
        "price_status": PRICE_STATUS_AWAITING,
        "created_at": now,
        "updated_at": now,
    }


def _count_existing(session: Session, agency_id: int, ticket_nos: Sequence[str]) -> int:
    stmt = (
        select(func.count())
        .select_from(Ticket)
        .where(Ticket.agency_id == agency_id, Ticket.ticket_no.in_(ticket_nos))
    )
    return session.scalar(stmt) or 0


def _store_amadeus_lines(
    session: Session, agency_id: int, upload_id: int, tickets: Sequence[ParsedTicket]
) -> None:
    """Append one ``amadeus_lines`` row per parsed line, in file order (``seq_order`` 1..n).

    Precondition: no two lines share a DOC NUMBER (the parser keeps the first); the table's
    UNIQUE (agency_id, upload_id, doc_number) would refuse the whole upload otherwise."""
    if len({t.ticket_no for t in tickets}) != len(tickets):
        raise ValueError("duplicate DOC NUMBER in the parsed lines")
    for chunk_start in range(0, len(tickets), UPSERT_CHUNK):
        chunk = tickets[chunk_start : chunk_start + UPSERT_CHUNK]
        session.execute(
            insert(AmadeusLine),
            [
                {
                    "agency_id": agency_id,
                    "upload_id": upload_id,
                    "seq_no": t.seq_no,
                    "seq_order": chunk_start + offset,
                    "doc_number": t.ticket_no,
                    "airline": t.airline,
                    "total_doc": t.cost,
                    "tax": t.tax,
                    "comm": t.comm,
                    "pax_name": t.passenger_name,
                    "pax_type": t.pax_type,
                    "staff_sign": t.staff_sign,
                    "rloc": t.booking_ref,
                    "trnc": t.transaction_type,
                    "form_of_payment": t.form_of_payment,
                    "confirmed": t.confirmed,
                    "flags": list(t.flags),
                    "source_row": t.source_row,
                }
                for offset, t in enumerate(chunk, start=1)
            ],
        )


def _identical_upload_with_lines(
    session: Session, agency_id: int, sha256: str, report_date: date, *, before: int | None = None
) -> int | None:
    """The earliest upload of this agency with the same bytes and report date that has stored
    lines (and, with ``before``, an id below it), or None."""
    stmt = (
        select(Upload.id)
        .where(
            Upload.agency_id == agency_id,
            Upload.sha256 == sha256,
            Upload.report_date == report_date,
            select(AmadeusLine.id)
            .where(
                AmadeusLine.agency_id == Upload.agency_id,
                AmadeusLine.upload_id == Upload.id,
            )
            .exists(),
        )
        .order_by(Upload.id)
        .limit(1)
    )
    if before is not None:
        stmt = stmt.where(Upload.id < before)
    return session.scalar(stmt)


def lines_upload_for(session: Session, agency_id: int, upload_id: int) -> int | None:
    """The upload whose ``amadeus_lines`` are the original report of ``upload_id``.

    An upload stores its own lines unless an earlier upload of the same agency had identical
    bytes and the same report date: then it stores none (a second identical set would only
    repeat the first) and this returns that earlier upload. None when no lines exist at all
    (an upload from before migration 0006)."""
    bind_tenant(session, agency_id)
    own = session.scalar(
        select(AmadeusLine.id)
        .where(AmadeusLine.agency_id == agency_id, AmadeusLine.upload_id == upload_id)
        .limit(1)
    )
    if own is not None:
        return upload_id
    upload = session.scalar(
        select(Upload).where(Upload.agency_id == agency_id, Upload.id == upload_id)
    )
    if upload is None:
        return None
    return _identical_upload_with_lines(
        session, agency_id, upload.sha256, upload.report_date, before=upload_id
    )


def import_report(
    session: Session,
    agency_id: int,
    parsed: ParsedReport,
    filename: str,
    sha256: str,
    uploaded_by_user_id: int | None = None,
    *,
    commit: bool = True,
) -> ImportResult:
    """Store an upload and upsert its tickets in a single transaction.

    ``commit=False`` leaves the transaction open for a caller that does more in the same
    transaction (``seed-demo``); a failure still rolls the whole session back.

    The session is bound to ``agency_id`` first (row-level security); one that is already bound
    to a different agency is refused (``TenantBindingError``), never re-pointed. The commit at
    the end ends the transaction and its tenant setting; the session's ``after_begin`` listener
    applies the binding again to whatever the caller reads next.

    Lines: one ``amadeus_lines`` row per parsed line, unless the same bytes were already stored
    for the same report date (see ``lines_upload_for``).

    Each chunk is one atomic upsert, so concurrent imports of the same tickets cannot
    collide; rows go in ticket-number order so concurrent writers lock in the same order.
    ``inserted``/``updated`` come from a count taken in the same transaction (exact unless
    another import commits the same tickets in between).
    """
    # Raises, and leaves the session untouched, if it is bound to another agency.
    bind_tenant(session, agency_id)
    try:
        if session.get(Agency, agency_id) is None:
            raise LookupError(f"unknown agency id {agency_id}")

        header = parsed.header
        # An identical re-upload (same bytes, same day) keeps the first line set: no second copy.
        already_stored = (
            _identical_upload_with_lines(session, agency_id, sha256, header.report_date) is not None
        )
        unique = {t.ticket_no: t for t in sorted(parsed.tickets, key=lambda t: t.ticket_no)}
        upload = Upload(
            agency_id=agency_id,
            filename=filename,
            sha256=sha256,
            report_range_raw=header.report_range_raw,
            report_date=header.report_date,
            generated_on=header.generated_on,
            ticket_count=len(unique),
            warnings=list(parsed.warnings),
            uploaded_by_user_id=uploaded_by_user_id,
        )
        session.add(upload)
        session.flush()

        if not already_stored:
            _store_amadeus_lines(session, agency_id, upload.id, parsed.tickets)

        now = datetime.now(UTC)
        existing = 0
        for chunk in _chunks(list(unique.values()), UPSERT_CHUNK):
            existing += _count_existing(session, agency_id, [t.ticket_no for t in chunk])
            rows = [_row(t, agency_id, header.report_date, upload.id, now) for t in chunk]
            _upsert_chunk(session, rows)

        upload_id = upload.id
        upload.inserted_count = len(unique) - existing
        upload.updated_count = existing
        if commit:
            session.commit()
        else:
            session.flush()
    except Exception:
        session.rollback()
        raise
    # Core upserts bypass the identity map; refresh anything already loaded.
    for obj in list(session.identity_map.values()):
        if isinstance(obj, Ticket):
            session.expire(obj)
    return ImportResult(upload_id=upload_id, inserted=len(unique) - existing, updated=existing)


def tickets_for_day(
    session: Session,
    agency_id: int,
    report_date: date,
    staff_signs: Collection[str] | None = None,
) -> list[Ticket]:
    """Every ticket of one report day, whatever its price status, in sign/booking/ticket order.

    Cancelled lines are included (the dashboard lists them apart); ``app.dashboard.summarize``
    leaves them out of every number.

    ``staff_signs`` limits the result to those signs (what a staff member may see); an empty
    collection is an empty result, never "all". Served by ``ix_tickets_agency_report_date_staff``.
    """
    bind_tenant(session, agency_id)
    if staff_signs is not None and not staff_signs:
        return []
    stmt = select(Ticket).where(Ticket.agency_id == agency_id, Ticket.report_date == report_date)
    if staff_signs is not None:
        stmt = stmt.where(Ticket.staff_sign.in_(list(staff_signs)))
    stmt = stmt.order_by(Ticket.staff_sign, Ticket.booking_ref, Ticket.ticket_no)
    return list(session.scalars(stmt))


def get_upload(
    session: Session, agency_id: int, upload_id: int, uploaded_by: int | None = None
) -> Upload | None:
    """One upload of this agency; another agency's id is None, like a missing one.

    With ``uploaded_by`` only an upload made by that user is found (what staff may open): a
    colleague's upload is None too.
    """
    bind_tenant(session, agency_id)
    stmt = select(Upload).where(Upload.agency_id == agency_id, Upload.id == upload_id)
    if uploaded_by is not None:
        stmt = stmt.where(Upload.uploaded_by_user_id == uploaded_by)
    return session.scalars(stmt).one_or_none()


def recent_uploads(
    session: Session, agency_id: int, limit: int = 50, uploaded_by: int | None = None
) -> list[Upload]:
    """The agency's newest uploads first (``ORDER BY id DESC``); only one user's with
    ``uploaded_by``."""
    bind_tenant(session, agency_id)
    stmt = select(Upload).where(Upload.agency_id == agency_id)
    if uploaded_by is not None:
        stmt = stmt.where(Upload.uploaded_by_user_id == uploaded_by)
    return list(session.scalars(stmt.order_by(Upload.id.desc()).limit(limit)))


def upload_ticket_dates(session: Session, agency_id: int, upload_id: int) -> list[tuple[date, int]]:
    """The report days of the tickets this upload last wrote, with how many tickets each holds.

    A ticket that already existed keeps the day it was first stored on, so after a re-upload with
    another date this can differ from the upload's own ``report_date``. (A later upload of the
    same tickets takes them over: this is "as of now", not a history.)
    """
    bind_tenant(session, agency_id)
    rows = session.execute(
        select(Ticket.report_date, func.count())
        .where(Ticket.agency_id == agency_id, Ticket.last_upload_id == upload_id)
        .group_by(Ticket.report_date)
        .order_by(Ticket.report_date)
    )
    return [(row[0], int(row[1])) for row in rows]


def user_names(session: Session, agency_id: int, user_ids: Sequence[int | None]) -> dict[int, str]:
    """Display names (the phone when none was given) of some users of this agency."""
    wanted = {i for i in user_ids if i is not None}
    if not wanted:
        return {}
    bind_tenant(session, agency_id)
    rows = session.execute(
        select(User.id, User.display_name, User.phone).where(
            User.agency_id == agency_id, User.id.in_(wanted)
        )
    )
    return {row.id: row.display_name or row.phone for row in rows}


def latest_report_date(
    session: Session, agency_id: int, staff_signs: Collection[str] | None = None
) -> date | None:
    """The newest report date that has tickets (None when there are none).

    ``staff_signs`` looks only at the tickets of those signs (what a staff member may see); an
    empty collection is "no tickets", never "all".
    """
    bind_tenant(session, agency_id)
    if staff_signs is not None and not staff_signs:
        return None
    stmt = select(func.max(Ticket.report_date)).where(Ticket.agency_id == agency_id)
    if staff_signs is not None:
        stmt = stmt.where(Ticket.staff_sign.in_(list(staff_signs)))
    return session.scalar(stmt)


def recent_report_dates(
    session: Session,
    agency_id: int,
    limit: int = 14,
    staff_signs: Collection[str] | None = None,
) -> list[date]:
    """The newest report dates that have tickets, latest first.

    ``staff_signs`` keeps only the days that hold a ticket of those signs, so a staff member is
    never shown a day (or learns that a day exists) on which they have nothing; an empty
    collection is an empty list.
    """
    bind_tenant(session, agency_id)
    if staff_signs is not None and not staff_signs:
        return []
    stmt = select(Ticket.report_date).where(Ticket.agency_id == agency_id)
    if staff_signs is not None:
        stmt = stmt.where(Ticket.staff_sign.in_(list(staff_signs)))
    stmt = stmt.distinct().order_by(Ticket.report_date.desc()).limit(limit)
    return list(session.scalars(stmt))


def sign_owners(session: Session, agency_id: int) -> dict[str, str]:
    """Which person each linked staff sign belongs to (display name, else phone)."""
    bind_tenant(session, agency_id)
    rows = session.execute(
        select(StaffSignLink.staff_sign, User.display_name, User.phone)
        .join(
            User, (User.agency_id == StaffSignLink.agency_id) & (User.id == StaffSignLink.user_id)
        )
        .where(StaffSignLink.agency_id == agency_id)
    )
    return {row.staff_sign: row.display_name or row.phone for row in rows}


# --- purging expired rows ----------------------------------------------------------------------

AUDIT_RETENTION_DAYS = 400  # keep in step with the retention_purge policy of migration 0003


@dataclass(frozen=True)
class PurgeCounts:
    user_sessions: int
    temporary_passwords: int
    audit_events: int

    @property
    def total(self) -> int:
        return self.user_sessions + self.temporary_passwords + self.audit_events


_PG_CLASS = table(
    "pg_class",
    column("relname", String),
    column("relnamespace"),
    column("relforcerowsecurity", Boolean),
)
_PG_NAMESPACE = table("pg_namespace", column("oid"), column("nspname", String))
_PURGED_TABLES = (
    UserSession.__tablename__,
    TemporaryPassword.__tablename__,
    AuditEvent.__tablename__,
)


def _require_forced_rls(session: Session) -> None:
    forced = session.scalar(
        select(func.count())
        .select_from(_PG_CLASS)
        .join(_PG_NAMESPACE, _PG_CLASS.c.relnamespace == _PG_NAMESPACE.c.oid)
        .where(
            _PG_NAMESPACE.c.nspname == func.current_schema(),
            _PG_CLASS.c.relname.in_(_PURGED_TABLES),
            _PG_CLASS.c.relforcerowsecurity.is_(True),
        )
    )
    if forced != len(_PURGED_TABLES):
        raise RuntimeError(
            "refusing to purge: row-level security is not forced on every purged table, and "
            "on Postgres the policies (not a WHERE clause) decide which rows may be deleted"
        )


def _delete_in_chunks(session: Session, model: Any, condition: Any) -> int:
    """Delete the rows matching ``condition``, ``PURGE_CHUNK`` per statement (SQLite)."""
    total = 0
    while True:
        oldest = select(model.id).where(condition).limit(PURGE_CHUNK)
        result = session.execute(delete(model).where(model.id.in_(oldest)))
        removed = cast("CursorResult[Any]", result).rowcount
        session.commit()
        total += removed
        if removed < PURGE_CHUNK:
            return total


def purge_expired(session: Session) -> PurgeCounts:
    """Delete expired or revoked sessions, used or expired temporary passwords and audit events
    older than AUDIT_RETENTION_DAYS, for every agency. Works with no tenant bound, in both
    deployment modes, and commits.

    Postgres: the three DELETE-only policies of migration 0003 pick the rows, so the statements
    carry no WHERE clause (one would need SELECT access to rows that nothing lets anybody read).
    That makes an unqualified DELETE dangerous anywhere those policies are missing or a tenant
    is bound (its own policy would then apply too), so both are refused. SQLite has no
    row-level security, so there the same conditions are an ordinary WHERE clause, and the delete
    runs in chunks of ``PURGE_CHUNK`` rows with a commit after each.

    Why Postgres is not chunked: a DELETE that says ``LIMIT``, or picks ids with a subquery,
    reads the table, and nothing grants a read of those rows (adding a SELECT policy would expose
    expired sessions and old audit rows of every agency). The hourly cron job keeps each run
    small instead: it only ever finds what expired since the previous run.
    """
    if bound_tenant(session) is not None:
        raise TenantBindingError(
            "purge_expired must run on an unbound session: with a tenant bound, that tenant's "
            "own policy would apply to the DELETE as well"
        )
    postgres = session.get_bind().dialect.name == "postgresql"
    if postgres:
        _require_forced_rls(session)
    now = datetime.now(UTC)
    conditions = {
        UserSession: or_(
            UserSession.absolute_expires_at < now, UserSession.revoked_at.is_not(None)
        ),
        TemporaryPassword: or_(
            TemporaryPassword.expires_at < now, TemporaryPassword.used_at.is_not(None)
        ),
        AuditEvent: AuditEvent.created_at < now - timedelta(days=AUDIT_RETENTION_DAYS),
    }
    try:
        deleted: list[int] = []
        for model, condition in conditions.items():
            if postgres:
                result = session.execute(delete(model))
                deleted.append(cast("CursorResult[Any]", result).rowcount)
            else:
                deleted.append(_delete_in_chunks(session, model, condition))
        session.commit()
    except Exception:
        session.rollback()
        raise
    return PurgeCounts(*deleted)
