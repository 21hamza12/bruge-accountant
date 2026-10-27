"""Who may do what: every role decision in the application goes through these functions.

Pure functions over a ``User``; they read nothing but ``user.role``. A role that is not listed
(an unknown value) may do nothing. A guard test (tests/test_permissions.py) fails when role
strings or constants are compared anywhere else than here and in app/accounts.py.
"""

from __future__ import annotations

from app.models import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_STAFF, User

_MANAGER_ONLY = frozenset({ROLE_MANAGER})
_MANAGER_OR_ACCOUNTANT = frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT})
_STAFF_ONLY = frozenset({ROLE_STAFF})
# Accounts whose temporary password a manager may issue. An allowlist: a new or unknown role is
# not on it until somebody decides so.
_PASSWORD_REISSUABLE = frozenset({ROLE_STAFF, ROLE_ACCOUNTANT})
_OWNER_MANAGED_PASSWORD = frozenset({ROLE_MANAGER})


def sees_all_signs(user: User) -> bool:
    """Every staff sign of the agency, not only the ones linked to the user."""
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_manage_staff(user: User) -> bool:
    return user.role in _MANAGER_ONLY


def can_manage_partners(user: User) -> bool:
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_validate_money(user: User) -> bool:
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_approve_partner_adjustment(user: User) -> bool:
    """Approve or reject an ``AJUSTEMENT`` of a partner account (phase F): the manager only, and
    never on a line they entered themselves (four eyes, checked by ``app/partner_book.py``)."""
    return user.role in _MANAGER_ONLY


def can_close_day(user: User) -> bool:
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_export_day(user: User) -> bool:
    """Download the TJQ APRES of a day (stored or draft): manager and accountant."""
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_compare_apres(user: User) -> bool:
    """Compare the stored TJQ APRES of a closed day with the accountant's own sheet (the parallel
    run, doc 15): the roles that may export it, manager and accountant."""
    return can_export_day(user)


def can_reopen_day(user: User) -> bool:
    return user.role in _MANAGER_ONLY


def can_run_comparison(user: User) -> bool:
    """Run (again) the comparison of a day with its Amadeus report: manager and accountant."""
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_edit_any_entry(user: User) -> bool:
    return user.role in _MANAGER_OR_ACCOUNTANT


def can_enter_own_sales(user: User) -> bool:
    """Staff enter and correct the sales of their own linked signs (any day that is not closed, see
    app/sales.py).
    Manager and accountant do not need this: they write any sale (``can_edit_any_entry``). A role
    that is not listed (an unknown value) may do nothing."""
    return user.role in _STAFF_ONLY


def can_use_sales_screens(user: User) -> bool:
    """The entry form, the list and the page of a sale: staff for their own signs, manager and
    accountant for the whole agency (what each may do to a given sale is decided by app/sales.py).
    A role that is not listed (an unknown value) gets none of it."""
    return can_enter_own_sales(user) or can_edit_any_entry(user)


def can_answer_flags(user: User) -> bool:
    """Answer a comparison flag (B9): staff for the flags of their own signs, manager and
    accountant for any flag. Which flags a staff member may see is decided by app/flags.py."""
    return can_use_sales_screens(user)


def can_use_chat(user: User) -> bool:
    """The in-app chat: whoever may use the sales screens, or sees every sign (manager,
    accountant). What each lookup shows is decided per tool (``app/chat/tools.py``), with the
    same rules as the screens. A role that is not listed (an unknown value) gets none of it."""
    return can_use_sales_screens(user) or sees_all_signs(user)


def can_reissue_password_for(actor: User, target: User) -> bool:
    """``actor`` may issue a temporary password to ``target``: only a staff manager, and only for
    staff or an accountant (never for a manager, themselves included, nor an unknown role)."""
    return can_manage_staff(actor) and target.role in _PASSWORD_REISSUABLE


def has_owner_managed_password(target: User) -> bool:
    """The password of ``target`` is reset by the platform owner (CLI), not by a manager."""
    return target.role in _OWNER_MANAGED_PASSWORD
