"""The partner account pages (phase F, slice F2): manager and accountant only, agency-bound,
CSRF-protected, rate-limited, idempotent forms, French refusals, no inline style or script."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import partner_book, partners, sales
from app.auth import ratelimit
from app.models import (
    ROLE_ACCOUNTANT,
    PartnerEntry,
    PartnerPayment,
    PartnerPeriod,
    ReportDay,
    User,
)
from app.sales import SaleInput, TicketInput
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

ACCOUNTANT_PHONE = "+22212345691"
OCT_1, OCT_3 = date(2026, 10, 1), date(2026, 10, 3)
NNBSP = " "  # app.formatting.NARROW_NBSP
_KEY = re.compile(r'name="request_key" value="([^"]+)"')


@pytest.fixture
def env(web_db: WebDb) -> Env:
    return Env(web_db)


def accountant(env: Env) -> TestClient:
    return env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)


def add_accountant(env: Env) -> int:
    return add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        ACCOUNTANT_PHONE,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )


def _user(s: object, user_id: int) -> User:
    found = s.scalars(select(User).where(User.id == user_id)).one()  # type: ignore[attr-defined]
    assert isinstance(found, User)
    return found


def setup_partner(env: Env, agency_id: int | None = None, manager_id: int | None = None) -> int:
    """A partner ("Partenaire Test", fee 300) with one sale of 21 368 on 3 October."""
    agency = agency_id or env.seeded.agency_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, agency)
        manager = _user(s, manager_id or env.seeded.manager_a)
        partner = partners.create_partner(s, manager, "Partenaire Test", now=T0)
        partners.set_fee(s, partner, Decimal("300"), date(2026, 1, 1), manager, now=T0)
        s.commit()
        data = SaleInput(
            ag_kind="partner",
            partner_id=partner.id,
            payment_method="credit",
            staff_sign="0004SV",
            tickets=(
                TicketInput(
                    doc_number="3940000001" if agency_id is None else "3940000002",
                    airline="199",
                    total_doc="21368",
                    pax_name="SYNTH01/PAX",
                    pax_type="ADT",
                    rloc="ABC123",
                    trnc="TKTT",
                ),
            ),
        )
        sales.create_sale(s, manager, data, day=OCT_3, now=T0)
        s.commit()
        return partner.id


def close_days(env: Env) -> None:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        for row in s.scalars(select(ReportDay).where(ReportDay.agency_id == env.seeded.agency_a)):
            row.status, row.closed_at, row.closed_by_user_id = "closed", T0, env.seeded.manager_a
        s.commit()


def pending_adjustment(env: Env, pid: int, by: int) -> int:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        written = partner_book.add_line(
            s, _user(s, by), pid, kind="AJUSTEMENT", amount="200", direction="plus",
            reason="frais oubliés le 3", request_key=partner_book.new_request_key(), now=T0,
            entry_date=OCT_3.isoformat(),
        )  # fmt: skip
        s.commit()
        entry_id: int = written.row.id
        return entry_id


def rows(env: Env, model: type) -> list:  # type: ignore[type-arg]
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        return list(s.scalars(select(model).where(model.agency_id == env.seeded.agency_a)))  # type: ignore[attr-defined]


def form_key(c: TestClient, path: str) -> tuple[str, str]:
    page = c.get(path)
    assert page.status_code == 200, page.text
    match = _KEY.search(page.text)
    assert match, "no request_key on the page"
    return csrf_for(c, path), match.group(1)


def post(c: TestClient, path: str, data: dict[str, str], via: str) -> httpx.Response:
    token, key = form_key(c, via)
    response: httpx.Response = c.post(
        path, data={"request_key": key, **data, "csrf_token": token}, follow_redirects=False
    )
    return response


LINE = {"kind": "ADM", "amount": "1 500", "reason": "pénalité de la compagnie"}
PAYMENT = {"amount": "10 000", "method": "cash", "paid_on": "2026-10-16"}

GETS = [
    "/partner-accounts",
    "/partner-accounts/{p}",
    "/partner-accounts/{p}/periods/2026-10-01",
    "/partner-accounts/{p}/lines/new",
    "/partner-accounts/{p}/payments/new",
    "/partner-accounts/{p}/periods/2026-10-01/close",
]
POSTS = [
    "/partner-accounts/{p}/lines",
    "/partner-accounts/{p}/payments",
    "/partner-accounts/{p}/lines/{e}/decision",
    "/partner-accounts/{p}/periods/2026-10-01/close",
]


# --- who may ----------------------------------------------------------------------------------


def test_every_route_needs_the_reader_or_the_writer_dependency() -> None:
    from fastapi.routing import APIRoute

    from app.web.routes import partner_accounts as routes

    found = [r for r in routes.router.routes if isinstance(r, APIRoute)]
    assert len(found) == len(GETS) + len(POSTS)
    writers = {"/{partner_id}/lines/new", "/{partner_id}/payments/new"}
    for route in found:
        calls = [d.call for d in route.dependant.dependencies]
        relative = route.path.removeprefix("/partner-accounts")
        if "POST" in (route.methods or set()) or relative in writers or relative.endswith("/close"):
            assert routes.require_account_writer in calls, route.path
        else:
            assert routes.require_account_reader in calls, route.path
    assert routes.require_account_reader.__name__ == "require_can_manage_partners"
    assert routes.require_account_writer.__name__ == "require_can_validate_money"


@pytest.mark.parametrize("who", ["manager", "accountant"])
@pytest.mark.parametrize("path", GETS)
def test_manager_and_accountant_open_every_page(env: Env, who: str, path: str) -> None:
    pid = setup_partner(env)
    add_accountant(env)
    c = env.manager_a() if who == "manager" else accountant(env)
    response = c.get(path.format(p=pid))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    assert '<html lang="fr"' in html
    assert not re.search(r"\sstyle\s*=", html, re.I) and "<style" not in html.lower()
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


@pytest.mark.parametrize("path", GETS)
def test_staff_get_403_and_an_audit_event(env: Env, path: str) -> None:
    pid = setup_partner(env)
    response = env.staff_a().get(path.format(p=pid))
    assert response.status_code == 403
    assert "Partenaire Test" not in response.text
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert event.actor_user_id == env.seeded.staff_a
    assert event.details["path"] == path.format(p=pid)


@pytest.mark.parametrize("path", POSTS)
def test_staff_posts_are_refused_and_nothing_is_written(env: Env, path: str) -> None:
    """Payments included (owner's answer 1: the accountant and the manager only)."""
    pid = setup_partner(env)
    add_accountant(env)
    entry = pending_adjustment(env, pid, env.seeded.manager_a)
    close_days(env)
    c = env.staff_a()
    response = c.post(
        path.format(p=pid, e=entry),
        data={
            **LINE,
            **PAYMENT,
            "received_by": str(env.seeded.staff_a),
            "decision": "approve",
            "request_key": partner_book.new_request_key(),
            "csrf_token": csrf_for(c),
        },
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert audit_events(env.db.owner_url, env.seeded.agency_a, "permission_denied")
    assert len(rows(env, PartnerEntry)) == 1  # the adjustment of the setup only
    assert rows(env, PartnerPayment) == [] and rows(env, PartnerPeriod) == []


@pytest.mark.parametrize("path", GETS[1:] + POSTS)
def test_another_agency_cannot_see_or_touch_a_partner_by_id(env: Env, path: str) -> None:
    pid = setup_partner(env)
    entry = pending_adjustment(env, pid, env.seeded.manager_a)
    close_days(env)
    b = env.manager_b()
    url = path.format(p=pid, e=entry)
    if path in POSTS:
        response = b.post(
            url,
            data={
                **LINE,
                **PAYMENT,
                "received_by": str(env.seeded.manager_b),
                "decision": "approve",
                "request_key": partner_book.new_request_key(),
                "csrf_token": csrf_for(b),
            },
            follow_redirects=False,
        )
    else:
        response = b.get(url)
    assert response.status_code == 404
    assert "Partenaire Test" not in response.text
    assert len(rows(env, PartnerEntry)) == 1
    assert rows(env, PartnerPayment) == [] and rows(env, PartnerPeriod) == []


def test_the_list_of_agency_b_shows_only_its_own_partners(env: Env) -> None:
    setup_partner(env)
    html = env.manager_b().get("/partner-accounts").text
    assert "Partenaire Test" not in html and "Aucun partenaire actif." in html


# --- writing through the pages ----------------------------------------------------------------


def test_a_line_is_added_once_even_when_the_form_is_posted_twice(env: Env) -> None:
    pid = setup_partner(env)
    c = env.manager_a()
    via = f"/partner-accounts/{pid}/lines/new"
    token, key = form_key(c, via)
    data = {**LINE, "entry_date": "2026-10-16", "request_key": key, "csrf_token": token}
    first = c.post(f"/partner-accounts/{pid}/lines", data=data, follow_redirects=False)
    second = c.post(f"/partner-accounts/{pid}/lines", data=data, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == f"/partner-accounts/{pid}"
    [line] = rows(env, PartnerEntry)
    assert (line.kind, line.amount) == ("ADM", Decimal("1500.00"))
    assert len(audit_events(env.db.owner_url, env.seeded.agency_a, "partner_entry_added")) == 1


def test_a_payment_is_recorded_by_the_accountant(env: Env) -> None:
    pid = setup_partner(env)
    acc_id = add_accountant(env)
    c = accountant(env)
    response = post(
        c,
        f"/partner-accounts/{pid}/payments",
        {**PAYMENT, "received_by": str(acc_id), "reference": "VIR 77"},
        via=f"/partner-accounts/{pid}/payments/new",
    )
    assert response.status_code == 303, response.text
    [paid] = rows(env, PartnerPayment)
    assert (paid.amount, paid.method, paid.reference) == (Decimal("10000.00"), "cash", "VIR 77")
    assert paid.recorded_by_user_id == acc_id
    page = c.get(f"/partner-accounts/{pid}").text
    assert f"10{NNBSP}000" in page and "Espèces" in page


def test_a_bad_amount_is_a_french_message_and_the_form_keeps_its_values(env: Env) -> None:
    pid = setup_partner(env)
    c = env.manager_a()
    response = post(
        c,
        f"/partner-accounts/{pid}/lines",
        {**LINE, "amount": "1,500", "reason": "<b>pénalité</b>"},
        via=f"/partner-accounts/{pid}/lines/new",
    )
    assert response.status_code == 400
    assert "Écrivez 1500 sans séparateur de milliers" in response.text
    assert "&lt;b&gt;pénalité" in response.text and "<b>pénalité" not in response.text
    assert rows(env, PartnerEntry) == []


def test_four_eyes_on_an_adjustment_through_the_pages(env: Env) -> None:
    pid = setup_partner(env)
    acc_id = add_accountant(env)
    entry = pending_adjustment(env, pid, acc_id)
    acc = accountant(env)
    page = acc.get(f"/partner-accounts/{pid}").text
    assert "à approuver" in page and "Approuver" not in page  # the accountant may not
    refused = acc.post(  # no button for the accountant: the post is forged
        f"/partner-accounts/{pid}/lines/{entry}/decision",
        data={
            "decision": "approve",
            "request_key": partner_book.new_request_key(),
            "csrf_token": csrf_for(acc),
        },
        follow_redirects=False,
    )
    assert refused.status_code == 403 and "Seul le gérant" in refused.text
    m = env.manager_a()
    assert "Approuver" in m.get(f"/partner-accounts/{pid}").text
    approved = post(
        m,
        f"/partner-accounts/{pid}/lines/{entry}/decision",
        {"decision": "approve"},
        via=f"/partner-accounts/{pid}",
    )
    assert approved.status_code == 303
    assert "validée" in m.get(f"/partner-accounts/{pid}").text


def test_closing_through_the_pages_freezes_the_statement(env: Env) -> None:
    pid = setup_partner(env)
    close_days(env)
    m = env.manager_a()
    confirm = m.get(f"/partner-accounts/{pid}/periods/2026-10-01/close").text
    assert "Clôturer la période du 1 au 15 octobre 2026" in confirm
    response = post(
        m, f"/partner-accounts/{pid}/periods/2026-10-01/close", {},
        via=f"/partner-accounts/{pid}/periods/2026-10-01/close",
    )  # fmt: skip
    assert response.status_code == 303
    assert response.headers["location"] == f"/partner-accounts/{pid}/periods/2026-10-01"
    [period] = rows(env, PartnerPeriod)
    assert (period.status, period.closing_balance) == ("closed", Decimal("21668.00"))
    html = m.get(f"/partner-accounts/{pid}/periods/2026-10-01").text
    assert "Clôturée" in html and f"21{NNBSP}668" in html and "SYNTH01/PAX" in html
    # A line dated in the closed period: refused with a French message, never a 500.
    late = post(
        m, f"/partner-accounts/{pid}/lines", {**LINE, "entry_date": "2026-10-03"},
        via=f"/partner-accounts/{pid}/lines/new",
    )  # fmt: skip
    assert late.status_code == 400
    assert "Cette date tombe dans une période déjà clôturée" in late.text
    assert rows(env, PartnerEntry) == []


def test_closing_while_a_day_is_open_is_refused_in_french(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    response = post(
        m, f"/partner-accounts/{pid}/periods/2026-10-01/close", {},
        via=f"/partner-accounts/{pid}/periods/2026-10-01/close",
    )  # fmt: skip
    assert response.status_code == 409
    assert "Des journées de la période ne sont pas clôturées" in response.text
    assert rows(env, PartnerPeriod) == []


def test_a_post_without_the_csrf_token_is_refused(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    for path, data in (
        (f"/partner-accounts/{pid}/lines", LINE),
        (f"/partner-accounts/{pid}/payments", {**PAYMENT, "received_by": "1"}),
        (f"/partner-accounts/{pid}/periods/2026-10-01/close", {}),
    ):
        response = m.post(
            path,
            data={**data, "request_key": partner_book.new_request_key()},
            follow_redirects=False,
        )
        assert response.status_code == 403, path
    assert rows(env, PartnerEntry) == [] and rows(env, PartnerPayment) == []
    assert rows(env, PartnerPeriod) == []


def test_a_post_without_a_request_key_is_refused(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    response = m.post(
        f"/partner-accounts/{pid}/lines",
        data={**LINE, "csrf_token": csrf_for(m)},
        follow_redirects=False,
    )
    assert response.status_code == 400 and "rechargez la page" in response.text
    assert rows(env, PartnerEntry) == []


def test_writes_are_rate_limited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ratelimit,
        "PARTNER_ACCOUNT_WRITE_USER",
        ratelimit.Limit("partner_account_write_user", 2, 3600),
    )
    pid = setup_partner(env)
    m = env.manager_a()
    via = f"/partner-accounts/{pid}/lines/new"
    codes = [
        post(
            m, f"/partner-accounts/{pid}/lines", {**LINE, "entry_date": "2026-10-16"}, via
        ).status_code
        for _ in range(3)
    ]
    assert codes == [303, 303, 429]
    assert len(rows(env, PartnerEntry)) == 2
    (event,) = audit_events(env.db.owner_url, env.seeded.agency_a, "rate_limited")
    assert event.details == {"scope": "partner_account_write_user"}


def test_closing_is_rate_limited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ratelimit,
        "PARTNER_PERIOD_CLOSE_USER",
        ratelimit.Limit("partner_period_close_user", 1, 3600),
    )
    pid = setup_partner(env)
    m = env.manager_a()
    via = f"/partner-accounts/{pid}/periods/2026-10-01/close"
    codes = [post(m, via, {}, via).status_code for _ in range(2)]
    assert codes == [409, 429]


# --- the pages --------------------------------------------------------------------------------


def test_the_sidebar_link_is_for_manager_and_accountant_only(env: Env) -> None:
    add_accountant(env)
    for client in (env.manager_a(), accountant(env)):
        html = client.get("/").text
        assert 'href="/partner-accounts"' in html and "Comptes partenaires" in html
    staff = env.staff_a().get("/").text
    assert 'href="/partner-accounts"' not in staff and "Comptes partenaires" not in staff


def test_the_account_page_shows_the_balance_and_the_buttons(env: Env) -> None:
    pid = setup_partner(env)
    html = env.manager_a().get(f"/partner-accounts/{pid}").text
    for label in (
        "Compte de Partenaire Test",
        "Ajouter une ligne",
        "Enregistrer un paiement",
        "Clôturer la période",
        "Période ouverte du 1 au 15 octobre 2026",
        "Périodes",
    ):
        assert label in html, label
    assert f"21{NNBSP}668" in html
    listing = env.manager_a().get("/partner-accounts").text
    assert "Partenaire Test" in listing and f"21{NNBSP}668" in listing


def test_every_post_form_carries_a_csrf_field_and_a_request_key(env: Env) -> None:
    pid = setup_partner(env)
    m = env.manager_a()
    pending_adjustment(env, pid, add_accountant(env))
    for path in (
        f"/partner-accounts/{pid}",
        f"/partner-accounts/{pid}/lines/new",
        f"/partner-accounts/{pid}/payments/new",
        f"/partner-accounts/{pid}/periods/2026-10-01/close",
    ):
        html = m.get(path).text
        forms = re.findall(r'<form[^>]*action="/partner-accounts[^>]*>.*?</form>', html, re.S)
        assert forms, path
        for form in forms:
            assert 'method="post"' in form
            assert 'name="csrf_token"' in form and 'name="request_key"' in form
