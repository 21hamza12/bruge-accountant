"""Demo data for trying the app locally and for the end-to-end suite (``seed-demo``).

Two agencies, ``demo-a`` and ``demo-b``, each with a manager; ``demo-a`` also has two staff,
linked to the Amadeus signs ``0004SV`` and ``0009ER`` of the synthetic fixture. One password,
read from standard input, is used for all four accounts (never printed, never an argument).
``demo-b`` receives the fixture, so the two agencies hold overlapping ticket numbers and the
isolation between them is visible; ``demo-a`` stays empty for whoever tries the upload.

All or nothing: the pre-check looks at both slugs and all four phones and names every field that is
already taken; the creation is then ONE transaction (both agencies, the users, the links, the
audit events and the fixture), so a failure leaves nothing behind.

Never for production: the command refuses when ``ENV=production``, and refuses a database that is
neither SQLite nor on this machine unless ``--i-know-this-is-not-production`` is passed
(:func:`demo_target_refusal`).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.auth.passwords import hash_password
from app.ingest import ingest_report
from app.models import ROLE_STAFF, StaffSignLink, User
from app.parsing.amadeus import ParsedReport
from app.repository import (
    AlreadyExistsError,
    add_agency_with_manager,
    get_agency_by_slug,
)
from app.tenancy import bound_tenant, lookup_scope, rebind_tenant, restore_binding

OVERRIDE_FLAG = "--i-know-this-is-not-production"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

DEMO_A = "demo-a"
DEMO_B = "demo-b"
PHONE_MANAGER_A = "+22290000001"
PHONE_MANAGER_B = "+22290000002"
PHONE_STAFF_SV = "+22290000003"
PHONE_STAFF_ER = "+22290000004"
ALL_PHONES = (PHONE_MANAGER_A, PHONE_MANAGER_B, PHONE_STAFF_SV, PHONE_STAFF_ER)
SIGN_SV = "0004SV"
SIGN_ER = "0009ER"


@dataclass(frozen=True)
class SeededDemo:
    agency_a: int
    agency_b: int
    manager_a: int
    manager_b: int
    staff_sv: int
    staff_er: int
    upload_b: int


class DemoCollisionError(AlreadyExistsError):
    """Some demo slugs or phones are taken. ``fields`` names each, e.g. ``"phone +22290000001"``."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        super().__init__(fields[0].split(" ", 1)[0] if fields else "slug")
        self.fields = fields


def demo_target_refusal(url: str, *, allow_remote: bool) -> str | None:
    """Why ``seed-demo`` must not touch this database, or None when it may.

    SQLite and a Postgres on this machine (``localhost``, ``127.0.0.1``, ``::1`` or a unix
    socket, which has no host) are fine. Anything else is refused unless the operator passed
    ``--i-know-this-is-not-production``. The message never contains the URL's credentials.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" or allow_remote:
        return None
    host = (parsed.host or "").strip().lower()
    if not host or host in LOCAL_HOSTS or host.startswith("/"):
        return None
    return (
        f"refusing to seed demo accounts into a database on {host!r}, which is neither SQLite nor "
        f"this machine; if you are sure it is not production, pass {OVERRIDE_FLAG}"
    )


def _taken_fields(session: Session) -> tuple[str, ...]:
    """Every demo slug and phone that already exists, in a fixed order."""
    taken: list[str] = []
    for slug in (DEMO_A, DEMO_B):
        if get_agency_by_slug(session, slug) is not None:
            taken.append(f"slug {slug}")
    for phone in ALL_PHONES:
        lookup_scope(session, "app.login_phone", phone)
        if session.scalars(select(User.id).where(User.phone == phone)).first() is not None:
            taken.append(f"phone {phone}")
    return tuple(taken)


def _add_staff(
    session: Session, agency_id: int, phone: str, name: str, sign: str, hashed: str
) -> int:
    rebind_tenant(session, agency_id)
    user = User(
        agency_id=agency_id,
        phone=phone,
        role=ROLE_STAFF,
        display_name=name,
        password_hash=hashed,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    session.add(StaffSignLink(agency_id=agency_id, user_id=user.id, staff_sign=sign))
    session.flush()
    return user.id


def seed_demo(
    factory: sessionmaker[Session],
    parsed: ParsedReport,
    fixture_bytes: bytes,
    fixture_name: str,
    password: str,
    operator: str,
) -> SeededDemo:
    """Create the demo agencies and users in ONE transaction.

    Raises :class:`DemoCollisionError` (before writing anything) if a slug or phone is taken.
    ``parsed`` is the fixture already parsed by the caller, so a bad file is refused before
    anything is written. Any later failure rolls everything back.
    """
    hashed = hash_password(password)
    with factory() as session:
        taken = _taken_fields(session)
        if taken:
            session.rollback()
            raise DemoCollisionError(taken)
        previous = bound_tenant(session)
        try:
            a = add_agency_with_manager(
                session, DEMO_A, "Agence démo A", PHONE_MANAGER_A, "Responsable A", hashed, operator
            )
            staff_sv = _add_staff(
                session, a.agency_id, PHONE_STAFF_SV, "Employé SV", SIGN_SV, hashed
            )
            staff_er = _add_staff(
                session, a.agency_id, PHONE_STAFF_ER, "Employé ER", SIGN_ER, hashed
            )
            b = add_agency_with_manager(
                session, DEMO_B, "Agence démo B", PHONE_MANAGER_B, "Responsable B", hashed, operator
            )
            result = ingest_report(
                session,
                b.agency_id,
                fixture_bytes,
                fixture_name,
                parse=lambda _data: parsed,
                uploaded_by=b.user_id,
                commit=False,
            )
            session.commit()
        except BaseException:
            restore_binding(session, previous)
            session.rollback()
            raise
    return SeededDemo(
        agency_a=a.agency_id,
        agency_b=b.agency_id,
        manager_a=a.user_id,
        manager_b=b.user_id,
        staff_sv=staff_sv,
        staff_er=staff_er,
        upload_b=result.upload_id,
    )
