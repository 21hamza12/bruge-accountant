"""The sale screens (B5): entry form, read-back, confirm, list, detail with history, edit, void.

The read-back never writes; its values travel in hidden fields and are never trusted: the confirm
step validates and prices everything again on the server. Synthetic data only.
"""

from __future__ import annotations

import re
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.auth import ratelimit
from app.models import (
    ROLE_ACCOUNTANT,
    AuditEvent,
    ReportDay,
    Sale,
    SaleTicket,
    SaleVersion,
)
from app.tenancy import rebind_tenant
from tests.page_support import Env, fr
from tests.web_support import (
    PASSWORD_S,
    T0,
    WebDb,
    add_user,
    csrf_for,
    owner_session,
)

ACCOUNTANT_PHONE = "+22212345691"
STAFF2_PHONE = "+22212345692"
SIGN = "0004SV"
SIGN2 = "0006AC"
SIGN3 = "0007AA"
D1, D2, D3, D4 = "3940000001", "3940000002", "3940000003", "3940000004"
TODAY = "2026-10-18"  # the FakeClock's day
YESTERDAY = "2026-10-17"
TICKET_FIELDS = ("doc_number", "airline", "total_doc", "pax_name", "pax_type", "rloc", "trnc")


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    return e


# --- helpers ------------------------------------------------------------------------------------


def tk(doc: str = D1, cost: str = "21368", **kw: str) -> dict[str, str]:
    row = {
        "doc_number": doc,
        "airline": "199",
        "total_doc": cost,
        "pax_name": "SYNTH01/PAX",
        "pax_type": "ADT",
        "rloc": "ABC123",
        "trnc": "TKTT",
    }
    row.update(kw)
    return row


def form(*tickets: dict[str, str], **fields: Any) -> dict[str, Any]:
    """The posted fields of one sale: a list per ticket field (one entry per row)."""
    rows = tickets or (tk(),)
    data: dict[str, Any] = {name: [row[name] for row in rows] for name in TICKET_FIELDS}
    data.update({"ag": "direct", "pb": "24000", "clt": "Client Synth", "payment_method": "cash"})
    data.update(fields)
    return data


def post(c: TestClient, path: str, data: dict[str, Any], *, via: str = "/sales/new") -> Any:
    payload = {**data, "csrf_token": csrf_for(c, via)}
    return c.post(path, data=payload, follow_redirects=False)


class Hidden(HTMLParser):
    """The hidden inputs of the form that posts to ``action``."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.inside = False
        self.fields: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.inside = a.get("action") == self.action
        elif tag == "input" and self.inside and a.get("type") == "hidden":
            self.fields.append((a["name"], a.get("value", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.inside = False


class Dom(HTMLParser):
    """What the form tests need to know about a page: the ticket rows, the error paragraphs (with
    the row they sit in), every control with its id, label and description."""

    def __init__(self) -> None:
        super().__init__()
        self.rows = 0
        self.row: int | None = None
        self.ids: set[str] = set()
        self.label_for: set[str] = set()
        self.controls: list[tuple[str, dict[str, str], int | None]] = []
        self.errors: list[tuple[int | None, str, str]] = []  # (row, id, text)
        self.invalid: list[tuple[int | None, str]] = []  # (row, control id)
        self.tags: list[str] = []
        self._error: list[str] | None = None
        self._error_id = ""
        self._error_row: int | None = None

    @classmethod
    def of(cls, html: str) -> Dom:
        dom = cls()
        dom.feed(html)
        return dom

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        self.tags.append(tag)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "fieldset" and "ticket-row" in a.get("class", "").split():
            self.row = self.rows
            self.rows += 1
        elif tag == "label" and "for" in a:
            self.label_for.add(a["for"])
        elif tag in ("input", "select") and a.get("type") not in ("hidden", "submit"):
            self.controls.append((tag, a, self.row))
            if a.get("aria-invalid") == "true":
                self.invalid.append((self.row, a.get("id", "")))
        elif tag == "p" and a.get("class") == "error":
            self._error, self._error_id, self._error_row = [], a.get("id", ""), self.row

    def handle_data(self, data: str) -> None:
        if self._error is not None:
            self._error.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "fieldset":
            self.row = None
        elif tag == "p" and self._error is not None:
            self.errors.append((self._error_row, self._error_id, "".join(self._error).strip()))
            self._error = None


def hidden(html: str, action: str) -> list[tuple[str, str]]:
    parser = Hidden(action)
    parser.feed(html)
    assert parser.fields, f"no hidden fields in a form posting to {action}"
    return parser.fields


def confirm_with(c: TestClient, html: str, action: str, **override: str) -> httpx.Response:
    """Post the read-back's own hidden fields (as a browser would), optionally tampered."""
    data: dict[str, list[str]] = {}
    for key, value in hidden(html, action):
        if key not in override:
            data.setdefault(key, []).append(value)
    data.update({key: [value] for key, value in override.items()})
    response: httpx.Response = c.post(action, data=data, follow_redirects=False)
    return response


def staff_a_client(env: Env) -> TestClient:
    return env.staff_a()


def staff2(env: Env) -> TestClient:
    uid = add_user(env.db.owner_url, env.seeded.agency_a, STAFF2_PHONE, display_name="Staff 2")
    env.link(uid, SIGN2)
    return env.as_user(STAFF2_PHONE, PASSWORD_S)


def accountant(env: Env) -> TestClient:
    add_user(
        env.db.owner_url,
        env.seeded.agency_a,
        ACCOUNTANT_PHONE,
        role=ROLE_ACCOUNTANT,
        display_name="Comptable A",
    )
    return env.as_user(ACCOUNTANT_PHONE, PASSWORD_S)


def make_partner(env: Env, name: str = "BADI", fee: str | None = "500") -> int:
    c = env.manager_a()
    r = post(c, "/partners", {"name": name}, via="/partners")
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    if fee is not None:
        post(
            c,
            f"/partners/{pid}/fees",
            {"fee": fee, "valid_from": "2026-01-01"},
            via="/partners",
        )
    return pid


def table(env: Env, model: Any, agency_id: int | None = None) -> list[Any]:
    agency = agency_id or env.seeded.agency_a
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, agency)
        return list(s.scalars(select(model).where(model.agency_id == agency)))


def counts(env: Env) -> tuple[int, ...]:
    """Rows of the sale tables and of the audit log, not counting the logins of the test itself."""
    quiet = ("login", "agency_", "user_", "csrf_")
    audit_rows = [e for e in table(env, AuditEvent) if not e.event_type.startswith(quiet)]
    return (
        *(len(table(env, m)) for m in (Sale, SaleVersion, SaleTicket, ReportDay)),
        len(audit_rows),
    )


def sale_id_of(response: httpx.Response) -> int:
    assert response.status_code == 303, response.text[:2000]
    match = re.fullmatch(r"/sales/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def enter(c: TestClient, *tickets: dict[str, str], **fields: Any) -> int:
    """Create a sale through the confirm route and return its id."""
    return sale_id_of(post(c, "/sales", form(*tickets, **fields)))


def latest(env: Env, sale_id: int) -> SaleVersion:
    versions = [v for v in table(env, SaleVersion) if v.sale_id == sale_id]
    newest: SaleVersion = max(versions, key=lambda v: v.version_no)
    return newest


def text_of(html: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def set_day_status(env: Env, status: str) -> None:
    with owner_session(env.db.owner_url) as s:
        rebind_tenant(s, env.seeded.agency_a)
        s.execute(
            update(ReportDay).values(
                status=status, closed_at=T0, closed_by_user_id=env.seeded.manager_a
            )
        )
        s.commit()


# --- the entry form -----------------------------------------------------------------------------


def test_the_new_sale_form_renders_for_staff_with_their_own_sign_only(env: Env) -> None:
    env.link(env.seeded.staff_a, SIGN3)
    uid = add_user(env.db.owner_url, env.seeded.agency_a, STAFF2_PHONE)
    env.link(uid, SIGN2)
    c = env.staff_a()
    r = c.get("/sales/new")
    assert r.status_code == 200
    html = r.text
    assert "Saisir une vente" in html
    for label in ("DOC NUMBER", "A/L", "TOTAL DOC", "PAX NAME", "RLOC", "TRNC", "P.B", "CLT", "AP"):
        assert label in html, label
    select_sign = re.search(r'<select[^>]*name="staff_sign".*?</select>', html, re.S)
    assert select_sign
    assert SIGN in select_sign.group(0) and SIGN3 in select_sign.group(0)
    assert SIGN2 not in html
    assert 'name="day"' not in html  # staff always enter today
    assert 'action="/sales/preview"' in html
    for method in ("Espèces", "Bankily", "Masrvi", "Sedad", "Virement", "Chèque", "Crédit"):
        assert method in html, method
    for kind in ("ADT", "CHD", "INF", "TKTT", "EMDA", "EMDS", "RFND", "CANX"):
        assert kind in html, kind
    assert "BRUGE (vente directe)" in html
    assert "le client a refusé de donner son numéro" in html.lower()


def test_a_staff_member_with_one_sign_does_not_get_a_select(env: Env) -> None:
    html = env.staff_a().get("/sales/new").text
    assert '<select id="staff_sign"' not in html and 'name="staff_sign"' in html
    assert SIGN in html


def test_manager_and_accountant_choose_the_sign_and_the_day(env: Env) -> None:
    for c in (env.manager_a(), accountant(env)):
        html = c.get("/sales/new").text
        assert 'name="staff_sign"' in html and 'name="day"' in html
        assert 'max="2026-10-18"' in html


def test_a_staff_member_without_a_sign_is_told_to_ask_the_manager(env: Env) -> None:
    add_user(env.db.owner_url, env.seeded.agency_a, STAFF2_PHONE)
    c = env.as_user(STAFF2_PHONE, PASSWORD_S)
    r = c.get("/sales/new")
    assert r.status_code == 200
    assert "Demandez au gérant de lier votre signe" in r.text
    assert 'action="/sales/preview"' not in r.text


def test_the_partner_list_has_only_the_active_partners_of_the_agency(env: Env) -> None:
    make_partner(env, "BADI")
    inactive = make_partner(env, "ANCIEN")
    m = env.manager_a()
    post(m, f"/partners/{inactive}/deactivate", {}, via="/partners")
    other = env.manager_b()
    post(other, "/partners", {"name": "AUTRE-AGENCE"}, via="/partners")
    html = env.staff_a().get("/sales/new").text
    assert "BADI" in html and "ANCIEN" not in html and "AUTRE-AGENCE" not in html


def test_pb_and_the_partner_note_are_both_in_the_page_for_the_two_kinds_of_sale(env: Env) -> None:
    html = env.staff_a().get("/sales/new").text
    assert 'name="pb"' in html
    assert "calculé : coût + frais du partenaire" in html
    assert 'name="fee_override"' in html and 'name="fee_override_reason"' in html


def test_the_pages_need_a_login(env: Env) -> None:
    anonymous = env.factory()
    for path in ("/sales", "/sales/new", "/sales/1", "/sales/1/edit", "/sales/1/void"):
        r = anonymous.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


# --- add a row (no JavaScript) ------------------------------------------------------------------


def test_add_a_ticket_row_rerenders_the_form_with_one_more_empty_row(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1), clt="Keep Me", add_row="1"))
    assert r.status_code == 200
    assert r.text.count('name="doc_number"') == 2
    assert D1 in r.text and "Keep Me" in r.text
    assert counts(env) == (0, 0, 0, 0, 0)


def test_the_form_has_at_most_twenty_rows(env: Env) -> None:
    c = env.staff_a()
    rows = [tk(f"39400000{n:02d}") for n in range(20)]
    r = post(c, "/sales/preview", form(*rows, add_row="1"))
    assert r.text.count('name="doc_number"') == 20
    assert "20 billets" in r.text


def test_empty_rows_are_ignored_and_errors_point_at_the_row_typed(env: Env) -> None:
    c = env.staff_a()
    empty = tk("", "", pax_name="", rloc="", airline="")
    bad = tk(D2, "abc")
    r = post(c, "/sales/preview", form(tk(D1), empty, bad))
    assert r.status_code == 400
    dom = Dom.of(r.text)
    assert dom.rows == 3
    # the error sits in row 3 (index 2), where it was typed, not in row 2
    assert [row for row, _, _ in dom.errors if row is not None] == [2]
    assert "montant" in dom.errors[0][2].lower() or "Montant" in dom.errors[0][2]


def test_a_form_without_any_ticket_asks_for_one(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk("", "", pax_name="", rloc="", airline="")))
    assert r.status_code == 400 and "Ajoutez au moins un billet" in r.text


# --- the read-back ------------------------------------------------------------------------------


def test_the_read_back_writes_nothing_at_all(env: Env) -> None:
    c = env.staff_a()
    before = counts(env)
    r = post(c, "/sales/preview", form(tk(D1, "21368"), pb="24000"))
    assert r.status_code == 200
    assert counts(env) == before == (0, 0, 0, 0, 0)
    text = text_of(r.text)
    assert "Confirmer" in text and D1 in text
    assert f"{fr(21368)}" in text and f"{fr(24000)}" in text
    assert fr(2632) not in text  # the profit is the manager's alone (doc 02, decision 24)
    assert "Client Synth" in text and "Espèces" in text and SIGN in text
    assert 'action="/sales"' in r.text


def test_a_failed_read_back_writes_nothing_either(env: Env) -> None:
    c = env.staff_a()
    post(c, "/sales/preview", form(tk(D1, "abc")))
    assert counts(env) == (0, 0, 0, 0, 0)


def test_a_two_ticket_layover_sale_shows_both_tickets_and_one_price(env: Env) -> None:
    c = env.staff_a()
    layover = form(
        tk(D1, "21368", airline="TU", pax_name="SYNTH01/PAX"),
        tk(D2, "10000", airline="SV", pax_name="SYNTH01/PAX"),
        pb="35000",
    )
    r = post(c, "/sales/preview", layover)
    assert r.status_code == 200
    text = text_of(r.text)
    assert D1 in text and D2 in text and "TU" in text and "SV" in text
    assert text.count(fr(35000)) == 1  # one P.B, once
    assert fr(31368) in text  # sigma cost
    assert fr(3632) not in text  # the profit (P.B - sigma cost) is the manager's alone
    sale_id = sale_id_of(confirm_with(c, r.text, "/sales"))
    assert len([t for t in table(env, SaleTicket) if t.sale_id == sale_id]) == 2
    stored = latest(env, sale_id)
    assert (stored.pb, stored.cost_total) == (Decimal("35000"), Decimal("31368"))


def test_a_partner_sale_shows_the_computed_price_cost_plus_fee_per_ticket(env: Env) -> None:
    pid = make_partner(env, "BADI", "500")
    c = env.staff_a()
    data = form(tk(D1, "1000"), tk(D2, "2000"), ag=str(pid), payment_method="credit", pb="1")
    data.pop("clt")
    r = post(c, "/sales/preview", data)
    assert r.status_code == 200, r.text[:3000]
    text = text_of(r.text)
    assert fr(4000) in text  # 3000 + 2 x 500
    assert "BADI" in text and fr(500) in text  # the fee per ticket (the profit is the manager's)


def test_a_price_below_the_cost_shows_the_warning_and_can_still_be_confirmed(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(tk(D1, "21368"), pb="20000"))
    assert r.status_code == 200
    assert "inférieur au coût" in text_of(r.text)
    assert sale_id_of(confirm_with(c, r.text, "/sales")) > 0


def test_a_manager_is_warned_about_a_sign_nobody_has(env: Env) -> None:
    r = post(env.manager_a(), "/sales/preview", form(staff_sign="0099ZZ", day=TODAY))
    assert r.status_code == 200 and "0099ZZ" in text_of(r.text) and "aucun compte" in r.text


def test_field_errors_are_in_french_next_to_the_fields(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(clt="", pb="", payment_method="", ap="12"))
    assert r.status_code == 400
    for message in (
        "Le nom du client est obligatoire",
        "Saisissez le prix P.B",
        "Choisissez un mode de paiement",
    ):
        assert message in r.text, message
    assert 'aria-describedby="clt-error"' in r.text and 'id="clt-error"' in r.text
    assert "Traceback" not in r.text


def test_a_phone_and_the_refusal_checkbox_together_are_an_error(env: Env) -> None:
    r = post(
        env.staff_a(),
        "/sales/preview",
        form(ap="22 33 44 55", client_refused_phone="1"),
    )
    assert r.status_code == 400 and "refusé" in r.text


def test_a_refused_phone_is_read_back_as_such(env: Env) -> None:
    c = env.staff_a()
    r = post(c, "/sales/preview", form(client_refused_phone="1"))
    assert r.status_code == 200 and "a refusé" in text_of(r.text)
    sale_id = sale_id_of(confirm_with(c, r.text, "/sales"))
    stored = latest(env, sale_id)
    assert stored.client_refused_phone is True and stored.ap is None


def test_the_phone_is_optional_for_a_direct_sale(env: Env) -> None:
    r = post(env.staff_a(), "/sales/preview", form())
    assert r.status_code == 200


def test_oversized_and_odd_values_are_french_errors_not_422(env: Env) -> None:
    c = env.staff_a()
    for data in (
        form(clt="x" * 5000),
        form(tk(D1, "9" * 40)),
        form(pb="1e999"),
        form(ag="not-a-number"),
        form(ag="9" * 40),
        form(tk(pax_type="XYZ", trnc="NOPE", airline="!!")),
        form(payment_method="bitcoin"),
        form(staff_sign="x" * 500),
    ):
        r = post(c, "/sales/preview", data)
        assert r.status_code in (400, 403), (r.status_code, data)
        assert r.status_code != 422 and "Traceback" not in r.text


def test_missing_fields_are_french_errors_not_a_raw_validation_page(env: Env) -> None:
    c = env.staff_a()
    r = c.post("/sales/preview", data={"csrf_token": csrf_for(c, "/sales/new")})
    assert r.status_code == 400 and "Ajoutez au moins un billet" in r.text


def test_too_many_rows_posted_directly_are_refused(env: Env) -> None:
    rows = [tk(f"39400000{n:02d}") for n in range(21)]
    r = post(env.staff_a(), "/sales/preview", form(*rows))
    assert r.status_code == 400 and "20 billets" in r.text


def test_misaligned_ticket_columns_are_refused(env: Env) -> None:
    data = form(tk(D1), tk(D2))
    data["rloc"] = ["ABC123"]  # one short
    r = post(env.staff_a(), "/sales/preview", data)
    assert r.status_code == 400 and "Traceback" not in r.text


# --- duplicates ---------------------------------------------------------------------------------


def test_a_duplicate_of_my_own_sale_offers_to_edit_the_existing_sale(env: Env) -> None:
    c = env.staff_a()
    first = enter(c, tk(D1))
    r = post(c, "/sales/preview", form(tk(D1)))
    assert r.status_code == 409
    assert "déjà enregistré" in r.text and D1 in r.text
    assert f'href="/sales/{first}/edit"' in r.text and "Modifier la vente existante" in r.text
    assert len(table(env, Sale)) == 1


def test_a_duplicate_of_another_agents_sale_shows_no_link_and_no_details(env: Env) -> None:
    other = staff2(env)
    theirs = enter(other, tk(D1), clt="Secret Client Name")
    c = env.staff_a()
    for path in ("/sales/preview", "/sales"):
        r = post(c, path, form(tk(D1)))
        assert r.status_code == 409, path
        assert "Ce billet est déjà enregistré par un autre agent" in r.text
        assert f"/sales/{theirs}" not in r.text
        assert "Modifier la vente existante" not in r.text
        assert "Secret Client Name" not in r.text and SIGN2 not in r.text
    assert len(table(env, Sale)) == 1


def test_a_manager_sees_the_link_for_any_duplicate(env: Env) -> None:
    first = enter(env.staff_a(), tk(D1))
    r = post(env.manager_a(), "/sales/preview", form(tk(D1), staff_sign=SIGN))
    assert r.status_code == 409 and f'href="/sales/{first}/edit"' in r.text


# --- confirm ------------------------------------------------------------------------------------


def test_confirm_creates_the_sale_and_redirects_to_it(env: Env) -> None:
    c = env.staff_a()
    preview = post(c, "/sales/preview", form(tk(D1, "21368"), pb="24000", ap="22 33 44 55"))
    r = confirm_with(c, preview.text, "/sales")
    sale_id = sale_id_of(r)
    (sale,) = table(env, Sale)
    assert (sale.id, sale.agency_id, sale.staff_sign, sale.status) == (
        sale_id,
        env.seeded.agency_a,
        SIGN,
        "active",
    )
    version = latest(env, sale_id)
    assert version.pb == Decimal("24000") and version.ap == "+22222334455"
    assert version.actor_user_id == env.seeded.staff_a and version.source == "form"
    detail = c.get(r.headers["location"])
    assert detail.status_code == 200 and D1 in detail.text


def test_confirm_revalidates_and_recomputes_a_tampered_partner_price(env: Env) -> None:
    pid = make_partner(env, "BADI", "500")
    c = env.staff_a()
    data = form(tk(D1, "1000"), ag=str(pid), payment_method="credit", pb="9999")
    data.pop("clt")
    preview = post(c, "/sales/preview", data)
    assert preview.status_code == 200
    # the read-back's own hidden fields, with P.B and the cost tampered with
    r = confirm_with(c, preview.text, "/sales", pb="1", total_doc="1")
    sale_id = sale_id_of(r)
    stored = latest(env, sale_id)
    assert stored.pb == Decimal("501.00")  # 1 + 1 x 500: the server priced the posted cost
    r2 = post(c, "/sales", {**data, "doc_number": [D2], "pb": "5"})
    stored2 = latest(env, sale_id_of(r2))
    assert stored2.pb == Decimal("1500.00") and stored2.fee_applied == Decimal("500")


def test_a_tampered_hidden_sign_that_is_not_mine_is_refused_and_audited(env: Env) -> None:
    other = staff2(env)
    del other
    c = env.staff_a()
    preview = post(c, "/sales/preview", form())
    r = confirm_with(c, preview.text, "/sales", staff_sign=SIGN2)
    assert r.status_code == 403
    assert "pas lié à votre compte" in r.text
    assert table(env, Sale) == []
    denied = [e for e in table(env, AuditEvent) if e.event_type == "permission_denied"]
    assert any(e.details.get("action") == "create_sale" for e in denied)


def test_a_tampered_sign_at_preview_is_refused_too(env: Env) -> None:
    staff2(env)
    r = post(env.staff_a(), "/sales/preview", form(staff_sign=SIGN2))
    assert r.status_code == 403 and "pas lié à votre compte" in r.text


def test_agency_id_and_sale_id_fields_are_ignored(env: Env) -> None:
    c = env.staff_a()
    r = post(
        c,
        "/sales",
        form(agency_id=str(env.seeded.agency_b), sale_id="1", id="1", report_day_id="1"),
    )
    sale_id = sale_id_of(r)
    (sale,) = table(env, Sale)
    assert sale.id == sale_id and sale.agency_id == env.seeded.agency_a
    assert table(env, Sale, env.seeded.agency_b) == []
    # and when editing, a sale_id in the body does not redirect the edit to another sale
    other = enter(c, tk(D2))
    r = post(
        c,
        f"/sales/{sale_id}",
        form(pb="25000", sale_id=str(other), version_no="1"),
        via=f"/sales/{sale_id}/edit",
    )
    assert r.status_code == 303 and r.headers["location"] == f"/sales/{sale_id}"
    assert latest(env, sale_id).version_no == 2 and latest(env, other).version_no == 1


def test_staff_cannot_choose_the_day_they_post(env: Env) -> None:
    # the day follows the tickets' issue date (tests/test_sale_day_pages.py); a posted day with
    # no ticket date behind it is refused, and the typed sale stays on today
    c = env.staff_a()
    refused = post(c, "/sales", form(day=YESTERDAY))
    assert refused.status_code == 403 and "émission" in text_of(refused.text)
    sale_id = sale_id_of(post(c, "/sales", form()))
    assert "18 octobre 2026" in c.get(f"/sales/{sale_id}").text


def test_a_manager_enters_a_sale_for_a_past_day_and_a_chosen_sign(env: Env) -> None:
    m = env.manager_a()
    sale_id = sale_id_of(post(m, "/sales", form(staff_sign=SIGN, day=YESTERDAY)))
    (sale,) = table(env, Sale)
    assert sale.staff_sign == SIGN
    days = {d.id: d.day.isoformat() for d in table(env, ReportDay)}
    assert days[sale.report_day_id] == YESTERDAY
    assert latest(env, sale_id).actor_user_id == env.seeded.manager_a


def test_a_manager_must_choose_a_sign_and_cannot_choose_a_future_day(env: Env) -> None:
    m = env.manager_a()
    r = post(m, "/sales/preview", form(staff_sign="", day=TODAY))
    assert r.status_code == 400 and "Choisissez le signe" in r.text
    r = post(m, "/sales/preview", form(staff_sign=SIGN, day="2026-10-19"))
    assert r.status_code == 400 and "futur" in r.text
    r = post(m, "/sales/preview", form(staff_sign=SIGN, day="31/12/2026"))
    assert r.status_code == 400 and "date" in r.text.lower()
    assert counts(env) == (0, 0, 0, 0, 0)


def test_the_accountant_can_enter_a_sale_too(env: Env) -> None:
    a = accountant(env)
    assert sale_id_of(post(a, "/sales", form(staff_sign=SIGN, day=TODAY))) > 0


def test_a_closed_day_is_a_french_message_not_a_500(env: Env) -> None:
    c = env.staff_a()
    first = enter(c, tk(D1))
    set_day_status(env, "closed")
    for path in ("/sales/preview", "/sales"):
        r = post(c, path, form(tk(D2)))
        assert r.status_code == 409 and "clôturée" in r.text, path
    r = post(c, f"/sales/{first}/preview-edit", form(pb="30000", version_no="1"))
    assert r.status_code == 409 and "clôturée" in r.text
    r = post(c, f"/sales/{first}", form(pb="30000", version_no="1"))
    assert r.status_code == 409 and "clôturée" in r.text
    r = post(c, f"/sales/{first}/void", {"reason": "saisie en double", "version_no": "1"})
    assert r.status_code == 409 and "clôturée" in r.text
    assert len(table(env, Sale)) == 1 and latest(env, first).version_no == 1


def test_a_tampered_hidden_agency_ag_value_of_another_agency_is_refused(env: Env) -> None:
    foreign = make_partner_in_b(env)
    r = post(env.staff_a(), "/sales", form(ag=str(foreign), payment_method="credit"))
    assert r.status_code == 400 and "partenaire" in r.text.lower()
    assert table(env, Sale) == []


def make_partner_in_b(env: Env) -> int:
    c = env.manager_b()
    r = post(c, "/partners", {"name": "ETRANGER"}, via="/partners")
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    post(
        c,
        f"/partners/{pid}/fees",
        {"fee": "100", "valid_from": "2026-01-01"},
        via="/partners",
    )
    return pid


def test_a_partner_without_a_fee_is_refused_and_named(env: Env) -> None:
    pid = make_partner(env, "SANSFRAIS", None)
    r = post(env.staff_a(), "/sales/preview", form(ag=str(pid), payment_method="credit"))
    assert r.status_code == 400 and "SANSFRAIS" in r.text


def test_a_fee_override_needs_a_reason_and_is_priced_per_ticket(env: Env) -> None:
    pid = make_partner(env, "BADI", "500")
    c = env.staff_a()
    base = form(tk(D1, "1000"), tk(D2, "2000"), ag=str(pid), payment_method="credit")
    r = post(c, "/sales/preview", {**base, "fee_override": "100"})
    assert r.status_code == 400 and "motif" in r.text
    sale_id = sale_id_of(
        post(c, "/sales", {**base, "fee_override": "100", "fee_override_reason": "accord oral"})
    )
    stored = latest(env, sale_id)
    assert stored.pb == Decimal("3200") and stored.fee_override_reason == "accord oral"


# --- my sales, the day's sales ------------------------------------------------------------------


def test_my_sales_lists_the_day_with_exact_totals(env: Env) -> None:
    c = env.staff_a()
    enter(c, tk(D1, "100.10"), pb="150.25", clt="Alpha Client")
    enter(
        c, tk(D2, "200.20"), tk(D3, "50.05"), pb="400", clt="Beta Client", payment_method="bankily"
    )
    r = c.get("/sales")
    assert r.status_code == 200
    text = text_of(r.text)
    assert "Mes ventes" in text and "Alpha Client" in text and "Beta Client" in text
    assert "Bankily" in text and "Espèces" in text
    # totals: 2 sales, 3 tickets, cost 350.35, P.B 550.25 (the profit, 199.90, is the manager's)
    assert "350,35" in text and "550,25" in text and "199,90" not in text
    assert "3 billets" in text
    assert re.search(r'href="/sales/\d+"', r.text)


def test_the_list_defaults_to_today_and_takes_a_date(env: Env) -> None:
    m = env.manager_a()
    enter(m, tk(D1), staff_sign=SIGN, day=YESTERDAY, clt="Hier Client")
    enter(m, tk(D2), staff_sign=SIGN, day=TODAY, clt="Aujourdhui Client")
    today = m.get("/sales").text
    assert "Aujourdhui Client" in today and "Hier Client" not in today
    yesterday = m.get(f"/sales?date={YESTERDAY}").text
    assert "Hier Client" in yesterday and "Aujourdhui Client" not in yesterday
    assert m.get("/sales?date=20261018").status_code == 400
    assert m.get("/sales?date=2026-13-45").status_code == 400
    assert m.get("/sales?date=2030-01-01").status_code == 200  # nothing there, not an error


def test_staff_see_only_their_own_signs_and_the_sign_filter_cannot_widen_it(env: Env) -> None:
    other = staff2(env)
    enter(other, tk(D1), clt="Autre Agent Client")
    enter(env.staff_a(), tk(D2), clt="Mon Client")
    mine = env.staff_a().get("/sales").text
    assert "Mon Client" in mine and "Autre Agent Client" not in mine
    widened = env.staff_a().get(f"/sales?sign={SIGN2}").text
    assert "Autre Agent Client" not in widened


def test_manager_and_accountant_see_every_sign_and_can_filter_by_sign(env: Env) -> None:
    enter(staff2(env), tk(D1), clt="Autre Agent Client")
    enter(env.staff_a(), tk(D2), clt="Mon Client")
    for c in (env.manager_a(), accountant(env)):
        both = c.get("/sales").text
        assert "Autre Agent Client" in both and "Mon Client" in both
        assert "Ventes du jour" in both
        only = c.get(f"/sales?sign={SIGN2}").text
        assert "Autre Agent Client" in only and "Mon Client" not in only


def test_voided_sales_leave_the_list_and_the_totals(env: Env) -> None:
    c = env.staff_a()
    keep = enter(c, tk(D1, "1000"), pb="1500", clt="Gardée")
    gone = enter(c, tk(D2, "5000"), pb="6000", clt="Annulée")
    post(
        c,
        f"/sales/{gone}/void",
        {"reason": "saisie en double", "version_no": "1"},
        via=f"/sales/{gone}/void",
    )
    html = c.get("/sales").text
    assert "Gardée" in html and "Annulée" not in html
    assert fr(6000) not in text_of(html) and f"/sales/{keep}" in html


def test_the_list_never_shows_another_agencys_sales(env: Env) -> None:
    enter(env.staff_a(), tk(D1), clt="Client Agence A")
    assert "Client Agence A" not in env.manager_b().get("/sales").text


def test_an_empty_day_says_so(env: Env) -> None:
    assert "Aucune vente" in env.staff_a().get("/sales").text


# --- the sale page and its history --------------------------------------------------------------


def test_the_sale_page_shows_the_current_values(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, "21368", pax_name="SYNTH01/PAX"), pb="24000", ap="22 33 44 55")
    r = c.get(f"/sales/{sale_id}")
    assert r.status_code == 200
    text = text_of(r.text)
    for expected in (D1, "SYNTH01/PAX", "Client Synth", "+22222334455", "Espèces", SIGN, fr(24000)):
        assert expected in text, expected
    assert f'href="/sales/{sale_id}/edit"' in r.text and f'href="/sales/{sale_id}/void"' in r.text


def test_history_lists_versions_newest_first_with_who_when_and_why(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, "21368"), pb="24000")
    post(
        c,
        f"/sales/{sale_id}",
        form(pb="25000", version_no="1", reason="le client a négocié"),
        via=f"/sales/{sale_id}/edit",
    )
    m = env.manager_a()
    post(
        m,
        f"/sales/{sale_id}",
        form(pb="26000", version_no="2", reason="correction du gérant", staff_sign=SIGN),
        via=f"/sales/{sale_id}/edit",
    )
    html = m.get(f"/sales/{sale_id}").text
    text = text_of(html)
    first, second, third = (text.find(f"Version {n}") for n in (1, 2, 3))
    assert 0 < third < second < first
    assert "Manager A" in text and "Staff A" in text
    assert "correction du gérant" in text and "le client a négocié" in text
    assert "18 octobre 2026" in text and "12:00" in text
    assert fr(26000) in text and fr(25000) in text and fr(24000) in text
    assert "P.B" in text  # what changed names the field


def test_a_void_appears_in_the_history(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c)
    post(
        c,
        f"/sales/{sale_id}/void",
        {"reason": "erreur de saisie", "version_no": "1"},
        via=f"/sales/{sale_id}/void",
    )
    text = text_of(c.get(f"/sales/{sale_id}").text)
    assert "Annulée" in text and "erreur de saisie" in text
    assert "/edit" not in c.get(f"/sales/{sale_id}").text


def test_the_sale_page_warns_again_about_a_low_price(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, "21368"), pb="20000")
    assert "inférieur au coût" in text_of(c.get(f"/sales/{sale_id}").text)


# --- access to somebody else's sale -------------------------------------------------------------


GETS = ["/sales/{id}", "/sales/{id}/edit", "/sales/{id}/void"]
POSTS = ["/sales/{id}/edit", "/sales/{id}/preview-edit", "/sales/{id}", "/sales/{id}/void"]


@pytest.mark.parametrize("path", GETS)
def test_staff_cannot_open_another_signs_sale_pages(env: Env, path: str) -> None:
    sale_id = enter(staff2(env), tk(D1))
    assert env.staff_a().get(path.format(id=sale_id)).status_code == 404


@pytest.mark.parametrize("path", POSTS)
def test_staff_cannot_post_to_another_signs_sale(env: Env, path: str) -> None:
    sale_id = enter(staff2(env), tk(D1))
    c = env.staff_a()
    r = post(c, path.format(id=sale_id), form(version_no="1", reason="essai non autorisé"))
    assert r.status_code == 404
    assert latest(env, sale_id).version_no == 1


@pytest.mark.parametrize("path", GETS)
def test_another_agency_gets_404_on_every_sale_page(env: Env, path: str) -> None:
    sale_id = enter(env.staff_a(), tk(D1))
    assert env.manager_b().get(path.format(id=sale_id)).status_code == 404


@pytest.mark.parametrize("path", POSTS)
def test_another_agency_gets_404_on_every_sale_post(env: Env, path: str) -> None:
    sale_id = enter(env.staff_a(), tk(D1))
    r = post(
        env.manager_b(), path.format(id=sale_id), form(version_no="1", reason="essai étranger")
    )
    assert r.status_code == 404
    assert latest(env, sale_id).version_no == 1


def test_an_unknown_or_oversized_sale_id_is_a_404(env: Env) -> None:
    c = env.staff_a()
    assert c.get("/sales/987654").status_code == 404
    assert c.get(f"/sales/{2**70}").status_code == 404
    assert c.get("/sales/abc").status_code == 400


def test_a_staff_member_edits_a_sale_of_a_past_day_that_is_not_closed(env: Env) -> None:
    sale_id = enter(env.manager_a(), tk(D1), staff_sign=SIGN, day=YESTERDAY)
    c = env.staff_a()
    assert c.get(f"/sales/{sale_id}").status_code == 200
    # an older sale: the agent gives a reason (owner's decision, 26 Oct)
    r = post(c, f"/sales/{sale_id}", form(pb="30000", version_no="1"), via="/sales/new")
    assert r.status_code == 400 and "motif" in text_of(r.text).lower()
    data = form(pb="30000", version_no="1", reason="prix corrigé")
    r = post(c, f"/sales/{sale_id}", data, via="/sales/new")
    assert r.status_code == 303 and latest(env, sale_id).version_no == 2
    set_day_status(env, "closed")  # a closed day refuses staff (tests/test_sale_day_pages.py)
    data = form(pb="31000", version_no="2", reason="prix corrigé")
    r = post(c, f"/sales/{sale_id}", data, via="/sales/new")
    assert r.status_code == 409 and "clôturée" in text_of(r.text)
    assert latest(env, sale_id).version_no == 2


# --- edit ---------------------------------------------------------------------------------------


def test_the_edit_form_is_prefilled_with_the_current_values(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, "21368"), tk(D2, "10000"), pb="35000", clt="Prefill Client")
    html = c.get(f"/sales/{sale_id}/edit").text
    assert html.count('name="doc_number"') == 2
    assert D1 in html and D2 in html and "Prefill Client" in html and "35000" in html
    assert f'action="/sales/{sale_id}/preview-edit"' in html
    assert 'name="version_no" value="1"' in html


def test_edit_read_back_then_confirm_appends_a_version(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, "21368"), pb="24000")
    before = counts(env)
    preview = post(
        c,
        f"/sales/{sale_id}/preview-edit",
        form(pb="26000", version_no="1"),
        via=f"/sales/{sale_id}/edit",
    )
    assert preview.status_code == 200 and fr(26000) in text_of(preview.text)
    assert counts(env) == before  # still nothing written
    r = confirm_with(c, preview.text, f"/sales/{sale_id}")
    assert r.status_code == 303 and r.headers["location"] == f"/sales/{sale_id}"
    assert latest(env, sale_id).version_no == 2 and latest(env, sale_id).pb == Decimal("26000")
    assert len([v for v in table(env, SaleVersion) if v.sale_id == sale_id]) == 2


def test_staff_may_edit_their_own_sale_of_today_without_a_reason(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    r = post(c, f"/sales/{sale_id}", form(pb="25000", version_no="1"), via="/sales/new")
    assert r.status_code == 303
    assert latest(env, sale_id).reason is None


def test_a_manager_edit_without_a_reason_is_a_field_error(env: Env) -> None:
    sale_id = enter(env.staff_a(), tk(D1))
    m = env.manager_a()
    for path in (f"/sales/{sale_id}/preview-edit", f"/sales/{sale_id}"):
        r = post(m, path, form(pb="25000", version_no="1", staff_sign=SIGN), via="/sales/new")
        assert r.status_code == 400, path
        assert "Indiquez le motif" in r.text and 'id="reason-error"' in r.text
    assert latest(env, sale_id).version_no == 1


def test_an_accountant_edit_needs_a_reason_and_records_it(env: Env) -> None:
    sale_id = enter(env.staff_a(), tk(D1))
    a = accountant(env)
    bad = post(a, f"/sales/{sale_id}", form(pb="25000", version_no="1", staff_sign=SIGN, reason=""))
    assert bad.status_code == 400
    ok = post(
        a,
        f"/sales/{sale_id}",
        form(pb="25000", version_no="1", staff_sign=SIGN, reason="prix confirmé par téléphone"),
    )
    assert ok.status_code == 303
    assert latest(env, sale_id).reason == "prix confirmé par téléphone"


def test_an_edit_built_on_an_old_version_says_reload(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    post(c, f"/sales/{sale_id}", form(pb="25000", version_no="1"), via="/sales/new")
    for path in (f"/sales/{sale_id}/preview-edit", f"/sales/{sale_id}"):
        r = post(c, path, form(pb="27000", version_no="1"), via="/sales/new")
        assert r.status_code == 409 and "rechargez la page" in r.text, path
    assert latest(env, sale_id).version_no == 2


def test_a_missing_or_garbled_version_is_refused_not_trusted(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    for version in ("", "abc", "-1", "99999999999999999999"):
        r = post(c, f"/sales/{sale_id}", form(pb="25000", version_no=version), via="/sales/new")
        assert r.status_code in (400, 409), version
        assert "Traceback" not in r.text
    assert latest(env, sale_id).version_no == 1


def test_an_edit_that_changes_nothing_says_so(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, "21368"), pb="24000")
    r = post(
        c,
        f"/sales/{sale_id}",
        form(tk(D1, "21368"), pb="24000", version_no="1"),
        via="/sales/new",
    )
    assert r.status_code == 400 and "Aucune modification" in r.text


def test_editing_into_a_ticket_of_another_sale_is_a_duplicate(env: Env) -> None:
    c = env.staff_a()
    enter(c, tk(D2))
    sale_id = enter(c, tk(D1))
    r = post(c, f"/sales/{sale_id}", form(tk(D2), version_no="1"), via="/sales/new")
    assert r.status_code == 409 and "Modifier la vente existante" in r.text


def test_a_voided_sale_cannot_be_edited(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c)
    post(
        c,
        f"/sales/{sale_id}/void",
        {"reason": "erreur de saisie", "version_no": "1"},
        via="/sales/new",
    )
    r = post(c, f"/sales/{sale_id}", form(pb="25000", version_no="2"), via="/sales/new")
    assert r.status_code == 409 and "annulée" in r.text
    assert c.get(f"/sales/{sale_id}/edit").status_code in (200, 409)
    assert "annulée" in text_of(c.get(f"/sales/{sale_id}/edit").text)


def test_the_back_to_form_button_redisplays_the_values_without_validating(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    r = post(
        c,
        f"/sales/{sale_id}/edit",
        form(clt="Typed Again", version_no="1"),
        via="/sales/new",
    )
    assert r.status_code == 200 and "Typed Again" in r.text
    r = post(c, "/sales/new", form(clt="Typed New"))
    assert r.status_code == 200 and "Typed New" in r.text


# --- void ---------------------------------------------------------------------------------------


def test_void_confirmation_page_then_void(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    page = c.get(f"/sales/{sale_id}/void")
    assert page.status_code == 200 and f'action="/sales/{sale_id}/void"' in page.text
    assert 'name="reason"' in page.text and D1 in page.text
    assert latest(env, sale_id).version_no == 1  # a GET changes nothing
    r = post(c, f"/sales/{sale_id}/void", {"reason": "saisie en double", "version_no": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"/sales/{sale_id}"
    assert latest(env, sale_id).version_no == 2
    assert [s.status for s in table(env, Sale)] == ["voided"]
    assert table(env, SaleTicket) == []  # the ticket number is free again
    enter(c, tk(D1))  # and can be sold again


def test_void_needs_a_reason(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c)
    for reason in ("", "   ", "abc"):
        r = post(c, f"/sales/{sale_id}/void", {"reason": reason, "version_no": "1"})
        assert r.status_code == 400 and "motif" in r.text and 'id="reason-error"' in r.text, reason
    assert [s.status for s in table(env, Sale)] == ["active"]


def test_void_built_on_an_old_version_says_reload(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c)
    post(c, f"/sales/{sale_id}", form(pb="25000", version_no="1"), via="/sales/new")
    r = post(c, f"/sales/{sale_id}/void", {"reason": "saisie en double", "version_no": "1"})
    assert r.status_code == 409 and "rechargez la page" in r.text
    assert [s.status for s in table(env, Sale)] == ["active"]


def test_manager_can_void_any_sale_with_a_reason(env: Env) -> None:
    sale_id = enter(env.staff_a(), tk(D1))
    r = post(
        env.manager_a(), f"/sales/{sale_id}/void", {"reason": "doublon constaté", "version_no": "1"}
    )
    assert r.status_code == 303


# --- security -----------------------------------------------------------------------------------


ALL_POSTS = [
    "/sales/paste",
    "/sales/paste/list",
    "/sales/screenshot",
    "/sales/preview",
    "/sales/new",
    "/sales",
    "/sales/{id}/edit",
    "/sales/{id}/preview-edit",
    "/sales/{id}",
    "/sales/{id}/void",
]


def test_the_post_list_below_is_every_post_route_of_the_sale_pages() -> None:
    from fastapi.routing import APIRoute

    from app.web.csrf import csrf_protect
    from app.web.routes import sale_entry
    from app.web.routes import sales as sales_routes

    found: set[str] = set()
    for router in (sale_entry.router, sales_routes.router, sale_entry.screenshot_router):
        for r in router.routes:
            if isinstance(r, APIRoute) and "POST" in (r.methods or set()):
                found.add(r.path.replace("{sale_id}", "{id}"))
        # the CSRF check is on each router, so it covers every route, present and future; the
        # screenshot router is the one exception: its route checks Origin and the token itself,
        # after login and before it reads the body (tests/test_sale_screenshot.py)
        if router is not sale_entry.screenshot_router:
            assert any(d.dependency is csrf_protect for d in router.dependencies)
    assert found == set(ALL_POSTS)


@pytest.mark.parametrize("path", ALL_POSTS)
def test_every_post_without_the_csrf_token_is_a_403(env: Env, path: str) -> None:
    from app.ai.fake import FakeTranscribeClient

    env.factory.app.state.ai_client = FakeTranscribeClient()  # type: ignore[attr-defined]
    c = env.staff_a()  # (the screenshot route is a 404 while the feature is off)
    sale_id = enter(c, tk(D1))
    before = counts(env)[:3]
    url = path.format(id=sale_id)
    # the screenshot route reads only a few small fields (a sale form is "too many")
    fields = {"reason": "essai"} if path == "/sales/screenshot" else form(version_no="1")
    no_token = c.post(url, data={**fields, "reason": "essai sans jeton"})
    assert no_token.status_code == 403, url
    wrong = c.post(url, data={**fields, "csrf_token": "x" * 40})
    assert wrong.status_code == 403
    foreign = c.post(
        url,
        data={**fields, "csrf_token": csrf_for(c, "/sales/new")},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert counts(env)[:3] == before


def test_the_client_name_is_escaped_everywhere(env: Env) -> None:
    c = env.staff_a()
    nasty = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    preview = post(c, "/sales/preview", form(clt=nasty))
    assert "<script>alert" not in preview.text and "<img src=x" not in preview.text
    assert "&lt;script&gt;" in preview.text
    sale_id = sale_id_of(confirm_with(c, preview.text, "/sales"))
    for path in (f"/sales/{sale_id}", f"/sales/{sale_id}/edit", f"/sales/{sale_id}/void", "/sales"):
        html = c.get(path).text
        assert "<script>alert" not in html and "<img src=x" not in html, path
    bad = post(c, "/sales/preview", form(clt=nasty, pb="abc"))
    assert "<script>alert" not in bad.text and "<img src=x" not in bad.text


def test_pax_name_and_reasons_are_escaped_too(env: Env) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1, pax_name="<b>X</b>/PAX"))
    post(
        c,
        f"/sales/{sale_id}",
        form(
            tk(D1, pax_name="<b>X</b>/PAX"), pb="25000", version_no="1", reason="<i>raison</i> ok"
        ),
        via="/sales/new",
    )
    html = c.get(f"/sales/{sale_id}").text
    assert "<b>X</b>" not in html and "<i>raison</i>" not in html


PAGES = ["/sales/new", "/sales", "/sales/{id}", "/sales/{id}/edit", "/sales/{id}/void"]


@pytest.mark.parametrize("path", PAGES)
def test_pages_are_never_cached_and_have_no_inline_style_or_script(env: Env, path: str) -> None:
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    r = c.get(path.format(id=sale_id))
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    html = r.text
    assert not re.search(r"\sstyle\s*=", html, re.I) and "<style" not in html.lower()
    assert not re.search(r"<script(?![^>]*\ssrc=)", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)
    assert '<html lang="fr"' in html
    assert "content-security-policy" in r.headers


def test_post_responses_are_not_cached_either(env: Env) -> None:
    c = env.staff_a()
    for data in (form(), form(clt="")):
        r = post(c, "/sales/preview", data)
        assert r.headers["cache-control"] == "no-store"
    r = post(c, "/sales", form())
    assert r.headers.get("cache-control") == "no-store"


def test_sales_creation_is_rate_limited_per_user(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "SALE_WRITE_USER", ratelimit.Limit("sale_write_user", 2, 3600))
    c = env.staff_a()
    enter(c, tk(D1))
    enter(c, tk(D2))
    r = post(c, "/sales", form(tk(D3)))
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert len(table(env, Sale)) == 2
    # a different user has their own budget
    assert enter(env.manager_a(), tk(D4), staff_sign=SIGN, day=TODAY) > 0
    limited = [e for e in table(env, AuditEvent) if e.event_type == "rate_limited"]
    assert len(limited) == 1 and limited[0].details["scope"] == "sale_write_user"


def test_edits_and_voids_share_the_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ratelimit, "SALE_WRITE_USER", ratelimit.Limit("sale_write_user", 3, 3600))
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    assert post(c, f"/sales/{sale_id}", form(pb="25000", version_no="1")).status_code == 303
    assert post(c, f"/sales/{sale_id}", form(pb="26000", version_no="2")).status_code == 303
    assert post(c, f"/sales/{sale_id}", form(pb="27000", version_no="3")).status_code == 429
    assert (
        post(c, f"/sales/{sale_id}/void", {"reason": "doublon", "version_no": "1"}).status_code
        == 429
    )


def test_the_limit_defaults_to_300_an_hour() -> None:
    assert (ratelimit.SALE_WRITE_USER.max_hits, ratelimit.SALE_WRITE_USER.window_seconds) == (
        300,
        3600,
    )


# --- navigation ---------------------------------------------------------------------------------


def tabbar(html: str) -> str:
    return html.split('<nav class="tabbar"')[1].split("</nav>")[0]


def sidebar(html: str) -> str:
    return html.split('<div class="sidebar">')[1].split('<div class="content-col">')[0]


def test_staff_have_the_sales_tabs_in_the_sidebar_and_the_bottom_bar(env: Env) -> None:
    html = env.staff_a().get("/").text
    side, bar = sidebar(html), tabbar(html)
    for href in ("/sales/new", "/sales"):
        assert f'href="{href}"' in side and f'href="{href}"' in bar, href
    assert "Saisir une vente" in text_of(bar) and "Mes ventes" in text_of(bar)
    assert bar.count('class="tab"') == 5  # five fit a 320px screen
    labels = [text_of(x).strip() for x in re.findall(r'<span class="tab-label">(.*?)</span>', bar)]
    assert labels == [
        "Tableau de bord",
        "Saisir une vente",
        "Mes ventes",
        "Envoyer un rapport",
        "Compte",
    ]
    assert "Ventes du jour" not in html


@pytest.mark.parametrize("who", ["manager", "accountant"])
def test_manager_and_accountant_have_ventes_du_jour_in_the_sidebar(env: Env, who: str) -> None:
    env.load()
    c = env.manager_a() if who == "manager" else accountant(env)
    html = c.get("/").text
    assert 'href="/sales"' in sidebar(html) and "Ventes du jour" in text_of(sidebar(html))
    assert "Mes ventes" not in html
    assert tabbar(html).count('class="tab"') == 5
    assert (
        'href="/sales?date=' in html.split('<div class="content-col">')[1]
    )  # also on the dashboard


def test_the_sales_entries_are_current_on_their_pages(env: Env) -> None:
    c = env.staff_a()
    new = c.get("/sales/new").text
    assert re.search(r'href="/sales/new" aria-current="page"', new)
    assert re.search(r'href="/sales" aria-current="page"', c.get("/sales").text)
    sale_id = enter(c)
    assert re.search(r'href="/sales" aria-current="page"', c.get(f"/sales/{sale_id}").text)
