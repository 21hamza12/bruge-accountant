"""B11, the local check: the app's TJQ APRES against the accountant's real one.

Runs only where the two real files exist (``intake/amadeus-reports/TJQ AVANT.xlsx`` and
``intake/excel-sheets/TJQ APRES.xlsx``, kept local and git-ignored); everywhere else it is skipped.
The files hold passenger names, so **nothing printed or asserted here carries a value from them**:
only counts. The sales are entered through the web forms with P.B = cost (the real prices are not
needed for the check).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from app.parsing.amadeus import parse_report
from tests.acceptance_support import (
    Bruge,
    Row,
    data_rows,
    dec,
    direct,
    enter_sale,
    open_sheet,
    text_of,
)
from tests.web_support import WebDb

AVANT_NAME = ("amadeus-reports", "TJQ AVANT.xlsx")
APRES_NAME = ("excel-sheets", "TJQ APRES.xlsx")
COLUMNS = ("SEQ NO", "A/L", "DOC NUMBER", "TOTAL DOC", "PAX NAME", "AS", "TRNC", "RLOC")


def _intake_file(parts: tuple[str, str]) -> Path | None:
    """``intake/<folder>/<file>`` of the repository this checkout belongs to (also found from a
    git worktree, which lives below the main checkout)."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "intake" / parts[0] / parts[1]
        if candidate.is_file():
            return candidate
    return None


AVANT = _intake_file(AVANT_NAME)
APRES = _intake_file(APRES_NAME)

pytestmark = pytest.mark.skipif(
    AVANT is None or APRES is None,
    reason="the real TJQ AVANT / TJQ APRES are not present (kept local, git-ignored)",
)


def _same(a: object, b: object, *, spaces: bool = True) -> bool:
    """Equal as the accountant would read them: numbers by value, text trimmed (and, with
    ``spaces`` false, without any white space at all)."""
    if isinstance(a, int | float | Decimal) or isinstance(b, int | float | Decimal):
        try:
            return Decimal(str(a)) == Decimal(str(b))
        except InvalidOperation:
            return False
    left, right = (str(x if x is not None else "").strip() for x in (a, b))
    if not spaces:
        left, right = ("".join(x.split()) for x in (left, right))
    return left == right


def _accountants_lines(path: Path) -> list[list[Any]]:
    """The data lines of the accountant's file: the 8 Amadeus columns, from the row under the
    headers up to the first line without a SEQ NO."""
    ws = open_sheet(path.read_bytes())
    head = next(
        r
        for r in range(1, 30)
        if str(ws.cell(row=r, column=1).value or "").strip().upper() == "SEQ NO"
    )
    where = {}
    for col in range(1, 30):
        title = str(ws.cell(row=head, column=col).value or "").strip().upper()
        if title in COLUMNS and title not in where:
            where[title] = col
    assert set(where) == set(COLUMNS), "the accountant's file lacks some of the 8 columns"
    lines: list[list[Any]] = []
    row = head + 1
    while str(ws.cell(row=row, column=where["SEQ NO"]).value or "").strip():
        lines.append([ws.cell(row=row, column=where[c]).value for c in COLUMNS])
        row += 1
    return lines


def test_the_real_20_october_exports_like_the_accountants_file(
    web_db: WebDb, capsys: pytest.CaptureFixture[str]
) -> None:
    assert AVANT is not None and APRES is not None
    report = parse_report(AVANT)
    tickets = [t for t in report.tickets if t.transaction_type != "CANX"]
    cancelled = len(report.tickets) - len(tickets)
    day = report.header.report_date
    assert day is not None
    iso = day.isoformat()

    w = Bruge(web_db)
    w.clock.now = datetime(day.year, day.month, day.day, 9, tzinfo=UTC) + timedelta(days=1)
    manager = w.manager()
    for t in tickets:
        cost = t.cost if t.cost is not None else Decimal(0)
        row = Row(t.ticket_no, t.airline, format(cost, "f"), t.passenger_name, t.booking_ref,
                  t.pax_type, t.transaction_type)  # fmt: skip
        try:
            enter_sale(manager, [row], t.staff_sign, day=iso, **direct(format(cost, "f"), "Client"))
        except AssertionError:
            raise AssertionError("a sale of the real report was refused by the form") from None

    accountant = w.accountant()
    assert w.upload(accountant, AVANT.read_bytes(), "TJQ AVANT.xlsx").status_code == 303
    blocking = len(w.blocking_open())
    assert blocking == 0, f"{blocking} blocking flag(s) after entering the lines of the report"

    closed = accountant.post(
        f"/days/{iso}/close",
        data={"csrf_token": _token(accountant, f"/days/{iso}")},
        follow_redirects=False,
    )
    assert closed.status_code == 303, f"close refused: {len(text_of(closed.text))} chars of reasons"
    got = accountant.get(f"/days/{iso}/apres.xlsx")
    assert got.status_code == 200

    ours = data_rows(open_sheet(got.content), len(tickets))
    theirs = _accountants_lines(APRES)
    assert len(ours) == len(theirs), f"{len(ours)} lines exported, {len(theirs)} in the real file"
    # The report pads "(CHD)" / "(INF)" away from the name; the app keeps the name with single
    # spaces (the parser cleans white space). That padding is the only difference tolerated, and
    # only on the lines of a child or an infant.
    wrong = {title: 0 for title in COLUMNS}
    padding = 0
    for mine, other in zip(ours, theirs, strict=True):
        for index, title in enumerate(COLUMNS):
            if not _same(mine[index], other[index], spaces=title != "PAX NAME"):
                wrong[title] += 1
            elif title == "PAX NAME" and not _same(mine[index], other[index]):
                padding += 1
    order_ok = [str(r[0]) for r in ours] == [str(r[0]) for r in theirs]
    total_ours = sum(dec(r[3]) for r in ours)
    total_theirs = sum(dec(r[3]) for r in theirs)
    with capsys.disabled():
        print(
            f"\nreal 20 Oct: {len(report.tickets)} report lines, {len(tickets)} exported, "
            f"{cancelled} cancelled, {sum(wrong.values())} differing cells, {padding} names differ "
            f"only by padding, order equal: {order_ok}"
        )
    assert order_ok, "the SEQ NO order differs from the accountant's file"
    assert wrong == {title: 0 for title in COLUMNS}, f"differing cells per column: {wrong}"
    assert padding <= sum(1 for t in tickets if t.pax_type != "ADT")
    assert total_ours == total_theirs, "the TOTAL DOC sums differ"


def _token(client: Any, path: str) -> str:
    from tests.web_support import csrf_for

    return csrf_for(client, path)
