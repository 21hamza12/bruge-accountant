"""« Coordonnées de l'agence » (phase F, slice F3): the manager edits, the accountant reads, staff
get a 403; agency-bound, CSRF-protected, rate-limited, idempotent form, audited without values."""

from __future__ import annotations

import json
import re

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import agency_details, partner_book
from app.agency_details import DetailsError, DetailsForbidden
from app.auth import ratelimit
from app.models import AGENCY_DETAIL_LIMITS, ROLE_ACCOUNTANT, AgencyDetails, User
from app.tenancy import rebind_tenant
from tests.page_support import Env
from tests.web_support import (
    PASSWORD_S,
    T0,
    WebDb,
    add_user,
    audit_events,
    csrf_for,
    owner_session,
)

ACCOUNTANT_PHONE = "+22212345692"
PATH = "/agency-details"
_KEY = re.compile(r'name="request_key" value="([^"]+)"')

DETAILS = {
    "name_fr": "Agence Test",
    "name_ar": "وكالة تجريبية",
    "address": "Rue Test, Nouakchott",
    "phones": "+222 45 00 00 00 / 22 00 00 00",
    "email": "contact@agence-test.example",
    "nif": "00000000",
    "bank_name": "Banque Test",
    "bank_account": "00000 00000 00000000000 00",
    "bankily": "22000000",
    "masrvi": "22000001",
    "sedad": "22000002",
}


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def add_accountant(env: Env) -> int:
    return add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        ACCOUNTANT_PHONE,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )


def accountant(env: Env) -> TestClient:
    return env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)


def stored(env: Env, agency_id: int | None = None) -> AgencyDetails | None:
    agency = agency_id or env.seeded.agency_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, agency)
        row = s.scalars(select(AgencyDetails).where(AgencyDetails.agency_id == agency)).first()
        if row is not None:
            s.expunge(row)
        return row


def form_key(c: TestClient) -> tuple[str, str]:
    page = c.get(PATH)
    assert page.status_code == 200, page.text
    match = _KEY.search(page.text)
    assert match, "no request_key on the page"
    return csrf_for(c, PATH), match.group(1)


def save(c: TestClient, data: dict[str, str]) -> httpx.Response:
    token, key = form_key(c)
    response: httpx.Response = c.post(
        PATH, data={"request_key": key, **data, "csrf_token": token}, follow_redirects=False
    )
    return response


def user(s: object, user_id: int) -> User:
    found = s.scalars(select(User).where(User.id == user_id)).one()  # type: ignore[attr-defined]
    assert isinstance(found, User)
    return found


# --- who may ----------------------------------------------------------------------------------


def test_the_routes_need_the_reader_or_the_writer_dependency() -> None:
    from fastapi.routing import APIRoute

    from app.web.routes import agency_details as routes

    found = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    assert len(found) == 2
    for route in found:
        calls = [d.call for d in route.dependant.dependencies]
        if "POST" in (route.methods or set()):
            assert routes.require_details_writer in calls
        else:
            assert routes.require_details_reader in calls
    assert routes.require_details_reader.__name__ == "require_can_view_agency_details"
    assert routes.require_details_writer.__name__ == "require_can_edit_agency_details"


def test_the_manager_sees_the_form_without_inline_style_or_script(env: Env) -> None:
    response = env.manager_a().get(PATH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    assert "Coordonnées de l'agence" in html
    assert f'action="{PATH}"' in html and 'name="csrf_token"' in html
    assert 'name="request_key"' in html
    for name in AGENCY_DETAIL_LIMITS:
        assert f'name="{name}"' in html, name
    assert not re.search(r"\sstyle\s*=", html, re.I) and "<style" not in html.lower()
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


def test_the_accountant_reads_the_details_but_has_no_form(env: Env) -> None:
    add_accountant(env)
    assert save(env.manager_a(), DETAILS).status_code == 303
    response = accountant(env).get(PATH)
    assert response.status_code == 200
    html = response.text
    assert "Agence Test" in html and "00000000" in html and "Banque Test" in html
    assert f'action="{PATH}"' not in html and 'name="request_key"' not in html
    assert "Seul le gérant modifie ces coordonnées." in html


def test_the_accountant_cannot_save_and_nothing_is_written(env: Env) -> None:
    acc_id = add_accountant(env)
    c = accountant(env)
    response = c.post(
        PATH,
        data={
            **DETAILS,
            "request_key": partner_book.new_request_key(),
            "csrf_token": csrf_for(c),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert event.actor_user_id == acc_id and event.details["path"] == PATH
    assert stored(env) is None


@pytest.mark.parametrize("method", ["get", "post"])
def test_staff_get_403_and_an_audit_event(env: Env, method: str) -> None:
    assert save(env.manager_a(), DETAILS).status_code == 303
    c = env.staff_a()
    if method == "get":
        response = c.get(PATH)
    else:
        response = c.post(
            PATH,
            data={
                **DETAILS,
                "name_fr": "Changé",
                "request_key": partner_book.new_request_key(),
                "csrf_token": csrf_for(c),
            },
            follow_redirects=False,
        )
    assert response.status_code == 403
    assert "Agence Test" not in response.text
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert event.actor_user_id == env.seeded.staff_a
    row = stored(env)
    assert row is not None and row.name_fr == "Agence Test"


def test_the_service_refuses_the_accountant_and_audits_it(env: Env) -> None:
    acc_id = add_accountant(env)
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        with pytest.raises(DetailsForbidden):
            agency_details.save_details(
                s, user(s, acc_id), DETAILS, request_key=partner_book.new_request_key(), now=T0
            )
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert event.details == {"action": "save_agency_details"}
    assert stored(env) is None


def test_the_sidebar_link_is_for_manager_and_accountant_only(env: Env) -> None:
    add_accountant(env)
    for client in (env.manager_a(), accountant(env)):
        html = client.get("/").text
        assert f'href="{PATH}"' in html and "Coordonnées de l'agence" in html
    staff = env.staff_a().get("/").text
    assert f'href="{PATH}"' not in staff


# --- saving -----------------------------------------------------------------------------------


def test_the_manager_saves_every_field_and_the_audit_names_fields_only(env: Env) -> None:
    m = env.manager_a()
    response = save(m, DETAILS)
    assert response.status_code == 303
    assert response.headers["location"] == f"{PATH}?enregistre=1"
    row = stored(env)
    assert row is not None
    assert {name: getattr(row, name) for name in AGENCY_DETAIL_LIMITS} == DETAILS
    assert row.updated_by_user_id == env.seeded.manager_a
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "agency_details_saved")
    assert event.details == {"fields": sorted(DETAILS)}
    dumped = json.dumps(event.details, ensure_ascii=False)
    for value in DETAILS.values():
        assert value not in dumped
    page = m.get(response.headers["location"]).text
    assert "Coordonnées enregistrées." in page and 'value="Agence Test"' in page


def test_a_second_save_audits_only_the_fields_that_changed(env: Env) -> None:
    m = env.manager_a()
    assert save(m, DETAILS).status_code == 303
    changed = {**DETAILS, "phones": "+222 45 11 11 11", "sedad": ""}
    assert save(m, changed).status_code == 303
    row = stored(env)
    assert row is not None and row.phones == "+222 45 11 11 11" and row.sedad is None
    events = audit_events(env.db.owner_url, env.seeded.agency_a, "agency_details_saved")
    assert [e.details for e in events] == [
        {"fields": sorted(DETAILS)},
        {"fields": ["phones", "sedad"]},
    ]
    assert "45 11 11 11" not in json.dumps(events[1].details)


def test_saving_the_same_values_writes_nothing(env: Env) -> None:
    m = env.manager_a()
    assert save(m, DETAILS).status_code == 303
    again = save(m, DETAILS)
    assert again.status_code == 200 and "Aucune modification" in again.text
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "agency_details_saved")) == 1


def test_a_double_click_saves_once(env: Env) -> None:
    m = env.manager_a()
    token, key = form_key(m)
    data = {**DETAILS, "request_key": key, "csrf_token": token}
    first = m.post(PATH, data=data, follow_redirects=False)
    second = m.post(PATH, data=data, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "agency_details_saved")) == 1


def test_a_key_replayed_with_other_values_comes_back_with_a_fresh_key(env: Env) -> None:
    m = env.manager_a()
    token, key = form_key(m)
    data = {**DETAILS, "request_key": key, "csrf_token": token}
    assert m.post(PATH, data=data, follow_redirects=False).status_code == 303
    changed = m.post(PATH, data={**data, "nif": "11111111"}, follow_redirects=False)
    assert changed.status_code == 400
    assert "Ce formulaire a déjà été envoyé avec" in changed.text
    match = _KEY.search(changed.text)
    assert match and match.group(1) != key
    assert 'value="11111111"' in changed.text
    row = stored(env)
    assert row is not None and row.nif == "00000000"


def test_every_field_is_optional(env: Env) -> None:
    m = env.manager_a()
    assert save(m, {"name_fr": "Agence Test"}).status_code == 303
    row = stored(env)
    assert row is not None and row.name_fr == "Agence Test"
    assert all(getattr(row, n) is None for n in AGENCY_DETAIL_LIMITS if n != "name_fr")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("phones", "appelez-nous", "Écrivez les numéros de téléphone"),
        ("phones", "12", "Écrivez les numéros de téléphone"),
        ("phones", "+222 45 00 00 00 / abc", "Écrivez les numéros de téléphone"),
        ("email", "pas-une-adresse", "Saisissez une adresse e-mail valide"),
        ("email", "a@b", "Saisissez une adresse e-mail valide"),
        ("bankily", "=22000000", "Bankily"),
        ("masrvi", "22-00-00 x", "Masrvi"),
        ("name_fr", "x" * 121, "ne peut pas dépasser 120 caractères"),
        ("address", "x" * 301, "ne peut pas dépasser 300 caractères"),
    ],
)
def test_bad_values_are_refused_in_french_and_nothing_is_saved(
    env: Env, field: str, value: str, message: str
) -> None:
    response = save(env.manager_a(), {**DETAILS, field: value})
    assert response.status_code == 400
    assert message in response.text
    assert stored(env) is None


def test_phones_are_validated_loosely(env: Env) -> None:
    for phones in ("22000000", "+222 45 00 00 00", "(+222) 45-00-00-00; 22.00.00.00"):
        with owner_session(env.db.owner_url) as s:
            rebind_tenant(s, env.seeded.agency_a)
            assert agency_details.clean_values({"phones": phones})["phones"] == phones


def test_text_is_cleaned_and_markup_is_escaped_on_the_page(env: Env) -> None:
    m = env.manager_a()
    raw = "  <b>Agence</b>‮   Test\n "
    assert save(m, {"name_fr": raw}).status_code == 303
    row = stored(env)
    assert row is not None and row.name_fr == "<b>Agence</b> Test"
    html = m.get(PATH).text
    assert "&lt;b&gt;Agence&lt;/b&gt; Test" in html and "<b>Agence</b>" not in html


def test_a_post_without_the_csrf_token_is_refused(env: Env) -> None:
    m = env.manager_a()
    response = m.post(
        PATH,
        data={**DETAILS, "request_key": partner_book.new_request_key()},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert stored(env) is None


def test_a_post_without_a_request_key_is_refused(env: Env) -> None:
    m = env.manager_a()
    response = m.post(PATH, data={**DETAILS, "csrf_token": csrf_for(m)}, follow_redirects=False)
    assert response.status_code == 400 and "rechargez la page" in response.text
    assert stored(env) is None


def test_saves_are_rate_limited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ratelimit,
        "AGENCY_DETAILS_WRITE_USER",
        ratelimit.Limit("agency_details_write_user", 2, 3600),
    )
    m = env.manager_a()
    codes = [save(m, {**DETAILS, "nif": f"0000000{n}"}).status_code for n in range(3)]
    assert codes == [303, 303, 429]
    row = stored(env)
    assert row is not None and row.nif == "00000001"
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "rate_limited")
    assert event.details == {"scope": "agency_details_write_user"}


# --- isolation --------------------------------------------------------------------------------


def test_each_agency_has_its_own_details(env: Env) -> None:
    assert save(env.manager_a(), DETAILS).status_code == 303
    b = env.manager_b()
    page = b.get(PATH).text
    assert "Agence Test" not in page and "00000000" not in page
    assert save(b, {"name_fr": "Agence Test B", "nif": "99999999"}).status_code == 303
    row_a, row_b = stored(env), stored(env, env.seeded.agency_b)
    assert row_a is not None and row_a.name_fr == "Agence Test" and row_a.nif == "00000000"
    assert row_b is not None and row_b.name_fr == "Agence Test B"
    assert "Agence Test B" not in env.manager_a().get(PATH).text


def test_the_service_reads_only_the_given_agency(env: Env) -> None:
    assert save(env.manager_a(), DETAILS).status_code == 303
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_b)
        assert agency_details.get_details(s, env.seeded.agency_b) is None


# --- the header of a document -----------------------------------------------------------------


def test_the_header_leaves_out_what_is_missing() -> None:
    row = AgencyDetails(agency_id=1, name_fr="Agence Test", nif="00000000", sedad="22000002")
    assert agency_details.header_lines(row, "AGENCE-A") == [
        "Agence Test",
        "NIF : 00000000",
        "Sedad : 22000002",
    ]


def test_the_header_of_complete_details() -> None:
    row = AgencyDetails(agency_id=1, **DETAILS)
    assert agency_details.header_lines(row, "AGENCE-A") == [
        "Agence Test",
        "وكالة تجريبية",
        "Rue Test, Nouakchott",
        "Tél. : +222 45 00 00 00 / 22 00 00 00 · E-mail : contact@agence-test.example",
        "NIF : 00000000",
        "Banque : Banque Test · RIB : 00000 00000 00000000000 00",
        "Bankily : 22000000 · Masrvi : 22000001 · Sedad : 22000002",
    ]


def test_without_details_the_header_is_the_agency_name() -> None:
    assert agency_details.header_lines(None, "AGENCE-A") == ["AGENCE-A"]


def test_the_error_class_is_a_user_error() -> None:
    from app.errors import UserError

    assert issubclass(DetailsError, UserError) and issubclass(DetailsForbidden, DetailsError)
