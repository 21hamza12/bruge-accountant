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
from sqlalchemy import literal, select
from sqlalchemy.orm import Session

from app import accounts, approvals, flags, partner_aliases, permissions
from app.models import Agency, User
from app.repository import awaiting_counts_by_sign, latest_report_date, sign_owners
from app.web.deps import AuthContext

ACTIVE_HOME = "home"
ACTIVE_QUESTIONS = "questions"
ACTIVE_UPLOADS = "uploads"
ACTIVE_UPLOAD_NEW = "upload_new"
ACTIVE_STAFF = "staff"
ACTIVE_PARTNERS = "partners"
ACTIVE_SALE_NEW = "sale_new"
ACTIVE_SALES = "sales"
ACTIVE_DAYS = "days"
ACTIVE_PROFILE = "profile"
ACTIVE_FLAGS = "flags"
ACTIVE_VALIDATE = "validate"
ACTIVE_CHAT = "chat"


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
    can_enter_sales: bool  # staff: "Saisir une vente" and "Mes ventes"
    can_see_day_sales: bool  # manager, accountant: "Ventes du jour"
    # Five tabs fit 320px, so the bottom bar differs by role (a test counts them per role):
    bottom_bar_partners: bool  # only users without the staff tab get this one
    bottom_bar_sales: bool  # the two sales tabs, for staff, in place of "Questions"
    bottom_bar_questions: bool
    active: str
    latest_day: date | None
    badge: int
    flag_badge: int  # open blocking comparison flags the viewer may see ("Écarts")
    can_chat: bool  # the "Discussion" entry (staff, manager, accountant)
    can_validate: bool  # manager, accountant: the "À valider" screen
    validate_badge: int  # answers and fee overrides waiting for a validator ("À valider")
    pending_aliases: int  # agency names waiting for a decision (who may manage partners)
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
    # Every sidebar badge in one SELECT (each a scalar subquery; 0 when the role has no badge).
    visible = None if sees_all else my_signs
    zero = literal(0)
    flag_badge, validate_badge, pending = db.execute(
        select(
            flags.open_blocking_query(agency_id, visible) if visible is None or visible else zero,
            approvals.to_validate_query(agency_id)
            if permissions.can_validate_money(user)
            else zero,
            partner_aliases.pending_count_query(agency_id)
            if permissions.can_manage_partners(user)
            else zero,
        )
    ).one()
    return Shell(
        agency=agency,
        user=user,
        sees_all=sees_all,
        can_manage_staff=permissions.can_manage_staff(user),
        can_manage_partners=permissions.can_manage_partners(user),
        can_enter_sales=permissions.can_enter_own_sales(user),
        can_see_day_sales=permissions.can_edit_any_entry(user),
        bottom_bar_partners=permissions.can_manage_partners(user)
        and not permissions.can_manage_staff(user),
        bottom_bar_sales=permissions.can_enter_own_sales(user),
        bottom_bar_questions=not permissions.can_enter_own_sales(user),
        active=active,
        latest_day=day,
        badge=sum(counts.values()),
        flag_badge=int(flag_badge or 0),
        can_chat=permissions.can_use_chat(user),
        can_validate=permissions.can_validate_money(user),
        validate_badge=int(validate_badge or 0),
        pending_aliases=int(pending or 0),
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
