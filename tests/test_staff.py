"""Step 22: manager staff management, under row-level security."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import StaffSignLink, TemporaryPassword, User
from app.tenancy import rebind_tenant
from tests.web_support import (
    PASSWORD_A,
    PASSWORD_B,
    PASSWORD_S,
    PHONE_A,
    PHONE_B,
    PHONE_STAFF_A,
    FakeClock,
    Seeded,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    database_text,
    login,
    make_client,
    owner_session,
    seed,
)

NEW_PHONE = "+22212345650"
EXPECTED = re.compile(r'name="expected_temp_id" value="(\d+)"')
SECRET = re.compile(r'class="secret"><code>([A-Za-z0-9]+)</code>')


class Env:
    def __init__(self, web_db: WebDb) -> None:
        self.db = web_db
        self.seeded: Seeded = seed(web_db.owner_url)
        self.clock = FakeClock()
        self.factory = make_client(web_db, self.clock)

    def manager_a(self) -> TestClient:
        c = self.factory()
        assert login(c, PHONE_A, PASSWORD_A).status_code == 303
        return c

    def manager_b(self) -> TestClient:
        c = self.factory()
        assert login(c, PHONE_B, PASSWORD_B).status_code == 303
        return c

    def staff_a(self) -> TestClient:
        c = self.factory()
        assert login(c, PHONE_STAFF_A, PASSWORD_S).status_code == 303
        return c

    def post(self, c: TestClient, path: str, **data: str) -> httpx.Response:
        token = csrf_for(c, "/staff")
        response: httpx.Response = c.post(
            path, data={**data, "csrf_token": token}, follow_redirects=False
        )
        return response

    def create(self, c: TestClient, name: str, phone: str, role: str = "staff") -> httpx.Response:
        return self.post(c, "/staff", display_name=name, phone=phone, role=role)

    def reissue(self, c: TestClient, user_id: int) -> httpx.Response:
        """Press the button: read the detail page's hidden field, then POST it back."""
        page = c.get(f"/staff/{user_id}")
        assert page.status_code == 200
        match = EXPECTED.search(page.text)
        assert match, "the re-issue form has no expected_temp_id field"
        return self.post(c, f"/staff/{user_id}/temp-password", expected_temp_id=match.group(1))

    def user(self, user_id: int, agency: int | None = None) -> User:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency or self.seeded.agency_a)
            return s.scalars(select(User).where(User.id == user_id)).one()

    def users(self, agency: int) -> list[User]:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency)
            return list(s.scalars(select(User).where(User.agency_id == agency).order_by(User.id)))

    def signs(self, agency: int) -> list[str]:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, agency)
            query = select(StaffSignLink.staff_sign).where(StaffSignLink.agency_id == agency)
            return sorted(s.scalars(query))


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def _plain(response: httpx.Response) -> str:
    return re.sub(
        r'name="csrf_token" value="[^"]+"|data-[a-z-]+=\'[^\']*\'|<code>[0-9A-F]{8}</code>',
        "",
        response.text,
    )


# --- access ---------------------------------------------------------------------------------------


def test_anonymous_is_sent_to_login(env: Env) -> None:
    with env.factory() as c:
        response = c.get("/staff", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_staff_gets_403_on_the_page_and_on_posts_and_it_is_audited(env: Env) -> None:
    c = env.staff_a()
    assert c.get("/staff").status_code == 403
    assert c.get(f"/staff/{env.seeded.manager_a}").status_code == 403
    token = csrf_for(c)
    response = c.post(
        "/staff",
        data={"display_name": "Intrus", "phone": NEW_PHONE, "role": "manager", "csrf_token": token},
    )
    assert response.status_code == 403
    assert len(env.users(env.seeded.agency_a)) == 2  # nothing was created
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert len(denied) == 3
    assert all(e.actor_user_id == env.seeded.staff_a for e in denied)


@pytest.mark.parametrize(
    "path", ["/staff", "/staff/1/role", "/staff/1/deactivate", "/staff/1/temp-password"]
)
def test_a_post_without_csrf_is_403(env: Env, path: str) -> None:
    c = env.manager_a()
    assert c.post(path, data={"role": "staff"}).status_code == 403
    assert c.post(path, data={"role": "staff", "csrf_token": "forged"}).status_code == 403


def test_the_list_shows_only_this_agency(env: Env) -> None:
    c = env.manager_a()
    env.post(c, f"/staff/{env.seeded.staff_a}/signs", sign="0004SV")
    page = c.get("/staff")
    assert page.status_code == 200
    assert "Manager A" in page.text
    assert "Staff A" in page.text
    assert PHONE_STAFF_A in page.text
    assert "0004SV" in page.text
    assert "Manager B" not in page.text
    assert PHONE_B not in page.text


def test_display_names_are_escaped(env: Env) -> None:
    c = env.manager_a()
    env.create(c, "<script>alert(1)</script>", NEW_PHONE)
    page = c.get("/staff")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


# --- creating -------------------------------------------------------------------------------------


def test_creating_a_user_shows_the_temporary_password_once(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    c = env.manager_a()
    with caplog.at_level(logging.DEBUG):
        response = env.create(c, "  Nouveau   Agent ", "12 34 56 50")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    match = SECRET.search(response.text)
    assert match
    plaintext = match.group(1)
    assert len(plaintext) == 12
    assert response.text.count(plaintext) == 1
    assert "location" not in response.headers

    user = next(u for u in env.users(env.seeded.agency_a) if u.phone == NEW_PHONE)
    assert user.display_name == "Nouveau Agent"
    assert user.role == "staff"
    assert user.password_hash is None
    assert user.must_change_password is True
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        (row,) = s.scalars(select(TemporaryPassword).where(TemporaryPassword.user_id == user.id))
    assert row.password_hash.startswith("$argon2id$")
    assert row.expires_at == env.clock() + timedelta(minutes=30)

    # never again: not on the next pages, not in the database, not in the logs
    assert plaintext not in c.get("/staff").text
    assert plaintext not in c.get(f"/staff/{user.id}").text
    assert plaintext not in database_text(env.db.owner_url, [env.seeded.agency_a])
    assert plaintext not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)

    created = audit_events(env.db.owner_url, env.seeded.agency_a, "user_created")
    issued = audit_events(env.db.owner_url, env.seeded.agency_a, "temp_password_issued")
    assert len(created) == 1
    assert created[0].details == {"user_id": user.id, "role": "staff", "phone": "+222******50"}
    assert len(issued) == 1
    assert issued[0].actor_user_id == env.seeded.manager_a

    # and the new person can log in with it, once, and must change it
    newcomer = env.factory()
    assert login(newcomer, NEW_PHONE, plaintext).status_code == 303
    assert newcomer.get("/", follow_redirects=False).headers["location"] == "/password/change"
    assert login(env.factory(), NEW_PHONE, plaintext).status_code == 401


def test_a_manager_can_create_another_manager(env: Env) -> None:
    c = env.manager_a()
    assert env.create(c, "Deuxième", NEW_PHONE, "manager").status_code == 200
    assert next(u for u in env.users(env.seeded.agency_a) if u.phone == NEW_PHONE).role == "manager"


@pytest.mark.parametrize(
    ("name", "phone", "role", "message"),
    [
        ("", NEW_PHONE, "staff", "Le nom est obligatoire"),
        ("   ", NEW_PHONE, "staff", "Le nom est obligatoire"),
        ("x" * 101, NEW_PHONE, "staff", "Le nom est obligatoire"),
        ("Bad\x00Name", NEW_PHONE, "staff", "Le nom est obligatoire"),
        ("Ok", "123", "staff", "pas valide"),
        ("Ok", NEW_PHONE, "admin", "rôle valide"),
        ("Ok", NEW_PHONE, "", "rôle valide"),
    ],
)
def test_invalid_forms_create_nothing_and_keep_what_was_typed(
    env: Env, name: str, phone: str, role: str, message: str
) -> None:
    c = env.manager_a()
    response = env.create(c, name, phone, role)
    assert response.status_code == 400
    assert message in response.text
    assert len(env.users(env.seeded.agency_a)) == 2
    if phone == "123":
        assert 'value="123"' in response.text


def test_duplicate_phone_is_a_generic_error_whichever_agency_has_it(env: Env) -> None:
    c = env.manager_a()
    in_my_agency = env.create(c, "Copie", "12 34 56 03")  # staff A, another spelling
    in_other_agency = env.create(c, "Copie", PHONE_B)  # manager B
    nowhere = env.create(c, "Copie", "+22212345651")
    assert in_my_agency.status_code == in_other_agency.status_code == 400
    assert _plain(in_my_agency).replace("12 34 56 03", "") == _plain(in_other_agency).replace(
        PHONE_B, ""
    )
    assert "Impossible de créer ce compte" in in_other_agency.text
    assert "déjà" not in in_other_agency.text
    assert PHONE_B not in in_other_agency.text.replace(f'value="{PHONE_B}"', "")
    assert nowhere.status_code == 200
    assert len(env.users(env.seeded.agency_a)) == 3
    assert len(env.users(env.seeded.agency_b)) == 1


# --- signs ----------------------------------------------------------------------------------------


def test_signs_are_normalised_linked_and_unlinked(env: Env) -> None:
    c = env.manager_a()
    uid = env.seeded.staff_a
    response = env.post(c, f"/staff/{uid}/signs", sign=" 0004sv ")
    assert response.status_code == 303
    assert response.headers["location"] == f"/staff/{uid}"
    assert env.signs(env.seeded.agency_a) == ["0004SV"]
    assert "0004SV" in c.get(f"/staff/{uid}").text
    link_id = re.search(r"/signs/(\d+)/unlink", c.get(f"/staff/{uid}").text)
    assert link_id
    assert env.post(c, f"/staff/{uid}/signs/{link_id.group(1)}/unlink").status_code == 303
    assert env.signs(env.seeded.agency_a) == []
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "staff_sign_linked")) == 1
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "staff_sign_unlinked")) == 1


@pytest.mark.parametrize("sign", ["", "A", "A B", "ÉCOLE", "A-1", "x" * 33, "<b>", "０００４SV"])
def test_bad_signs_are_a_form_error(env: Env, sign: str) -> None:
    c = env.manager_a()
    response = env.post(c, f"/staff/{env.seeded.staff_a}/signs", sign=sign)
    if sign == "０００４SV":  # full-width digits fold to ASCII (NFKC) and are accepted
        assert response.status_code == 303
        return
    assert response.status_code == 400
    assert "2 à 32 lettres ou chiffres" in response.text
    assert env.signs(env.seeded.agency_a) == []


def test_a_duplicate_sign_is_a_form_error_but_other_agencies_may_use_it(env: Env) -> None:
    a, b = env.manager_a(), env.manager_b()
    env.post(a, f"/staff/{env.seeded.staff_a}/signs", sign="0004SV")
    again = env.post(a, f"/staff/{env.seeded.manager_a}/signs", sign="0004sv")
    assert again.status_code == 400
    assert "déjà associé" in again.text
    assert env.post(b, f"/staff/{env.seeded.manager_b}/signs", sign="0004SV").status_code == 303
    assert env.signs(env.seeded.agency_a) == ["0004SV"]
    assert env.signs(env.seeded.agency_b) == ["0004SV"]


def test_unlinking_a_missing_or_foreign_link_is_404(env: Env) -> None:
    c = env.manager_a()
    env.post(c, f"/staff/{env.seeded.staff_a}/signs", sign="0004SV")
    link_id = re.search(r"/signs/(\d+)/unlink", c.get(f"/staff/{env.seeded.staff_a}").text)
    assert link_id
    wrong_user = env.post(c, f"/staff/{env.seeded.manager_a}/signs/{link_id.group(1)}/unlink")
    assert wrong_user.status_code == 404
    assert env.post(c, f"/staff/{env.seeded.staff_a}/signs/99999/unlink").status_code == 404
    assert env.signs(env.seeded.agency_a) == ["0004SV"]


# --- deactivating, roles and the last manager -----------------------------------------------------


def test_deactivating_revokes_sessions_and_blocks_login(env: Env) -> None:
    c, staff = env.manager_a(), env.staff_a()
    assert staff.get("/", follow_redirects=False).status_code == 200
    response = env.post(c, f"/staff/{env.seeded.staff_a}/deactivate")
    assert response.status_code == 303
    assert env.user(env.seeded.staff_a).is_active is False
    assert staff.get("/", follow_redirects=False).status_code == 303
    assert login(env.factory(), PHONE_STAFF_A, PASSWORD_S).status_code == 401
    assert "Désactivé" in c.get("/staff").text
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "user_deactivated")
    assert [e.details for e in events] == [{"user_id": env.seeded.staff_a}]


def test_the_last_active_manager_cannot_be_deactivated_or_demoted(env: Env) -> None:
    c = env.manager_a()
    me = env.seeded.manager_a
    deactivate = env.post(c, f"/staff/{me}/deactivate")
    demote = env.post(c, f"/staff/{me}/role", role="staff")
    assert deactivate.status_code == demote.status_code == 400
    assert "au moins un responsable actif" in deactivate.text
    assert "au moins un responsable actif" in demote.text
    user = env.user(me)
    assert user.is_active and user.role == "manager"


def test_with_a_second_manager_one_can_go_but_not_the_last(env: Env) -> None:
    c = env.manager_a()
    env.create(c, "Second", NEW_PHONE, "manager")
    second = next(u for u in env.users(env.seeded.agency_a) if u.phone == NEW_PHONE).id
    assert env.post(c, f"/staff/{second}/deactivate").status_code == 303  # not the last: fine
    assert env.post(c, f"/staff/{env.seeded.manager_a}/deactivate").status_code == 400


def test_an_inactive_manager_does_not_count_as_a_manager(env: Env) -> None:
    c = env.manager_a()
    env.create(c, "Second", NEW_PHONE, "manager")
    second = next(u for u in env.users(env.seeded.agency_a) if u.phone == NEW_PHONE).id
    env.post(c, f"/staff/{second}/deactivate")
    assert env.post(c, f"/staff/{env.seeded.manager_a}/role", role="staff").status_code == 400


def test_role_changes(env: Env) -> None:
    c = env.manager_a()
    assert env.post(c, f"/staff/{env.seeded.staff_a}/role", role="manager").status_code == 303
    assert env.user(env.seeded.staff_a).role == "manager"
    assert env.post(c, f"/staff/{env.seeded.staff_a}/role", role="root").status_code == 400
    assert env.post(c, f"/staff/{env.seeded.manager_a}/role", role="staff").status_code == 303
    # a demoted manager lost the page at once
    assert c.get("/staff").status_code == 403
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "user_role_changed")
    assert [(e.details["from"], e.details["to"]) for e in events] == [
        ("staff", "manager"),
        ("manager", "staff"),
    ]


# --- re-issuing -----------------------------------------------------------------------------------


def test_a_manager_can_reissue_a_temporary_password_for_staff(env: Env) -> None:
    c, staff = env.manager_a(), env.staff_a()
    response = env.reissue(c, env.seeded.staff_a)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    match = SECRET.search(response.text)
    assert match
    assert staff.get("/", follow_redirects=False).status_code == 303  # sessions revoked
    assert login(env.factory(), PHONE_STAFF_A, PASSWORD_S).status_code == 401
    assert login(env.factory(), PHONE_STAFF_A, match.group(1)).status_code == 303


def test_a_reload_of_the_reissue_post_does_not_silently_issue_another(env: Env) -> None:
    c = env.manager_a()
    uid = env.seeded.staff_a
    page = c.get(f"/staff/{uid}")
    expected = EXPECTED.search(page.text)
    assert expected
    first = env.post(c, f"/staff/{uid}/temp-password", expected_temp_id=expected.group(1))
    secret = SECRET.search(first.text)
    assert first.status_code == 200 and secret
    # the browser's "resend the form" replays the very same POST
    replay = env.post(c, f"/staff/{uid}/temp-password", expected_temp_id=expected.group(1))
    assert replay.status_code == 400
    assert "déjà été émis" in replay.text
    assert SECRET.search(replay.text) is None
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        rows = list(s.scalars(select(TemporaryPassword).where(TemporaryPassword.user_id == uid)))
    assert len(rows) == 1  # nothing was reissued: the first password is still the live one
    assert login(env.factory(), PHONE_STAFF_A, secret.group(1)).status_code == 303
    # a form that does not carry the field is refused as well (nothing is issued blindly)
    assert env.post(c, f"/staff/{uid}/temp-password").status_code == 400


def test_a_second_deliberate_reissue_from_a_fresh_page_works(env: Env) -> None:
    c = env.manager_a()
    first = SECRET.search(env.reissue(c, env.seeded.staff_a).text)
    second = SECRET.search(env.reissue(c, env.seeded.staff_a).text)
    assert first and second and first.group(1) != second.group(1)


def test_a_manager_cannot_reset_another_manager_or_themselves(env: Env) -> None:
    c = env.manager_a()
    env.create(c, "Second", NEW_PHONE, "manager")
    second = next(u for u in env.users(env.seeded.agency_a) if u.phone == NEW_PHONE).id
    before_second = env.user(second).password_hash
    for target in (second, env.seeded.manager_a):
        page = c.get(f"/staff/{target}")
        assert "expected_temp_id" not in page.text  # no button to press
        response = env.post(c, f"/staff/{target}/temp-password", expected_temp_id="0")
        assert response.status_code == 403
        assert "Accès refusé" in response.text
    assert env.user(second).password_hash == before_second
    assert env.user(env.seeded.manager_a).password_hash is not None
    denied = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert len(denied) == 2
    assert {e.actor_user_id for e in denied} == {env.seeded.manager_a}
    assert denied[0].details["path"] == f"/staff/{second}/temp-password"
    # the manager is still logged in and nothing was issued to them
    assert c.get("/", follow_redirects=False).status_code == 200
    issued = audit_events(env.db.owner_url, env.seeded.agency_a, "temp_password_issued")
    assert [e.details["user_id"] for e in issued] == [second]  # only the one made at creation


def test_reissue_for_a_deactivated_user_is_refused(env: Env) -> None:
    c = env.manager_a()
    page = c.get(f"/staff/{env.seeded.staff_a}")
    expected = EXPECTED.search(page.text)
    assert expected
    env.post(c, f"/staff/{env.seeded.staff_a}/deactivate")
    gone = env.post(
        c, f"/staff/{env.seeded.staff_a}/temp-password", expected_temp_id=expected.group(1)
    )
    assert gone.status_code == 400
    assert "désactivé" in gone.text


def test_the_target_is_locked_before_a_temporary_password_is_issued() -> None:
    from sqlalchemy.dialects import postgresql

    from app import accounts

    statement = accounts.user_query(1, 2, lock=True)
    sql = str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[no-untyped-call]
    assert "FOR UPDATE" in sql
    assert "FOR UPDATE" not in str(
        accounts.user_query(1, 2).compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
    )


# --- another agency's ids are invisible -----------------------------------------------------


def test_manager_b_gets_404_for_every_action_on_agency_a_ids(env: Env) -> None:
    b = env.manager_b()
    uid = env.seeded.staff_a
    env.post(env.manager_a(), f"/staff/{uid}/signs", sign="0004SV")
    link_id = re.search(r"/signs/(\d+)/unlink", env.manager_a().get(f"/staff/{uid}").text)
    assert link_id

    assert b.get(f"/staff/{uid}").status_code == 404
    assert b.get(f"/staff/{env.seeded.manager_a}").status_code == 404
    attempts = [
        (f"/staff/{uid}/signs", {"sign": "ZZ99"}),
        (f"/staff/{uid}/signs/{link_id.group(1)}/unlink", {}),
        (f"/staff/{uid}/role", {"role": "manager"}),
        (f"/staff/{uid}/deactivate", {}),
        (f"/staff/{uid}/temp-password", {}),
        (f"/staff/{env.seeded.manager_a}/deactivate", {}),
    ]
    for path, data in attempts:
        response = env.post(b, path, **data)  # B's own, valid CSRF token
        assert response.status_code == 404, path
        assert "Page introuvable" in response.text

    # the same link id used through B's own user is also invisible
    through_own = env.post(b, f"/staff/{env.seeded.manager_b}/signs/{link_id.group(1)}/unlink")
    assert through_own.status_code == 404

    victim = env.user(uid)
    assert victim.is_active and victim.role == "staff" and victim.password_hash is not None
    assert env.signs(env.seeded.agency_a) == ["0004SV"]
    assert env.signs(env.seeded.agency_b) == []
    assert env.user(env.seeded.manager_a).is_active


def test_a_missing_id_looks_the_same_as_another_agencys_id(env: Env) -> None:
    b = env.manager_b()
    foreign = b.get(f"/staff/{env.seeded.staff_a}")
    missing = b.get("/staff/987654")
    assert foreign.status_code == missing.status_code == 404
    assert _plain(foreign) == _plain(missing)


OUT_OF_RANGE = [0, -1, 2**63, 2**64, 10**30]


@pytest.mark.parametrize("bad", OUT_OF_RANGE)
def test_out_of_range_ids_are_the_404_page_before_any_query(
    env: Env, monkeypatch: pytest.MonkeyPatch, bad: int
) -> None:
    from app import accounts

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("the database was queried for an impossible id")

    c = env.manager_a()
    monkeypatch.setattr(accounts, "get_user", boom)
    monkeypatch.setattr(accounts, "unlink_sign", boom)
    page = c.get(f"/staff/{bad}")
    assert page.status_code == 404
    assert "Page introuvable" in page.text
    for path in (
        f"/staff/{bad}/signs",
        f"/staff/{bad}/role",
        f"/staff/{bad}/deactivate",
        f"/staff/{bad}/temp-password",
        f"/staff/{env.seeded.staff_a}/signs/{bad}/unlink",
        f"/staff/{bad}/signs/{bad}/unlink",
    ):
        assert env.post(c, path).status_code == 404, path


def test_the_largest_id_is_an_ordinary_not_found(env: Env) -> None:
    c = env.manager_a()
    assert c.get(f"/staff/{2**63 - 1}").status_code == 404
    assert env.post(c, f"/staff/{env.seeded.staff_a}/signs/{2**63 - 1}/unlink").status_code == 404


def test_non_numeric_ids_are_a_clean_400(env: Env) -> None:
    c = env.manager_a()
    assert c.get("/staff/abc").status_code == 400


def test_creating_in_a_new_agency_does_not_touch_the_other(env: Env) -> None:
    env.create(env.manager_b(), "Agent B", NEW_PHONE)
    assert [u.phone for u in env.users(env.seeded.agency_b)] == [PHONE_B, NEW_PHONE]
    assert NEW_PHONE not in [u.phone for u in env.users(env.seeded.agency_a)]
    assert add_user is not None
