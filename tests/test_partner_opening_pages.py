"""The switch-over pages (phase F, slice F5): the opening-balance form on a partner's account,
« Soldes d'ouverture » (one form per partner still without one) and « Vérification du passage ».
Manager enters, accountant checks, staff get 403; agency-bound; CSRF; rate-limited; request keys;
no inline style or script."""

from __future__ import annotations

import re
from decimal import Decimal

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from app import partner_book
from app.auth import ratelimit
from app.models import PartnerEntry
from tests.page_support import Env
from tests.test_partner_account_pages import (
    NNBSP,
    accountant,
    add_accountant,
    close_days,
    form_key,
    rows,
    setup_partner,
)
from tests.web_support import WebDb, audit_events, csrf_for

OPENING = {
    "amount": "50 000",
    "sign": "partenaire_doit",
    "switch_date": "2026-10-16",
    "reason": "solde du fichier Excel au 15/10",
}
GETS = ["/partner-openings", "/partner-openings/check", "/partner-openings/{p}/new"]
MANAGER_GETS = ["/partner-openings", "/partner-openings/{p}/new"]


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def post_opening(
    c: TestClient, pid: int, data: dict[str, str] | None = None, via: str | None = None
) -> httpx.Response:
    path = via or f"/partner-accounts/{pid}"
    token, key = form_key(c, path)
    response: httpx.Response = c.post(
        f"/partner-openings/{pid}",
        data={"request_key": key, **(data or OPENING), "csrf_token": token},
        follow_redirects=False,
    )
    return response


def openings(env: Env) -> list[PartnerEntry]:
    return [e for e in rows(env, PartnerEntry) if e.kind == "OPENING"]


def assert_clean(html: str) -> None:
    assert '<html lang="fr"' in html
    assert not re.search(r"\sstyle\s*=", html, re.I) and "<style" not in html.lower()
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


# --- who may ----------------------------------------------------------------------------------


def test_every_route_carries_its_permission() -> None:
    from fastapi.routing import APIRoute

    from app.web.routes import partner_openings as routes

    found = {
        (r.path, m): [d.call for d in r.dependant.dependencies]
        for r in routes.router.routes
        if isinstance(r, APIRoute)
        for m in (r.methods or set())
    }
    assert set(found) == {
        ("/partner-openings", "GET"),
        ("/partner-openings/check", "GET"),
        ("/partner-openings/check", "POST"),
        ("/partner-openings/{partner_id}/new", "GET"),
        ("/partner-openings/{partner_id}", "POST"),
    }
    for (path, _method), calls in found.items():
        expected = routes.require_checker if "/check" in path else routes.require_opener
        assert expected in calls, path
    assert routes.require_opener.__name__ == "require_can_enter_partner_opening"
    assert routes.require_checker.__name__ == "require_can_check_partner_switchover"


@pytest.mark.parametrize("path", GETS)
def test_the_manager_opens_every_page_without_inline_code(env: Env, path: str) -> None:
    pid = setup_partner(env)
    response = env.manager_a().get(path.format(p=pid))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert_clean(response.text)


def test_the_accountant_views_the_check_page_but_cannot_enter(env: Env) -> None:
    pid = setup_partner(env)
    add_accountant(env)
    c = accountant(env)
    assert c.get("/partner-openings/check").status_code == 200
    for path in MANAGER_GETS:
        assert c.get(path.format(p=pid)).status_code == 403
    assert "Saisir le solde d'ouverture" not in c.get(f"/partner-accounts/{pid}").text
    forged = c.post(
        f"/partner-openings/{pid}",
        data={**OPENING, "request_key": partner_book.new_request_key(), "csrf_token": csrf_for(c)},
        follow_redirects=False,
    )
    assert forged.status_code == 403
    assert openings(env) == []
    assert audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")


@pytest.mark.parametrize("path", GETS)
def test_staff_get_403(env: Env, path: str) -> None:
    pid = setup_partner(env)
    response = env.staff_a().get(path.format(p=pid))
    assert response.status_code == 403
    assert "Partenaire Test" not in response.text


@pytest.mark.parametrize("path", ["/partner-openings/{p}", "/partner-openings/check"])
def test_staff_posts_are_refused(env: Env, path: str) -> None:
    pid = setup_partner(env)
    c = env.staff_a()
    response = c.post(
        path.format(p=pid),
        data={
            **OPENING,
            "partner_id": str(pid),
            "request_key": partner_book.new_request_key(),
            "csrf_token": csrf_for(c),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert openings(env) == []


def test_another_agency_cannot_see_or_open_a_partner(env: Env) -> None:
    pid = setup_partner(env)
    b = env.manager_b()
    assert b.get(f"/partner-openings/{pid}/new").status_code == 404
    response = b.post(
        f"/partner-openings/{pid}",
        data={**OPENING, "request_key": partner_book.new_request_key(), "csrf_token": csrf_for(b)},
        follow_redirects=False,
    )
    assert response.status_code == 404 and "Partenaire Test" not in response.text
    assert openings(env) == []
    assert "Partenaire Test" not in b.get("/partner-openings").text


def test_another_agency_does_not_see_the_check_row(env: Env) -> None:
    pid = setup_partner(env)
    assert post_opening(env.manager_a(), pid).status_code == 303
    b = env.manager_b()
    assert "Partenaire Test" not in b.get("/partner-openings/check").text
    compared = b.post(
        "/partner-openings/check",
        data={
            "partner_id": str(pid),
            "amount": "1",
            "sign": "partenaire_doit",
            "csrf_token": csrf_for(b),
        },
        follow_redirects=False,
    )
    assert compared.status_code == 404 and "Partenaire Test" not in compared.text


def test_a_post_without_the_csrf_token_is_refused(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    response = m.post(
        f"/partner-openings/{pid}",
        data={**OPENING, "request_key": partner_book.new_request_key()},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert openings(env) == []


# --- entering it ------------------------------------------------------------------------------


def test_the_form_is_on_the_account_page_only_while_there_is_no_opening(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    page = m.get(f"/partner-accounts/{pid}").text
    assert "Saisir le solde d'ouverture" in page
    assert "se corrige par un ajustement" in page
    response = post_opening(m, pid)
    assert response.status_code == 303
    assert response.headers["location"] == f"/partner-accounts/{pid}"
    [row] = openings(env)
    assert (row.amount, row.entry_date.isoformat()) == (Decimal("50000.00"), "2026-10-16")
    page = m.get(f"/partner-accounts/{pid}").text
    assert "Saisir le solde d'ouverture" not in page
    assert f"50{NNBSP}000" in page and "16 octobre 2026" in page
    assert "se corrige par un ajustement" in page
    [event] = audit_events(env.db.owner_url, env.seeded.agency_a, "partner_opening_entered")
    assert event.details == {"partner_id": pid, "entry_id": row.id, "amount": "50000.00"}


def test_the_same_form_posted_twice_writes_once(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    token, key = form_key(m, f"/partner-accounts/{pid}")
    data = {**OPENING, "request_key": key, "csrf_token": token}
    first = m.post(f"/partner-openings/{pid}", data=data, follow_redirects=False)
    second = m.post(f"/partner-openings/{pid}", data=data, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert second.headers["location"] == f"/partner-accounts/{pid}?deja=ouverture"
    assert "déjà enregistré" in m.get(second.headers["location"]).text
    assert len(openings(env)) == 1
    changed = m.post(
        f"/partner-openings/{pid}",
        data={**data, "amount": "60000"},
        follow_redirects=False,
    )
    assert changed.status_code == 400 and "autres valeurs" in changed.text
    assert key not in changed.text  # a fresh key for the corrected form
    assert len(openings(env)) == 1


def test_a_second_opening_is_refused_in_french(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    assert post_opening(m, pid).status_code == 303
    again = post_opening(m, pid, via=f"/partner-openings/{pid}/new")
    assert again.status_code == 409
    assert "déjà un solde d" in again.text
    assert len(openings(env)) == 1


def test_an_opening_after_a_closed_period_is_refused_in_french(env: Env) -> None:
    pid = setup_partner(env)
    close_days(env)
    m = env.manager_a()
    token, key = form_key(m, f"/partner-accounts/{pid}/periods/2026-10-01/close")
    closed = m.post(
        f"/partner-accounts/{pid}/periods/2026-10-01/close",
        data={"request_key": key, "csrf_token": token},
        follow_redirects=False,
    )
    assert closed.status_code == 303
    assert "Saisir le solde d'ouverture" not in m.get(f"/partner-accounts/{pid}").text
    response = post_opening(m, pid, via=f"/partner-openings/{pid}/new")
    assert response.status_code == 409
    assert "déjà clôturée" in response.text and "ajustement" in response.text
    assert openings(env) == []


def test_a_future_date_is_refused_and_the_form_keeps_its_values(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    response = post_opening(m, pid, {**OPENING, "switch_date": "2026-10-19"})
    assert response.status_code == 400
    assert "futur" in response.text
    assert 'value="50 000"' in response.text and "solde du fichier Excel" in response.text
    assert_clean(response.text)
    assert openings(env) == []


def test_zero_and_negative_openings(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    response = post_opening(m, pid, {**OPENING, "amount": "12 500", "sign": "agence_doit"})
    assert response.status_code == 303
    [row] = openings(env)
    assert row.amount == Decimal("-12500.00")


def test_a_zero_opening_through_the_page(env: Env) -> None:
    pid = setup_partner(env)
    response = post_opening(env.manager_a(), pid, {**OPENING, "amount": "0", "sign": ""})
    assert response.status_code == 303
    [row] = openings(env)
    assert row.amount == Decimal("0.00")


def test_openings_are_rate_limited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ratelimit,
        "PARTNER_OPENING_WRITE_USER",
        ratelimit.Limit("partner_opening_write_user", 1, 3600),
    )
    pid = setup_partner(env)
    m = env.manager_a()
    assert post_opening(m, pid, {**OPENING, "amount": "x"}).status_code == 400
    assert post_opening(m, pid, via=f"/partner-openings/{pid}/new").status_code == 429
    assert openings(env) == []


# --- the bulk page ----------------------------------------------------------------------------


def test_the_bulk_page_lists_partners_without_an_opening_and_posts_one_at_a_time(
    env: Env,
) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    page = m.get("/partner-openings").text
    assert "Partenaire Test" in page
    response = post_opening(m, pid, {**OPENING, "back": "liste"}, via="/partner-openings")
    assert response.status_code == 303
    assert response.headers["location"] == "/partner-openings?saisi=1"
    page = m.get(response.headers["location"]).text
    assert "Partenaire Test" not in page and "ouverture enregistré" in page
    assert len(openings(env)) == 1


# --- the check page ---------------------------------------------------------------------------


def test_the_check_page_shows_the_balance_and_the_difference(env: Env) -> None:
    """The sale of the 3rd is before the switch-over (16th): left out. The first half-month
    (16-31 Oct) ends at the opening alone: 50 000."""
    pid = setup_partner(env)
    add_accountant(env)
    assert post_opening(env.manager_a(), pid).status_code == 303
    c = accountant(env)
    page = c.get("/partner-openings/check").text
    assert "Vérification du passage" in page and "Partenaire Test" in page
    assert "31 octobre 2026" in page and f"50{NNBSP}000" in page
    token = csrf_for(c, "/partner-openings/check")
    same = c.post(
        "/partner-openings/check",
        data={"partner_id": str(pid), "amount": "50000", "sign": "partenaire_doit",
              "csrf_token": token},
    )  # fmt: skip
    assert same.status_code == 200 and "Identique" in same.text
    assert_clean(same.text)
    off = c.post(
        "/partner-openings/check",
        data={"partner_id": str(pid), "amount": "49 700", "sign": "partenaire_doit",
              "csrf_token": token},
    )  # fmt: skip
    assert off.status_code == 200 and "Écart de 300 MRU" in off.text
    bad = c.post(
        "/partner-openings/check",
        data={"partner_id": str(pid), "amount": "abc", "sign": "partenaire_doit",
              "csrf_token": token},
    )  # fmt: skip
    assert bad.status_code == 400 and "error-box" in bad.text
    # Nothing is stored and nothing is audited by a comparison.
    assert len(openings(env)) == 1
    assert len(rows(env, PartnerEntry)) == 1
    types = {e.event_type for e in audit_events(env.db.owner_url, env.seeded.agency_a)}
    assert not any("compar" in t or "check" in t for t in types)
