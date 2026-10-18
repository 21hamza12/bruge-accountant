"""Demo data for trying the app locally and for the end-to-end suite (``seed-demo``).

Two agencies, ``demo-a`` and ``demo-b``, each with a manager; ``demo-a`` also has two staff,
linked to the Amadeus signs ``0004SV`` and ``0009ER`` of the synthetic fixture. One password,
read from standard input, is used for all four accounts (never printed, never an argument).
``demo-b`` receives the fixture, so the two agencies hold overlapping ticket numbers and the
isolation between them is visible; ``demo-a`` stays empty for whoever tries the upload.

Never for production: the command refuses when ``ENV=production``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.auth.passwords import hash_password
from app.ingest import ingest_report
from app.models import ROLE_STAFF, StaffSignLink, User
from app.parsing.amadeus import ParsedReport
from app.repository import AlreadyExistsError, bootstrap_agency, get_agency_by_slug
from app.tenancy import bind_tenant, rebind_tenant

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
    session.commit()
    return user.id


def seed_demo(
    factory: sessionmaker[Session],
    parsed: ParsedReport,
    fixture_bytes: bytes,
    fixture_name: str,
    password: str,
    operator: str,
) -> SeededDemo:
    """Create the demo agencies and users. Raises ``AlreadyExistsError`` if either exists.

    ``parsed`` is the fixture already parsed by the caller, so a bad file is refused before
    anything is written.
    """
    hashed = hash_password(password)
    with factory() as session:
        for slug in (DEMO_A, DEMO_B):
            if get_agency_by_slug(session, slug) is not None:
                raise AlreadyExistsError("slug")
        session.rollback()
        a = bootstrap_agency(
            session, DEMO_A, "Agence démo A", PHONE_MANAGER_A, "Responsable A", hashed, operator
        )
        staff_sv = _add_staff(session, a.agency_id, PHONE_STAFF_SV, "Employé SV", SIGN_SV, hashed)
        staff_er = _add_staff(session, a.agency_id, PHONE_STAFF_ER, "Employé ER", SIGN_ER, hashed)
    with factory() as session:
        b = bootstrap_agency(
            session, DEMO_B, "Agence démo B", PHONE_MANAGER_B, "Responsable B", hashed, operator
        )
    with factory() as session:
        bind_tenant(session, b.agency_id)
        result = ingest_report(
            session,
            b.agency_id,
            fixture_bytes,
            fixture_name,
            parse=lambda _data: parsed,
            uploaded_by=b.user_id,
        )
    return SeededDemo(
        agency_a=a.agency_id,
        agency_b=b.agency_id,
        manager_a=a.user_id,
        manager_b=b.user_id,
        staff_sv=staff_sv,
        staff_er=staff_er,
        upload_b=result.upload_id,
    )
