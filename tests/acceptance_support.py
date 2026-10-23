"""Helpers of the end-to-end acceptance tests (B11): a world driven through the web client only.

The browser is simulated the honest way: a page is fetched, the fields of its form are read
exactly as a browser would send them (hidden inputs, the CSRF token, the selected option, the
checked radio), the test overrides what a person would type, and the form is posted. Nothing is
written to the database except the first setup (the agency, its users, their sign links).
Everything is synthetic.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx2 as httpx
import openpyxl
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import ComparisonFlag, ReportDay
from app.tenancy import rebind_tenant
from tests.flag_support import FlagWeb
from tests.web_support import PASSWORD_S, add_user, csrf_for, owner_session

ACCOUNTANT_PHONE = "+22212345710"
AGENT1_PHONE = "+22212345711"  # signs 0004SV and 0006AC
AGENT2_PHONE = "+22212345712"  # sign 0009ER
SIGN_SV, SIGN_AC, SIGN_ER = "0004SV", "0006AC", "0009ER"
DAY = "2026-10-20"
EVENING = datetime(2026, 10, 20, 18, 0, tzinfo=UTC)
NEXT_MORNING = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
MORNING = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
FIRST_ROW = 6


# --- reading a page the way a browser does ---------------------------------------------------


class _Form(HTMLParser):
    """The fields a browser would send for the form posting to ``action``."""

    def __init__(self, action: str) -> None:
        super().__init__()
        self.action = action
        self.found = False
        self.inside = False
        self.fields: list[tuple[str, str]] = []
        self._select: str | None = None
        self._options: list[tuple[str, bool]] = []
        self._textarea: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v if v is not None else "" for k, v in attrs}
        if tag == "form":
            self.inside = a.get("action") == self.action
            self.found = self.found or self.inside
            return
        if not self.inside:
            return
        if tag == "input" and a.get("name"):
            kind = a.get("type", "text")
            if kind in ("submit", "button", "image", "file", "reset"):
                return
            if kind in ("radio", "checkbox") and "checked" not in a:
                return
            self.fields.append((a["name"], a.get("value", "on" if kind == "checkbox" else "")))
        elif tag == "select" and a.get("name"):
            self._select, self._options = a["name"], []
        elif tag == "option" and self._select is not None:
            self._options.append((a.get("value", ""), "selected" in a))
        elif tag == "textarea" and a.get("name"):
            self._textarea, self._text = a["name"], []

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.inside = False
        elif tag == "select" and self._select is not None:
            chosen = [v for v, picked in self._options if picked] or [
                v for v, _ in self._options[:1]
            ]
            self.fields.extend((self._select, v) for v in chosen)
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            self.fields.append((self._textarea, "".join(self._text).lstrip("\n")))
            self._textarea = None


def fields_of(html: str, action: str) -> dict[str, list[str]]:
    """What the form posting to ``action`` sends, name by name (a list: ticket rows repeat)."""
    parser = _Form(action)
    parser.feed(html)
    assert parser.found, f"no form posting to {action}"
    data: dict[str, list[str]] = {}
    for name, value in parser.fields:
        data.setdefault(name, []).append(unescape(value))
    return data


def text_of(html: str) -> str:
    return re.sub(r"[ \t\r\n]+", " ", unescape(re.sub(r"<[^>]+>", " ", html)))


def dd(html: str, label: str) -> str:
    """The value printed after ``label`` in a description list of a page."""
    match = re.search(rf"<dt>{re.escape(label)}</dt>\s*<dd[^>]*>(.*?)</dd>", html, re.S)
    assert match, f"no {label!r} on the page"
    return text_of(match.group(1)).strip()


# --- the sales ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One ticket of a sale as the report has it."""

    doc: str
    airline: str
    cost: str
    name: str
    rloc: str
    pax_type: str = "ADT"
    trnc: str = "TKTT"


def ticket_columns(rows: Sequence[Row]) -> dict[str, list[str]]:
    return {
        "doc_number": [r.doc for r in rows],
        "airline": [r.airline for r in rows],
        "total_doc": [r.cost for r in rows],
        "pax_name": [r.name for r in rows],
        "pax_type": [r.pax_type for r in rows],
        "rloc": [r.rloc for r in rows],
        "trnc": [r.trnc for r in rows],
    }


def sale_id_from(response: httpx.Response) -> int:
    assert response.status_code == 303, response.text[:1500]
    match = re.fullmatch(r"/sales/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def override(data: dict[str, list[str]], **values: str | Sequence[str]) -> dict[str, list[str]]:
    """``data`` with the given fields replaced (a plain string is one value)."""
    out = {k: list(v) for k, v in data.items()}
    for key, value in values.items():
        out[key] = [value] if isinstance(value, str) else list(value)
    return out


def preview_then_confirm(
    c: TestClient,
    data: dict[str, list[str]],
    *,
    preview: str = "/sales/preview",
    confirm: str = "/sales",
) -> httpx.Response:
    """The two steps of a sale: the read-back (nothing written), then the confirm that posts the
    read-back's own hidden fields."""
    readback = c.post(preview, data=data, follow_redirects=False)
    assert readback.status_code == 200, text_of(readback.text)[:1500]
    hidden = fields_of(readback.text, confirm)
    posted: httpx.Response = c.post(confirm, data=hidden, follow_redirects=False)
    return posted


def enter_sale(c: TestClient, rows: Sequence[Row], sign: str, **fields: str | Sequence[str]) -> int:
    """A sale through the B5 form: fetch the blank form, fill it, read-back, confirm."""
    blank = fields_of(c.get("/sales/new").text, "/sales/preview")
    data = override(blank, staff_sign=sign, **ticket_columns(rows), **fields)
    return sale_id_from(preview_then_confirm(c, data))


def direct(price: int | str, client: str, phone: str = "") -> dict[str, str | Sequence[str]]:
    """The fields of a direct sale; a missing phone means the client refused to give it."""
    fields: dict[str, str | Sequence[str]] = {
        "ag": "direct",
        "pb": str(price),
        "clt": client,
        "payment_method": "cash",
    }
    if phone:
        fields["ap"] = phone
    else:
        fields["client_refused_phone"] = "1"
    return fields


def pnr_text(
    *,
    row: Row,
    agency: str,
    sign: str = "SV",
    price: int | None = None,
    payer: str = "SYNTHCLIENT PAYEUR",
    phone: str = "+222 37353400",
) -> str:
    """A synthetic Amadeus booking screen (shape of tests/fixtures/pnr) for one passenger and one
    ticket of the report. ``price`` is the RM line of a direct sale; a partner booking has none."""
    rm = f"  7 RM {price} MRU\n" if price else ""
    return (
        "--- TST RLR ---\n"
        f"RP/NKC262213/NKC262213            {sign}/SU  20OCT26/1659Z   {row.rloc}\n"
        f"  1.{row.name}\n"
        "  2  L6 021 Y 30OCT 5 EMNNKC HK1           0930 1100   *1A/E*\n"
        f"  3 AP NKC {phone} - {agency} - A\n"
        "  4 TK OK20OCT/NKC262213//ETL6\n"
        f"{rm}"
        f"  8 FA PAX {row.airline}-{row.doc}/ETL6/MRU{row.cost}.00/20OCT26/NKC262213/5121\n"
        "       1381/S2\n"
        "  9 FB PAX 0000000000 TTP/RT OK ETICKET/S2\n"
        " 10 FM PAX *C*1/S2\n"
        f" 11 FP NONREF {payer}\n"
        " 12 FV PAX L6/S2\n"
        "\n>\n"
    )


def paste(c: TestClient, text: str) -> httpx.Response:
    posted: httpx.Response = c.post(
        "/sales/paste",
        data={"pnr_text": text, "csrf_token": csrf_for(c, "/sales/new")},
        follow_redirects=False,
    )
    return posted


# --- the workbook -------------------------------------------------------------------------------


def open_sheet(data: bytes) -> Any:
    wb = openpyxl.load_workbook(io.BytesIO(data))  # formulas stay formulas: we would see them
    ws = wb.active
    assert ws is not None
    return ws


def data_rows(ws: Any, count: int) -> list[list[Any]]:
    return [
        [ws.cell(row=FIRST_ROW + i, column=c).value for c in range(1, 13)] for i in range(count)
    ]


def dec(value: object) -> Decimal:
    return Decimal(str(value))


# --- the world ----------------------------------------------------------------------------------


class Bruge(FlagWeb):
    """One agency at work: its manager, an accountant, two agents (agent 1 holds the signs 0004SV
    and 0006AC, agent 2 holds 0009ER) and a second, unrelated agency. The clock stands on the
    morning of 20 October 2026."""

    def __init__(self, web_db: Any) -> None:
        super().__init__(web_db)
        self.clock.now = MORNING
        agency = self.seeded.agency_a
        add_user(
            web_db.owner_url,
            agency,
            ACCOUNTANT_PHONE,
            role="accountant",
            password=PASSWORD_S,
            display_name="Comptable",
        )
        agent2 = add_user(
            web_db.owner_url, agency, AGENT2_PHONE, password=PASSWORD_S, display_name="Agent ER"
        )
        self.link_staff(SIGN_SV, SIGN_AC)  # the seeded staff member is agent 1
        from app.models import StaffSignLink

        with owner_session(web_db.owner_url) as s:
            rebind_tenant(s, agency)
            s.add(StaffSignLink(agency_id=agency, user_id=agent2, staff_sign=SIGN_ER))
            s.commit()

    def accountant(self) -> TestClient:
        client: TestClient = self.client(ACCOUNTANT_PHONE, PASSWORD_S)
        return client

    def agent1(self) -> TestClient:
        client: TestClient = self.staff()
        return client

    def agent2(self) -> TestClient:
        client: TestClient = self.client(AGENT2_PHONE, PASSWORD_S)
        return client

    def post_form(
        self,
        c: TestClient,
        page: str,
        action: str,
        extra: Mapping[str, str | Sequence[str]] | None = None,
        *,
        follow: bool = False,
    ) -> httpx.Response:
        """Fetch ``page``, read the form posting to ``action`` and post it with ``extra`` typed."""
        got = c.get(page)
        assert got.status_code == 200, (page, got.status_code)
        data = override(fields_of(got.text, action), **(extra or {}))
        posted: httpx.Response = c.post(action, data=data, follow_redirects=follow)
        return posted

    def flags_of(self, doc: str, kind: str | None = None) -> list[ComparisonFlag]:
        return [
            f for f in self.flags_rows() if f.doc_number == doc and (kind is None or f.kind == kind)
        ]

    def live_flags(self) -> list[ComparisonFlag]:
        return [f for f in self.flags_rows() if f.status in ("open", "answered", "resolved")]

    def blocking_open(self) -> list[ComparisonFlag]:
        return [f for f in self.live_flags() if f.blocking and f.status in ("open", "answered")]

    def day_status(self) -> str:
        with owner_session(self.db.owner_url) as s:
            rebind_tenant(s, self.seeded.agency_a)
            return str(s.scalars(select(ReportDay.status)).one())

    def run_comparison(self, c: TestClient) -> httpx.Response:
        return self.post_form(c, f"/flags?date={DAY}", f"/flags/run?date={DAY}", follow=False)
