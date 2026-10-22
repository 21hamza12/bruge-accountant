"""What the application shell (sidebar, top bar, bottom bar) needs on every signed-in page.

``build_shell`` is the one place that decides it, so a page only passes ``**shell_context(...)``
to ``page()``. It reads, for the signed-in user's own agency only (the id comes from the session):

* the agency (its name and currency);
* the day pages open on (a manager: the agency's newest report day; staff: the newest day with a
  ticket on their own signs, else the agency's) and, for that day, how many price questions still
  wait, per staff sign. A manager counts every sign and sees the agent list; staff count only the
  signs linked to them. The badge on "Questions de prix" is the sum.

Three or four small indexed queries per page. The tickets themselves are not loaded here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import accounts, permissions
from app.models import Agency, User
from app.repository import awaiting_counts_by_sign, latest_report_date, sign_owners
from app.web.deps import AuthContext

ACTIVE_HOME = "home"
ACTIVE_QUESTIONS = "questions"
ACTIVE_UPLOADS = "uploads"
ACTIVE_UPLOAD_NEW = "upload_new"
ACTIVE_STAFF = "staff"
ACTIVE_PARTNERS = "partners"
ACTIVE_PROFILE = "profile"


@dataclass(frozen=True)
class AgentNav:
    staff_sign: str
    owner: str | None  # None: the sign is not linked to anyone
    waiting: int


@dataclass(frozen=True)
class Shell:
    agency: Agency
    user: User
    sees_all: bool  # every sign of the agency (manager, accountant)
    can_manage_staff: bool
    can_manage_partners: bool
    bottom_bar_partners: bool  # five tabs fit 320px: only users without the staff tab get this one
    active: str
    latest_day: date | None
    badge: int
    agents: tuple[AgentNav, ...]  # managers only
    owners: Mapping[str, str]  # managers only: sign to person
    my_signs: tuple[str, ...]  # staff only: the signs linked to them

    @property
    def visible_signs(self) -> tuple[str, ...] | None:
        """The staff signs a query may look at: None (all of them) for a manager, the linked
        signs for staff (an empty tuple means nothing, never everything)."""
        return None if self.sees_all else self.my_signs

    @property
    def initial(self) -> str:
        return (self.agency.name.strip() or "B")[0].upper()

    @property
    def user_initial(self) -> str:
        return ((self.user.display_name or "").strip() or self.user.phone.lstrip("+") or "?")[
            0
        ].upper()


def _default_day(db: Session, agency_id: int, staff_signs: tuple[str, ...] | None) -> date | None:
    """The day pages open on. A manager gets the agency's newest report day. Staff get the newest
    day that has a ticket on their own signs, and only when there is none the agency's newest day
    (the pages then say that nothing is theirs that day)."""
    if staff_signs is None:
        return latest_report_date(db, agency_id)
    own = latest_report_date(db, agency_id, staff_signs) if staff_signs else None
    return own or latest_report_date(db, agency_id)


def build_shell(db: Session, auth: AuthContext, active: str) -> Shell:
    user = auth.user
    agency_id = user.agency_id
    agency = db.scalars(select(Agency).where(Agency.id == agency_id)).one_or_none()
    if agency is None:  # cannot happen (foreign key); fail closed rather than render half a page
        raise HTTPException(status_code=404)
    sees_all = permissions.sees_all_signs(user)
    my_signs: tuple[str, ...] = ()
    if not sees_all:
        my_signs = tuple(link.staff_sign for link in accounts.get_signs(db, agency_id, user.id))
    day = _default_day(db, agency_id, None if sees_all else my_signs)
    counts: dict[str, int] = {}
    if day is not None:
        counts = awaiting_counts_by_sign(db, agency_id, day, None if sees_all else my_signs)
    owners: Mapping[str, str] = sign_owners(db, agency_id) if sees_all else {}
    agents = (
        tuple(AgentNav(sign, owners.get(sign), n) for sign, n in sorted(counts.items()))
        if sees_all
        else ()
    )
    return Shell(
        agency=agency,
        user=user,
        sees_all=sees_all,
        can_manage_staff=permissions.can_manage_staff(user),
        can_manage_partners=permissions.can_manage_partners(user),
        bottom_bar_partners=permissions.can_manage_partners(user)
        and not permissions.can_manage_staff(user),
        active=active,
        latest_day=day,
        badge=sum(counts.values()),
        agents=agents,
        owners=owners,
        my_signs=my_signs,
    )


def shell_context(db: Session, auth: AuthContext, active: str) -> dict[str, Any]:
    """The template context every signed-in page shares."""
    shell = build_shell(db, auth, active)
    return {
        "shell": shell,
        "agency": shell.agency,
        "sees_all": shell.sees_all,
        "csrf_token": auth.csrf_token,
    }
