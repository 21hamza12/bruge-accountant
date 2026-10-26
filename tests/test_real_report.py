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

    per_sign: dict[str, list[Decimal]] = {}
    for t in report.tickets:
        per_sign.setdefault(t.staff_sign, []).append(t.cost or Decimal(0))
    assert {sign: (len(costs), sum(costs)) for sign, costs in per_sign.items()} == {
        "0004SV": (34, Decimal("746423")),
        "0009ER": (14, Decimal("372434")),
    }
