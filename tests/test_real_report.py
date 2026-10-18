"""Acceptance checks against the real Amadeus report, when it is present locally.

The real file holds passenger personal data, so it lives in the git-ignored
``tests/fixtures/real/`` folder and these tests skip when it is absent (e.g. in CI).
They assert counts and totals only, never names.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.parsing.amadeus import parse_report
from app.questions import build_questions

REAL_REPORT = Path(__file__).parent / "fixtures" / "real" / "1OCT.xlsx"

pytestmark = pytest.mark.skipif(
    not REAL_REPORT.exists(), reason="real report not present (kept local, git-ignored)"
)


def test_real_report_matches_acceptance_numbers() -> None:
    report = parse_report(REAL_REPORT)

    assert report.header.report_range_raw == "01OCT"
    assert report.header.report_date == date(2026, 10, 1)
    assert report.header.generated_on == date(2026, 10, 2)
    assert len(report.tickets) == 48
    assert sum(t.cost or Decimal(0) for t in report.tickets) == Decimal("1118857")
    assert sorted(t.pax_type for t in report.tickets if t.pax_type != "ADT") == [
        "CHD",
        "INF",
        "INF",
    ]

    staff = {s.staff_sign: s for s in build_questions(report.tickets)}
    assert (staff["0004SV"].ticket_count, staff["0004SV"].group_count) == (34, 25)
    assert staff["0004SV"].total_cost == Decimal("746423")
    assert (staff["0009ER"].ticket_count, staff["0009ER"].group_count) == (14, 8)
    assert staff["0009ER"].total_cost == Decimal("372434")
    assert sum(s.group_count for s in staff.values()) == 33
