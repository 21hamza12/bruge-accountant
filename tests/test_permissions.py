"""The permission matrix, the role-comparison guard, and the accountant role on the web."""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app import accounts, permissions
from app.models import ROLE_ACCOUNTANT, ROLE_MANAGER, ROLE_STAFF, User
from app.tenancy import rebind_tenant
from app.web.deps import AuthContext, require_permission
from tests.page_support import Env
from tests.web_support import (
    PASSWORD_A,
    PASSWORD_S,
    PHONE_A,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    owner_session,
)

ACCOUNTANT_PHONE = "+22212345660"
NEW_PHONE = "+22212345661"
ROLES = (ROLE_MANAGER, ROLE_ACCOUNTANT, ROLE_STAFF)

# function -> the roles that may
MATRIX: dict[str, frozenset[str]] = {
    "sees_all_signs": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_manage_staff": frozenset({ROLE_MANAGER}),
    "can_manage_partners": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_validate_money": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_approve_partner_adjustment": frozenset({ROLE_MANAGER}),
    "can_view_agency_details": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_edit_agency_details": frozenset({ROLE_MANAGER}),
    "can_export_partner_statement": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_enter_partner_opening": frozenset({ROLE_MANAGER}),
    "can_check_partner_switchover": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_close_day": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_reopen_day": frozenset({ROLE_MANAGER}),
    "can_edit_any_entry": frozenset({ROLE_MANAGER, ROLE_ACCOUNTANT}),
    "can_enter_own_sales": frozenset({ROLE_STAFF}),
    # Owner's decision, 28 Oct 2026: the profit and the margin are the manager's alone.
    "can_see_profit": frozenset({ROLE_MANAGER}),
}
# Granted to staff alone: the others write sales through can_edit_any_entry.
STAFF_ONLY = frozenset({"can_enter_own_sales"})


def _user(role: str) -> User:
    return User(agency_id=1, phone="+22200000000", role=role)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("name", sorted(MATRIX))
def test_matrix_per_role(name: str, role: str) -> None:
    check: Callable[[User], bool] = getattr(permissions, name)
    assert check(_user(role)) is (role in MATRIX[name])


def test_the_matrix_covers_every_single_user_permission() -> None:
    public = {n for n in dir(permissions) if n.startswith(("sees_", "can_")) and n in MATRIX}
    assert public == set(MATRIX)
    assert hasattr(permissions, "can_reissue_password_for")


# Spec-level statements, written from the plan and not from the implementation.


def test_staff_has_no_permission_at_all() -> None:
    assert not any(
        getattr(permissions, name)(_user(ROLE_STAFF)) for name in MATRIX if name not in STAFF_ONLY
    )
    assert permissions.can_enter_own_sales(_user(ROLE_STAFF))


def test_an_accountant_never_manages_staff_or_reopens_a_day() -> None:
    accountant = _user(ROLE_ACCOUNTANT)
    assert not permissions.can_manage_staff(accountant)
    assert not permissions.can_reopen_day(accountant)


def test_a_manager_has_every_permission() -> None:
    assert all(
        getattr(permissions, name)(_user(ROLE_MANAGER)) for name in MATRIX if name not in STAFF_ONLY
    )
    assert not any(getattr(permissions, name)(_user(ROLE_MANAGER)) for name in STAFF_ONLY)


def test_the_sales_screens_are_for_staff_manager_and_accountant_only() -> None:
    for role in ROLES:
        assert permissions.can_use_sales_screens(_user(role)) is True
    for role in ("root", "", "MANAGER", "admin"):
        assert permissions.can_use_sales_screens(_user(role)) is False


def test_an_unknown_role_has_no_permission() -> None:
    for role in ("root", "", "MANAGER", "admin"):
        user = _user(role)
        assert not any(getattr(permissions, name)(user) for name in MATRIX)
        assert not permissions.can_reissue_password_for(_user(ROLE_MANAGER), user)


@pytest.mark.parametrize(
    ("actor", "target", "expected"),
    [
        (ROLE_MANAGER, ROLE_STAFF, True),
        (ROLE_MANAGER, ROLE_ACCOUNTANT, True),
        (ROLE_MANAGER, ROLE_MANAGER, False),
        (ROLE_ACCOUNTANT, ROLE_STAFF, False),
        (ROLE_ACCOUNTANT, ROLE_ACCOUNTANT, False),
        (ROLE_STAFF, ROLE_STAFF, False),
        (ROLE_STAFF, ROLE_ACCOUNTANT, False),
    ],
)
def test_who_may_reissue_whose_password(actor: str, target: str, expected: bool) -> None:
    assert permissions.can_reissue_password_for(_user(actor), _user(target)) is expected


def test_only_a_manager_has_an_owner_managed_password() -> None:
    assert permissions.has_owner_managed_password(_user(ROLE_MANAGER))
    assert not permissions.has_owner_managed_password(_user(ROLE_ACCOUNTANT))
    assert not permissions.has_owner_managed_password(_user(ROLE_STAFF))
    assert not permissions.has_owner_managed_password(_user("root"))


def test_the_staff_pages_dropdown_lists_every_role_once() -> None:
    assert sorted(accounts.ROLES) == sorted(ROLES)


# --- guard: role strings are compared only in permissions.py and (marked lines) accounts.py ------

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PERMISSIONS_MODULE = APP_DIR / "permissions.py"
MARKER = "# role-check-ok"
ROLE_NAMES = {"manager", "accountant", "staff"}
ROLE_CONSTANTS = {"ROLE_MANAGER", "ROLE_ACCOUNTANT", "ROLE_STAFF"}
TEMPLATE_COMPARISON = re.compile(
    r"""\brole\s*(==|!=|\bin\b)\s*["'(\[]|["'](manager|accountant|staff)["']\s*(==|!=)\s*\S*role"""
)


def _is_role_value(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value in ROLE_NAMES
    if isinstance(node, ast.Name):
        return node.id in ROLE_CONSTANTS
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_is_role_value(e) for e in node.elts)
    return False


def _is_role_attribute(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "role"


def role_checks(source: str) -> list[int]:
    """Lines (not ending in the role-check marker) that decide on a role: a comparison with a role
    string or constant, ``.in_(...)`` with one, ``filter_by(role=...)``, or a ``match`` on
    ``x.role`` or with role patterns.

    Limits, stated plainly: a role copied into another variable first (``r = user.role``,
    then ``r == "manager"``) is not seen unless a role string or constant sits in the comparison,
    and neither is dynamic access (``getattr(user, "role")``) or a role compared in SQL text.
    Review those by eye."""
    lines = source.splitlines()
    found: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        hit = False
        if isinstance(node, ast.Compare):
            hit = any(_is_role_value(o) for o in [node.left, *node.comparators])
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "in_":
                hit = any(_is_role_value(a) for a in node.args)
            if isinstance(func, ast.Attribute) and func.attr in {"filter_by"}:
                hit = hit or any(k.arg == "role" for k in node.keywords)
        elif isinstance(node, ast.Match):
            hit = _is_role_attribute(node.subject) or any(
                _is_role_value(part) for case in node.cases for part in ast.walk(case.pattern)
            )
        if hit:
            line = node.lineno  # type: ignore[attr-defined]
            if MARKER not in lines[line - 1]:
                found.add(line)
    return sorted(found)


def test_role_decisions_live_only_in_permissions_and_marked_accounts_lines() -> None:
    offenders = [
        f"{path.relative_to(APP_DIR)}:{line}"
        for path in sorted(APP_DIR.rglob("*.py"))
        if path != PERMISSIONS_MODULE
        for line in role_checks(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_markers_are_used_only_in_accounts() -> None:
    marked = [
        path.name
        for path in sorted(APP_DIR.rglob("*.py"))
        if MARKER in path.read_text(encoding="utf-8")
    ]
    assert marked == ["accounts.py"]


def test_templates_do_not_compare_roles() -> None:
    offenders = [
        str(path.relative_to(APP_DIR))
        for path in sorted(APP_DIR.rglob("*.html"))
        if TEMPLATE_COMPARISON.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "source",
    [
        "x = user.role == ROLE_MANAGER",
        "x = user.role != 'manager'",
        "x = user.role in (ROLE_STAFF, ROLE_MANAGER)",
        "x = 'accountant' == user.role",
        "q = select(User).where(User.role.in_([ROLE_MANAGER, ROLE_ACCOUNTANT]))",
        "q = select(User).filter_by(role=ROLE_MANAGER)",
        "match user.role:\n    case _:\n        pass",
        "match x:\n    case 'manager':\n        pass",
    ],
)
def test_the_role_guard_catches_the_usual_shapes(source: str) -> None:
    assert role_checks(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "x = user.role == other.role",
        "x = user.name == 'staff_a'",
        "x = role in ROLES",
        "x = user.role == ROLE_MANAGER  # role-check-ok",
    ],
)
def test_the_role_guard_ignores_lookalikes_and_marked_lines(source: str) -> None:
    assert role_checks(source) == []


def test_template_guard_shapes() -> None:
    assert TEMPLATE_COMPARISON.search('{% if user.role == "manager" %}')
    assert not TEMPLATE_COMPARISON.search("{% if user.is_active %}")


# --- require_permission -------------------------------------------------------------------------


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def _accountant(env: Env, phone: str = ACCOUNTANT_PHONE) -> int:
    return add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        phone,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )


def _users(env: Env) -> list[User]:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        agency = env.seeded.agency_a
        return list(s.scalars(select(User).where(User.agency_id == agency).order_by(User.id)))


def test_an_unknown_role_is_refused_by_require_permission_and_audited(env: Env) -> None:
    """The role CHECK stops such a row from existing, so the user is detached and relabelled for
    the length of the request: what a future role that no permission mentions would look like."""
    from sqlalchemy.orm import object_session

    from app.web.deps import current_user, require_login

    app: FastAPI = env.factory.app  # type: ignore[attr-defined]

    def odd_user(auth: AuthContext = Depends(require_login)) -> AuthContext:  # noqa: B008
        session = object_session(auth.user)
        assert session is not None
        session.expunge(auth.user)
        auth.user.role = "root"
        return auth

    def page(
        auth: AuthContext = Depends(require_permission(permissions.sees_all_signs)),  # noqa: B008
    ) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.add_api_route("/t/all-signs", page, methods=["GET"])
    c = env.staff_a()
    assert c.get("/t/all-signs").status_code == 403  # staff: refused, as a baseline
    app.dependency_overrides[current_user] = odd_user
    try:
        assert c.get("/t/all-signs").status_code == 403
    finally:
        app.dependency_overrides.clear()
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert [e.actor_user_id for e in denied] == [env.seeded.staff_a] * 2
    assert env.manager_a().get("/t/all-signs").status_code == 200


def test_the_dependency_is_named_after_its_permission() -> None:
    assert require_permission(permissions.can_close_day).__name__ == "require_can_close_day"


# --- the web ------------------------------------------------------------------------------------

SIDE_HREFS = re.compile(r'<a class="nav-link" href="(/[^"]*)"')
USER_ROLE = re.compile(r'<span class="user-role">([^<]*)</span>')


def test_accountant_sees_all_signs_in_shell(env: Env) -> None:
    env.load()
    _accountant(env)
    with env.manager_a() as m:
        manager_html = m.get("/").text
    with env.as_user(ACCOUNTANT_PHONE, PASSWORD_S) as c:
        html = c.get("/").text
    # The same menu as the manager's, derived and not hardcoded, minus the staff management.
    manager_links, links = SIDE_HREFS.findall(manager_html), SIDE_HREFS.findall(html)
    assert "/questions" not in links
    assert "/staff" in manager_links
    assert links == [h for h in manager_links if h != "/staff"]
    assert 'href="/staff"' not in html  # but no staff management
    role = USER_ROLE.search(html)
    assert role is not None and role.group(1) == "Comptable"
    manager_role = USER_ROLE.search(manager_html)
    assert manager_role is not None and manager_role.group(1) == "Responsable"


STAFF_POSTS = [
    "/staff",
    "/staff/{staff}/signs",
    "/staff/{staff}/signs/1/unlink",
    "/staff/{staff}/role",
    "/staff/{staff}/deactivate",
    "/staff/{staff}/temp-password",
]
STAFF_GETS = ["/staff", "/staff/{staff}"]


def test_every_staff_route_uses_the_staff_admin_dependency() -> None:
    """The parametrised tests below must cover all of them: count and inspect the routes."""
    from fastapi.routing import APIRoute

    from app.web.deps import require_staff_admin
    from app.web.routes import staff as staff_routes

    routes = [r for r in staff_routes.router.routes if isinstance(r, APIRoute)]
    assert len(routes) == len(STAFF_POSTS) + len(STAFF_GETS) == 8
    for route in routes:
        calls = [d.call for d in route.dependant.dependencies]
        assert require_staff_admin in calls, route.path


@pytest.mark.parametrize("path", STAFF_GETS)
def test_accountant_cannot_open_staff_pages_403_and_audited(env: Env, path: str) -> None:
    accountant = _accountant(env)
    c = env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)
    response = c.get(path.format(staff=env.seeded.staff_a))
    assert response.status_code == 403
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert [e.actor_user_id for e in denied] == [accountant]
    assert denied[0].details["path"] == path.format(staff=env.seeded.staff_a)


@pytest.mark.parametrize("path", STAFF_POSTS)
def test_accountant_gets_403_and_an_audit_event_on_every_staff_post(env: Env, path: str) -> None:
    accountant = _accountant(env)
    before = _users(env)
    c = env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)
    response = c.post(
        path.format(staff=env.seeded.staff_a),
        data={
            "display_name": "Intrus",
            "phone": NEW_PHONE,
            "role": "manager",
            "sign": "0001AA",
            "csrf_token": csrf_for(c),
        },
    )
    assert response.status_code == 403
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert [e.actor_user_id for e in denied] == [accountant]
    assert [(u.id, u.role, u.is_active) for u in _users(env)] == [
        (u.id, u.role, u.is_active) for u in before
    ]  # nothing was created, promoted or deactivated


def test_manager_can_create_accountant(env: Env) -> None:
    c = env.manager_a()
    assert "Comptable" in c.get("/staff").text  # the role option
    response = c.post(
        "/staff",
        data={
            "display_name": "Nadia Test",
            "phone": NEW_PHONE,
            "role": "accountant",
            "csrf_token": csrf_for(c, "/staff"),
        },
        follow_redirects=False,
    )
    # 200, not a redirect: the temporary password is shown once, in this very response.
    assert response.status_code == 200
    created = next(u for u in _users(env) if u.phone == NEW_PHONE)
    assert created.role == ROLE_ACCOUNTANT
    row = re.search(
        r"<tr>(?:(?!</tr>).)*Nadia Test(?:(?!</tr>).)*</tr>", c.get("/staff").text, re.S
    )
    assert row is not None
    assert "Comptable" in row.group(0)
    detail = c.get(f"/staff/{created.id}").text
    assert "Comptable" in detail
    assert re.search(r'<option value="accountant" selected>Comptable</option>', detail)


def test_manager_can_change_a_role_to_and_from_accountant(env: Env) -> None:
    c = env.manager_a()
    token = csrf_for(c, "/staff")
    staff = env.seeded.staff_a
    for role in ("accountant", "staff"):
        response = c.post(
            f"/staff/{staff}/role",
            data={"role": role, "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert next(u for u in _users(env) if u.id == staff).role == role


def test_last_manager_cannot_become_accountant(env: Env) -> None:
    c = env.manager_a()
    token = csrf_for(c, "/staff")
    me = env.seeded.manager_a
    response = c.post(
        f"/staff/{me}/role",
        data={"role": "accountant", "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "au moins un responsable actif" in response.text
    assert next(u for u in _users(env) if u.id == me).role == ROLE_MANAGER


def test_the_detail_page_offers_reissue_for_an_accountant_not_for_a_manager(env: Env) -> None:
    accountant = _accountant(env)
    c = env.manager_a()
    assert "/temp-password" in c.get(f"/staff/{accountant}").text
    manager_page = c.get(f"/staff/{env.seeded.manager_a}").text
    assert "/temp-password" not in manager_page
    assert "réinitialisé par l'administrateur" in manager_page


def test_a_reissue_for_an_accountant_is_audited_with_the_target_role(env: Env) -> None:
    accountant = _accountant(env)
    c = env.manager_a()
    page = c.get(f"/staff/{accountant}").text
    expected = re.search(r'name="expected_temp_id" value="(\d+)"', page)
    assert expected is not None
    response = c.post(
        f"/staff/{accountant}/temp-password",
        data={"expected_temp_id": expected.group(1), "csrf_token": csrf_for(c, "/staff")},
    )
    assert response.status_code == 200
    issued = audit_events(env.db.owner_url, env.seeded.agency_a, "temp_password_issued")
    assert issued[-1].details["target_role"] == ROLE_ACCOUNTANT


def test_a_manager_cannot_reissue_a_manager_and_the_audit_names_the_role(env: Env) -> None:
    c = env.manager_a()
    response = c.post(
        f"/staff/{env.seeded.manager_a}/temp-password",
        data={"expected_temp_id": "None", "csrf_token": csrf_for(c, "/staff")},
    )
    assert response.status_code == 403
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert denied[-1].details["target_role"] == ROLE_MANAGER


def test_the_manager_login_still_works_after_the_refactor(env: Env) -> None:
    assert env.as_user(PHONE_A, PASSWORD_A).get("/staff").status_code == 200
