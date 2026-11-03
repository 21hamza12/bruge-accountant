"""B11: phase B, end to end, on the made-up 20 October 2026 (tests/fixtures/synthetic_20oct.py).

A whole day at the agency, through the web client only (real CSRF, real logins, forms read the way
a browser sends them): the partners and their names, the agents entering the 34 tickets (form and
paste, a layover sale, partner sales, direct sales), the accountant uploading the Amadeus report,
the comparison, an agent's typo, a ticket nobody entered, a sign the agent keeps, the close gate,
the TJQ APRES, who sees what, and the manager reopening the day. Synthetic data only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models import (
    ALIAS_APPROVED,
    ALIAS_PENDING,
    DayExport,
    FlagDecision,
    PartnerAlias,
    SaleVersion,
)
from app.parsing.amadeus import parse_report
from app.tenancy import rebind_tenant
from tests.acceptance_support import (
    DAY,
    EVENING,
    FIRST_ROW,
    NEXT_MORNING,
    SIGN_AC,
    SIGN_ER,
    SIGN_SV,
    XLSX,
    Bruge,
    Row,
    data_rows,
    dd,
    dec,
    direct,
    enter_sale,
    fields_of,
    open_sheet,
    override,
    paste,
    pnr_text,
    preview_then_confirm,
    sale_id_from,
    text_of,
)
from tests.fixtures import synthetic_20oct as fx
from tests.web_support import WebDb, csrf_for, owner_session

REND = "NOUR TRAVEL AGENCY"  # how a booking of the partner NOUR VOYAGES names its agency
SAHEL = "SAHEL VOYAGE"
EXTRA_DOC = "1993900001"  # a partner ticket issued elsewhere: not in the report


def report_rows() -> dict[int, Row]:
    """The 34 TKTT lines of the report, by SEQ number, as the agents would type them."""
    out: dict[int, Row] = {}
    for t in parse_report(fx.build_synthetic_20oct()).tickets:
        if t.transaction_type == "TKTT":
            assert t.cost is not None
            out[int(t.seq_no)] = Row(
                t.ticket_no, t.airline, str(int(t.cost)), t.passenger_name, t.booking_ref,
                t.pax_type,
            )  # fmt: skip
    assert len(out) == fx.TKTT_COUNT
    return out


ROWS = report_rows()
SIGN_OF = {int(x["SEQ NO"]): str(x["AS"]) for x in fx.LINES if x["TRNC"] == "TKTT"}
CANX_DOCS = [str(x["DOC NUMBER"]) for x in fx.LINES if x["TRNC"] == "CANX"]


def cost(seq: int) -> int:
    return int(ROWS[seq].cost)


def phone(seq: int) -> str:
    return f"+2223600{seq:04d}"


class Day:
    """What the day entered, so the export can be checked against it."""

    def __init__(self, w: Bruge) -> None:
        self.w = w
        self.sale: dict[int, int] = {}  # SEQ number -> sale id
        self.pb: dict[int, Decimal] = {}  # SEQ number -> P.B expected on its line
        self.ag: dict[int, str] = {}  # SEQ number -> AG expected on its line
        self.pasted: set[int] = set()
        self.partner: dict[str, int] = {}
        self.aliases: dict[str, int] = {}

    # --- setup ------------------------------------------------------------------------------

    def open_partners(self) -> None:
        w = self.w
        with w.manager() as m:
            for name, fee in (("BADI", "300"), ("NOUR VOYAGES", "250")):
                r = w.post_form(m, "/partners", "/partners", {"name": name})
                assert r.status_code == 303, text_of(r.text)[:500]
                pid = int(r.headers["location"].rsplit("/", 1)[1])
                self.partner[name] = pid
                fee_page = f"/partners/{pid}"
                r = w.post_form(
                    m, fee_page, f"/partners/{pid}/fees", {"fee": fee, "valid_from": "2026-01-01"}
                )
                assert r.status_code == 303, text_of(r.text)[:500]

    # --- entering ---------------------------------------------------------------------------

    def by_form(self, c, seqs: list[int], *, fee: int | None = None, partner: str = "") -> int:  # type: ignore[no-untyped-def]
        rows = [ROWS[s] for s in seqs]
        sign = SIGN_OF[seqs[0]]
        assert {SIGN_OF[s] for s in seqs} == {sign}
        total = sum(cost(s) for s in seqs)
        if partner:
            pid = self.partner[partner]
            fields: dict[str, str | Sequence[str]] = {"ag": str(pid), "payment_method": "credit"}
            expected, label = Decimal(total + (fee or 0) * len(seqs)), partner
        else:
            price = total + 1500 * len(seqs)
            fields = direct(price, f"Client {seqs[0]:02d}", phone(seqs[0]))
            expected, label = Decimal(price), "BRUGE"
        sale_id = enter_sale(c, rows, sign, **fields)
        for i, s in enumerate(seqs):
            self.sale[s] = sale_id
            self.pb[s] = expected if i == 0 else Decimal(0)
            self.ag[s] = label
        return sale_id

    def by_paste(self, c, seq: int, agency: str, answer: dict[str, str], **kw) -> int:  # type: ignore[no-untyped-def]
        """A booking screen pasted, the agency question answered, read-back, confirm."""
        row = ROWS[seq]
        text = pnr_text(row=row, agency=agency, **kw)
        got = paste(c, text)
        assert got.status_code == 200, text_of(got.text)[:800]
        form = fields_of(got.text, "/sales/preview")
        assert form["doc_number"] == [row.doc] and form["total_doc"] == [row.cost]
        assert form["pax_name"] == [row.name] and form["rloc"] == [row.rloc]
        assert form["source"] == ["paste"]
        # the agent has to answer the agency question: without it the read-back is refused
        unanswered = c.post("/sales/preview", data=override(form, staff_sign=SIGN_SV))
        assert unanswered.status_code == 400
        assert "Confirmez l'agence" in text_of(unanswered.text)
        data = override(form, staff_sign=SIGN_SV, payment_method="cash", **answer)
        sale_id = sale_id_from(preview_then_confirm(c, data))
        self.sale[seq] = sale_id
        self.pasted.add(seq)
        return sale_id


@pytest.fixture
def w(web_db: WebDb) -> Bruge:
    return Bruge(web_db)


def enter_the_day(w: Bruge) -> Day:
    """Steps 1 and 2: the partners, the aliases and every ticket of the report, entered by the
    two agents (agent 1 under 0004SV and 0006AC, agent 2 under 0009ER)."""
    day = Day(w)
    day.open_partners()
    badi, nour = day.partner["BADI"], day.partner["NOUR VOYAGES"]
    a1, a2 = w.agent1(), w.agent2()
    manager = w.manager()

    # --- agent 1, sign 0004SV: four pasted bookings, a layover, a partner pair, direct sales ----
    # (1) an agency name nobody knows: the agent says it is NOUR, which proposes the name
    day.by_paste(a1, 9, REND, {"ag_answer": "other", "ag_other": str(nour)})
    day.pb[9], day.ag[9] = Decimal(cost(9) + 250), "NOUR VOYAGES"
    pending = [a for a in aliases(w) if a.status == ALIAS_PENDING]
    assert [(a.alias, a.partner_id) for a in pending] == [(REND, nour)]
    # (2) the manager approves it: the next booking of that name is recognised as NOUR
    approve = manager.get("/partners")
    assert REND in approve.text
    r = w.post_form(manager, "/partners", f"/partners/aliases/{pending[0].id}/approve")
    assert r.status_code == 303
    assert [a.status for a in aliases(w)] == [ALIAS_APPROVED]
    again = paste(a1, pnr_text(row=ROWS[12], agency=REND))
    assert "Reconnue comme partenaire NOUR VOYAGES (frais 250 MRU/billet)" in text_of(again.text)
    day.by_paste(a1, 12, REND, {"ag_answer": "yes"})
    day.pb[12], day.ag[12] = Decimal(cost(12) + 250), "NOUR VOYAGES"
    # (3) a second unknown name, chosen as BADI: pending, and never believed by the next paste
    day.by_paste(a1, 18, SAHEL, {"ag_answer": "other", "ag_other": str(badi)})
    day.pb[18], day.ag[18] = Decimal(cost(18) + 300), "BADI"
    assert [(a.alias, a.status) for a in aliases(w) if a.alias == SAHEL] == [(SAHEL, ALIAS_PENDING)]
    nothing = paste(a1, pnr_text(row=ROWS[21], agency=SAHEL, price=cost(21) + 1500))
    assert "Aucun partenaire reconnu" in text_of(nothing.text)
    assert 'id="ag_answer_yes"' not in nothing.text  # nothing to say Oui to
    # (4) so that booking is sold direct, with the RM price, the payer and the phone of the screen
    price = str(cost(21) + 1500)
    day.by_paste(
        a1, 21, SAHEL, {"ag_answer": "direct", "pb": price, "ap": phone(21)}, price=cost(21) + 1500
    )
    day.pb[21], day.ag[21] = Decimal(cost(21) + 1500), "BRUGE"
    # the layover: two tickets, one sale, one P.B
    day.by_form(a1, [3, 6])
    # a partner sale of two tickets: P.B = cost + fee x tickets
    day.by_form(a1, [24, 27], fee=300, partner="BADI")
    for seq in (33, 36, 39, 42):
        day.by_form(a1, [seq])
    # --- agent 1, sign 0006AC -------------------------------------------------------------------
    for seq in (s for s, sign in SIGN_OF.items() if sign == SIGN_AC):
        day.by_form(a1, [seq])
    # --- agent 2, sign 0009ER --------------------------------------------------------------------
    for seq in (s for s, sign in SIGN_OF.items() if sign == SIGN_ER):
        day.by_form(a2, [seq])
    return day


def aliases(w: Bruge) -> list[PartnerAlias]:
    with owner_session(w.db.owner_url) as s:
        rebind_tenant(s, w.seeded.agency_a)
        rows = list(s.scalars(select(PartnerAlias).order_by(PartnerAlias.id)))
        s.expunge_all()
        return rows


# --- the day, step by step -----------------------------------------------------------------------


def post(c, page: str, path: str, **data: str):  # type: ignore[no-untyped-def]
    """A POST with the CSRF token of ``page`` (a page that carries it), like a button would."""
    return c.post(path, data={"csrf_token": csrf_for(c, page), **data}, follow_redirects=False)


def upload_report(w: Bruge, c) -> None:  # type: ignore[no-untyped-def]
    r = w.upload(c)
    assert r.status_code == 303, text_of(r.text)[:600]


def flag_id(w: Bruge, seq: int, kind: str, field: str | None = None) -> int:
    found = [
        f
        for f in w.live_flags()
        if f.doc_number == ROWS[seq].doc and f.kind == kind and (field is None or f.field == field)
    ]
    assert len(found) == 1, (seq, kind, field, [(f.kind, f.field, f.status) for f in found])
    return found[0].id


def flag_status(w: Bruge, flag: int) -> str:
    return next(f.status for f in w.flags_rows() if f.id == flag)


def decided(w: Bruge, flag: int) -> list[str]:
    """The choices recorded on a flag, in order (the agent's answer, then the validator's)."""
    with owner_session(w.db.owner_url) as s:
        rebind_tenant(s, w.seeded.agency_a)
        rows = s.scalars(
            select(FlagDecision).where(FlagDecision.flag_id == flag).order_by(FlagDecision.id)
        )
        return [d.choice for d in rows]


def sale_version(w: Bruge, sale_id: int) -> int:
    with owner_session(w.db.owner_url) as s:
        rebind_tenant(s, w.seeded.agency_a)
        versions = s.scalars(select(SaleVersion.version_no).where(SaleVersion.sale_id == sale_id))
        return max(versions)


def stored_versions(w: Bruge) -> list[int]:
    with owner_session(w.db.owner_url) as s:
        rebind_tenant(s, w.seeded.agency_a)
        return sorted(s.scalars(select(DayExport.version)))


def edit_sale(w: Bruge, c, sale_id: int, reason: str, **fields: str) -> None:  # type: ignore[no-untyped-def]
    """An edit through the form: read-back, then confirm, built on the version on the page."""
    page = c.get(f"/sales/{sale_id}/edit")
    assert page.status_code == 200, text_of(page.text)[:600]
    data = override(fields_of(page.text, f"/sales/{sale_id}/preview-edit"), reason=reason, **fields)
    done = preview_then_confirm(
        c, data, preview=f"/sales/{sale_id}/preview-edit", confirm=f"/sales/{sale_id}"
    )
    assert done.status_code == 303, text_of(done.text)[:800]


def assert_only_own(html: str, own_sign: str) -> None:
    """No ticket of another sign appears on the page (CANX lines included)."""
    for seq, sign in SIGN_OF.items():
        if sign != own_sign:
            assert ROWS[seq].doc not in html, f"{ROWS[seq].doc} of {sign} shown to {own_sign}"
    for x in fx.LINES:
        if x["TRNC"] == "CANX" and x["AS"] != own_sign:
            assert str(x["DOC NUMBER"]) not in html


def check_export(data: bytes, day: Day, *, corrected: dict[int, str] | None = None):  # type: ignore[no-untyped-def]
    """Step 5: the workbook against the report, the sales and the accountant's layout."""
    corrected = corrected or {}
    ws = open_sheet(data)
    assert [ws["A1"].value, ws["C1"].value, ws["D1"].value] == [
        "Agy no", "Report date range", "20OCT",
    ]  # fmt: skip
    assert [ws["C3"].value, ws["D3"].value] == ["Current date", fx.CURRENT_DATE]
    heads = [ws.cell(row=5, column=c).value for c in range(1, 13)]
    assert heads == ["SEQ NO", "A/L", "DOC NUMBER", "TOTAL DOC", "PAX NAME", "AS", "TRNC", "RLOC",
                     "P.B", "AG", "CLT", "AP"]  # fmt: skip
    rows = data_rows(ws, fx.TKTT_COUNT)
    assert ws.cell(row=FIRST_ROW + fx.TKTT_COUNT, column=1).value is None  # no 35th line, no CANX
    expected = [x for x in fx.LINES if x["TRNC"] == "TKTT"]
    assert len(expected) == 34
    seqs = [str(r[0]) for r in rows]
    assert seqs == sorted(seqs) == [str(x["SEQ NO"]) for x in expected]  # SEQ order
    for row, line in zip(rows, expected, strict=True):
        seq = int(str(line["SEQ NO"]))
        assert (row[1], row[2], row[4], row[6], row[7]) == (
            line["A/L"], line["DOC NUMBER"], line["PAX NAME"], "TKTT", line["RLOC"],
        )  # fmt: skip
        assert dec(row[3]) == Decimal(line["TOTAL DOC"])
        assert row[5] == corrected.get(seq, line["AS"])
    assert sum(dec(r[3]) for r in rows) == Decimal(fx.TKTT_TOTAL) == Decimal(624_126)
    by_seq = {int(str(r[0])): r for r in rows}
    # P.B: the full price on the first line of a sale, 0 on its other lines
    for seq, expected_pb in day.pb.items():
        assert dec(by_seq[seq][8]) == expected_pb, seq
        assert by_seq[seq][9] == day.ag[seq], seq
    assert dec(by_seq[3][8]) == cost(3) + cost(6) + 3000 and dec(by_seq[6][8]) == 0  # layover
    assert dec(by_seq[24][8]) == cost(24) + cost(27) + 2 * 300 and dec(by_seq[27][8]) == 0
    assert by_seq[24][9] == by_seq[27][9] == "BADI"
    assert dec(by_seq[12][8]) == cost(12) + 250 and by_seq[12][9] == "NOUR VOYAGES"
    # AG always; CLT and AP on every direct line; no formula anywhere
    for seq, r in by_seq.items():
        assert r[9]
        if r[9] == "BRUGE":
            assert r[10] and r[11], seq
    assert by_seq[33][10] == "Client 33" and by_seq[33][11] == phone(33)
    for row in ws.iter_rows():
        for cell in row:
            assert cell.data_type != "f", cell.coordinate
            assert not (isinstance(cell.value, str) and cell.value.startswith("=")), cell.coordinate
    # the totals row
    total_row = FIRST_ROW + fx.TKTT_COUNT + 1
    assert ws.cell(row=total_row, column=1).value == "TOTAUX"
    assert ws.cell(row=total_row, column=3).value == 34
    assert dec(ws.cell(row=total_row, column=4).value) == Decimal(624_126)
    assert dec(ws.cell(row=total_row, column=9).value) == sum(dec(r[8]) for r in rows)
    # corrections: highlighted, with the Amadeus original and who approved
    yellow = []
    position = {int(str(x["SEQ NO"])): i for i, x in enumerate(expected)}
    for seq, i in position.items():
        for col in range(1, 13):
            if ws.cell(row=FIRST_ROW + i, column=col).fill.fgColor.rgb == "FFFFFF00":
                yellow.append((seq, col))
    assert yellow == [(seq, 6) for seq in sorted(corrected)]
    for seq, value in corrected.items():
        cell = ws.cell(row=FIRST_ROW + position[seq], column=6)
        assert cell.value == value and cell.comment is not None
        assert SIGN_OF[seq] in cell.comment.text and "Comptable" in cell.comment.text
    return ws


def test_a_whole_day_with_its_troubles(w: Bruge) -> None:
    """Variant A: the clean day is not the one that happens. A typo, a ticket nobody entered, a sign
    the agent keeps, a partner ticket issued elsewhere: answered, validated, closed, exported,
    reopened and closed again."""
    day = enter_the_day(w)
    sale = day.sale
    assert sorted(sale) == sorted(ROWS)
    assert day.pasted == {9, 12, 18, 21}
    assert sale[3] == sale[6] and sale[24] == sale[27]
    assert len(set(sale.values())) == 34 - 2

    # --- 3. the accountant uploads the Amadeus report; the comparison runs by itself -------------
    w.clock.now = EVENING
    acc = w.accountant()
    upload_report(w, acc)
    assert w.live_flags() == [] and w.blocking_open() == []
    assert w.day_status() == "approved"
    listing = acc.get(f"/flags?date={DAY}")
    assert (
        dd(listing.text, "Écarts bloquants") == "0" and dd(listing.text, "Billets annulés") == "9"
    )
    shown = text_of(listing.text)
    assert all(doc in shown for doc in CANX_DOCS) and len(CANX_DOCS) == 9
    page = acc.get(f"/days/{DAY}")
    assert "Approuvée" in text_of(page.text) and dd(page.text, "Billets annulés") == "9"
    assert dd(page.text, "Billets partenaires absents du rapport") == "0"

    # --- 6. staff isolation: agent 2 (0009ER) never sees the sales of agent 1 -------------------
    a1, a2 = w.agent1(), w.agent2()
    mine = a2.get(f"/sales?date={DAY}")
    assert mine.status_code == 200
    own_seqs = [s for s, sign in SIGN_OF.items() if sign == SIGN_ER]
    assert dd(mine.text, "Ventes") == str(len(own_seqs))
    assert f"/sales/{sale[2]}" in mine.text and f"Client {2:02d}" in mine.text
    for seq in (3, 9, 24, 33, 37):  # clients and sales of the other signs
        assert (
            f"Client {seq:02d}" not in mine.text and f'href="/sales/{sale[seq]}"' not in mine.text
        )
    for seq in (3, 9, 24, 37):  # sales of 0004SV and 0006AC
        assert a2.get(f"/sales/{sale[seq]}").status_code == 404
        assert a2.get(f"/sales/{sale[seq]}/edit").status_code == 404
    day_page = a2.get(f"/days/{DAY}")
    assert_only_own(day_page.text, SIGN_ER)
    assert_only_own(a2.get(f"/flags?date={DAY}").text, SIGN_ER)
    for path in (f"/days/{DAY}/apres.xlsx", f"/days/{DAY}/apres-preview.xlsx", "/flags/validate"):
        assert a2.get(path).status_code == 403, path
    # the figures: an agent sees no day total, not even of his own tickets (decision 26)
    own_total = sum(cost(s) for s, sign in SIGN_OF.items() if sign == SIGN_ER)
    assert f"{own_total:,}".replace(",", " ") not in text_of(day_page.text)
    assert "Coût total" not in day_page.text
    # another agency sees nothing of this day
    other = w.other_manager()
    assert ROWS[2].doc not in other.get(f"/sales?date={DAY}").text
    assert other.get(f"/days/{DAY}/apres.xlsx").status_code == 404

    # --- 4. variant A: what goes wrong ------------------------------------------------------
    # (a) agent 2 mistypes a cost; (b) agent 1 re-signs a sale of 0006AC under 0004SV;
    # (c) the manager voids a sale of 0009ER; (d) a partner ticket issued in another office
    edit_sale(w, a2, sale[8], "erreur de frappe", total_doc="33010")
    edit_sale(w, a1, sale[37], "billet vendu sous mon signe", staff_sign=SIGN_SV)
    manager = w.manager()
    voided = post(
        manager, f"/sales/{sale[41]}/void", f"/sales/{sale[41]}/void",
        reason="saisie en double", version_no=str(sale_version(w, sale[41])),
    )  # fmt: skip
    assert voided.status_code == 303
    extra = Row(EXTRA_DOC, "199", "12000", "SYNTHX/EXTRA", "XT0001")
    enter_sale(a1, [extra], SIGN_SV, ag=str(day.partner["BADI"]), payment_method="credit")

    assert w.run_comparison(acc).status_code == 303
    live = w.live_flags()
    assert len(w.blocking_open()) == 3 and len(live) == 3
    f_cost = flag_id(w, 8, "different", "total_doc")
    f_sign = flag_id(w, 37, "different", "staff_sign")
    f_gone = flag_id(w, 41, "not_sent")
    assert [f.status for f in live] == ["open"] * 3
    assert dd(acc.get(f"/days/{DAY}").text, "Billets partenaires absents du rapport") == "1"
    # isolation again, now with flags: agent 1 sees his sign flag, never those of agent 2
    seen1 = a1.get(f"/flags?date={DAY}").text
    assert f'action="/flags/{f_sign}/answer"' in seen1 and f"/flags/{f_cost}/answer" not in seen1
    assert ROWS[8].doc not in seen1 and ROWS[41].doc not in seen1
    seen2 = a2.get(f"/flags?date={DAY}").text
    assert ROWS[8].doc in seen2 and ROWS[41].doc in seen2 and ROWS[37].doc not in seen2
    stolen = post(a2, f"/flags?date={DAY}", f"/flags/{f_sign}/answer",
                  choice="mine_right", reason="pas à moi")  # fmt: skip
    assert stolen.status_code == 404

    # the close is refused while anything blocks, and says why
    blocked = post(acc, f"/days/{DAY}", f"/days/{DAY}/close")
    assert blocked.status_code == 409
    why = text_of(blocked.text)
    assert "3 écarts bloquants restent à traiter" in why and "J'ai vu la liste" in why
    assert w.day_status() != "closed" and stored_versions(w) == []

    # (a) the typo: agent 2 says Amadeus is right; money, so it waits for a validator
    why_typo = "Faute de frappe, le rapport est juste"
    answered = post(
        a2, f"/flags?date={DAY}", f"/flags/{f_cost}/answer", choice="amadeus_right", reason=why_typo
    )
    assert answered.status_code == 303
    assert flag_status(w, f_cost) == "answered"
    # the same agent cannot validate (neither the screen nor the action), and nothing moved
    assert a2.get("/flags/validate").status_code == 403
    own = post(a2, f"/flags?date={DAY}", f"/flags/{f_cost}/validate", decision="approve")
    assert own.status_code == 403
    assert flag_status(w, f_cost) == "answered" and sale_version(w, sale[8]) == 2
    queue = acc.get("/flags/validate")
    assert queue.status_code == 200 and f"/flags/{f_cost}/validate" in queue.text
    ok = post(acc, "/flags/validate", f"/flags/{f_cost}/validate", decision="approve")
    assert ok.status_code == 303
    assert flag_status(w, f_cost) in ("resolved", "superseded")  # settled; the rerun retires it
    assert decided(w, f_cost) == ["amadeus_right", "validated"]
    assert [f for f in w.live_flags() if f.doc_number == ROWS[8].doc] == []
    assert sale_version(w, sale[8]) == 3  # corrected to the Amadeus cost
    edit_page = manager.get(f"/sales/{sale[8]}/edit")
    assert fields_of(edit_page.text, f"/sales/{sale[8]}/preview-edit")["total_doc"] == [
        ROWS[8].cost
    ]
    # (b) the sign: agent 1 keeps his value; the accountant approves it
    kept = post(a1, f"/flags?date={DAY}", f"/flags/{f_sign}/answer",
                choice="mine_right", reason="Le billet est vendu sous mon signe")  # fmt: skip
    assert kept.status_code == 303 and flag_status(w, f_sign) == "answered"
    own = post(a1, f"/flags?date={DAY}", f"/flags/{f_sign}/validate", decision="approve")
    assert own.status_code == 403
    ok = post(acc, "/flags/validate", f"/flags/{f_sign}/validate", decision="approve")
    assert ok.status_code == 303 and flag_status(w, f_sign) == "resolved"
    # (c) the ticket nobody entered: agent 2 enters it from the prefilled form
    assert f'href="/flags/{f_gone}/enter"' in a2.get(f"/flags?date={DAY}").text
    entry = a2.get(f"/flags/{f_gone}/enter")
    assert entry.status_code == 200
    form = fields_of(entry.text, "/sales/preview")
    assert form["doc_number"] == [ROWS[41].doc] and form["total_doc"] == [ROWS[41].cost]
    assert form["staff_sign"] == [SIGN_ER] and form["pax_name"] == [ROWS[41].name]
    data = override(form, **direct(cost(41) + 1500, "Client 41", phone(41)))
    day.sale[41] = sale_id_from(preview_then_confirm(a2, data))
    day.pb[41], day.ag[41] = Decimal(cost(41) + 1500), "BRUGE"
    assert w.run_comparison(acc).status_code == 303
    assert w.blocking_open() == []
    assert flag_status(w, f_gone) == "superseded"  # the sale exists now: the flag is retired
    assert [f for f in w.live_flags() if f.doc_number == ROWS[41].doc] == []

    # (d) the partner ticket issued elsewhere: the gate wants the list seen
    w.clock.now = NEXT_MORNING
    acc = w.accountant()
    still = post(acc, f"/days/{DAY}", f"/days/{DAY}/close")
    assert still.status_code == 409 and "J'ai vu la liste" in text_of(still.text)
    assert EXTRA_DOC in still.text  # the list itself is on the page
    assert w.day_status() != "closed" and stored_versions(w) == []
    a1 = w.agent1()
    staff_ack = post(a1, "/sales/new", f"/days/{DAY}/partner-list", digest="x")
    assert staff_ack.status_code == 403
    ack = w.post_form(acc, f"/days/{DAY}", f"/days/{DAY}/partner-list")
    assert ack.status_code == 303
    closed = post(acc, f"/days/{DAY}", f"/days/{DAY}/close")
    assert closed.status_code == 303, text_of(closed.text)[:800]
    assert w.day_status() == "closed" and stored_versions(w) == [1]

    # --- 5. the TJQ APRES ----------------------------------------------------------------------
    got = acc.get(f"/days/{DAY}/apres.xlsx")
    assert got.status_code == 200 and got.headers["content-type"].startswith(XLSX)
    assert got.headers["content-disposition"] == 'attachment; filename="TJQ_APRES_2026-10-20.xlsx"'
    v1 = got.content
    check_export(v1, day, corrected={37: SIGN_SV})
    a2 = w.agent2()  # agent 2 never gets it; the closed day offers him nothing
    assert a2.get(f"/days/{DAY}/apres.xlsx").status_code == 403
    assert "TJQ APRES" not in text_of(a2.get(f"/days/{DAY}").text)

    # --- 7. the manager reopens (with a reason), edits, and the day closes again as version 2 --
    manager = w.manager()
    short = post(manager, f"/days/{DAY}/reopen", f"/days/{DAY}/reopen", reason="")
    assert short.status_code == 400
    refused = post(acc, f"/days/{DAY}", f"/days/{DAY}/reopen", reason="je corrige un prix")
    assert refused.status_code == 403  # only the manager reopens
    reopened = post(manager, f"/days/{DAY}/reopen", f"/days/{DAY}/reopen",
                    reason="Le client du billet 33 a payé 500 de plus")  # fmt: skip
    assert reopened.status_code == 303 and w.day_status() != "closed"
    edit_sale(w, manager, sale[33], "prix corrigé après réouverture", pb=str(cost(33) + 2000))
    day.pb[33] += 500
    again = post(acc, f"/days/{DAY}", f"/days/{DAY}/close")
    assert again.status_code == 303, text_of(again.text)[:800]
    assert w.day_status() == "closed" and stored_versions(w) == [1, 2]
    v2 = acc.get(f"/days/{DAY}/apres.xlsx").content
    old = acc.get(f"/days/{DAY}/apres.xlsx?v=1").content
    assert hashlib.sha256(old).hexdigest() == hashlib.sha256(v1).hexdigest()  # v1 kept as it was
    assert v2 != v1
    check_export(v2, day, corrected={37: SIGN_SV})
    assert acc.get(f"/days/{DAY}/apres.xlsx?v=3").status_code == 404


def test_a_clean_day_closes_and_exports_the_accountants_file(w: Bruge) -> None:
    """The plain day: everything entered right, nothing to answer, the close is one click."""
    day = enter_the_day(w)
    w.clock.now = EVENING
    acc = w.accountant()
    upload_report(w, acc)
    assert w.live_flags() == [] and w.day_status() == "approved"
    closed = post(acc, f"/days/{DAY}", f"/days/{DAY}/close")
    assert closed.status_code == 303, text_of(closed.text)[:800]
    assert stored_versions(w) == [1]
    got = acc.get(f"/days/{DAY}/apres.xlsx")
    assert got.status_code == 200
    ws = check_export(got.content, day)
    for i in range(fx.TKTT_COUNT):  # a plain day marks no line as unmatched or duplicated
        comment = ws.cell(row=FIRST_ROW + i, column=9).comment
        assert comment is None
