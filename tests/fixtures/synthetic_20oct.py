"""A made-up Amadeus report for 20 October 2026, shaped like a real day (nothing here is real).

43 lines: 34 TKTT summing to 624 126 MRU and 9 CANX summing to 177 372 MRU, staff signs 0004SV,
0006AC and 0009ER, airlines 199, 147, 495, 235, 169 and 124. Special lines other tests rely on:

* a cancel-and-reissue pair: one CANX and one TKTT for the same passenger, the same booking
  reference and the same amount (``PAIR_*``);
* two airline-495 TKTT lines at cost 0 that share a booking reference (``ZERO_COST_RLOC``);
* some non-zero COMM values and form of payment ``CA`` on every line.

Passenger names are placeholders (``SYNTHnn/PAX``), never real people. The comparison (B8) and
the end-to-end acceptance (B11) reuse this file.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tests.builders import amadeus_line, build_amadeus_xlsx

REPORT_RANGE = "20OCT"
CURRENT_DATE = "21-OCT-2026"
TKTT_TOTAL = 624_126
CANX_TOTAL = 177_372
TKTT_COUNT = 34
CANX_COUNT = 9

PAIR_PAX = "SYNTH01/PAX"
PAIR_RLOC = "PRQ7ZA"
PAIR_AMOUNT = 18_450
PAIR_AIRLINE = "199"
ZERO_COST_RLOC = "ZR0K4B"
ZERO_COST_AIRLINE = "495"

_SIGNS = ("0004SV", "0006AC", "0009ER")

# (airline, amount); the first is the TKTT half of the pair, the 495 zeros share ZERO_COST_RLOC.
_TKTT: tuple[tuple[str, int], ...] = (
    (PAIR_AIRLINE, PAIR_AMOUNT),
    ("147", 22_300),
    ("235", 15_600),
    ("169", 41_200),
    ("124", 9_800),
    ("199", 27_650),
    ("147", 33_100),
    ("495", 12_900),
    ("235", 16_400),
    ("169", 17_250),
    ("124", 8_300),
    ("199", 29_900),
    ("147", 14_700),
    ("235", 38_600),
    ("169", 21_100),
    ("495", 10_450),
    ("124", 17_500),
    ("199", 19_300),
    ("147", 25_800),
    ("235", 13_200),
    ("169", 31_750),
    ("124", 16_900),
    ("199", 7_600),
    ("147", 22_050),
    ("235", 35_400),
    ("169", 11_300),
    ("124", 28_600),
    ("199", 9_150),
    ("147", 13_800),
    ("235", 20_500),
    (ZERO_COST_AIRLINE, 0),
    (ZERO_COST_AIRLINE, 0),
    ("169", 7_576),
    ("124", 6_000),
)
_CANX: tuple[tuple[str, int], ...] = (
    (PAIR_AIRLINE, PAIR_AMOUNT),
    ("147", 23_400),
    ("235", 16_800),
    ("169", 31_200),
    ("124", 9_500),
    ("199", 27_100),
    ("147", 14_350),
    ("235", 22_900),
    ("169", 13_672),
)


# Positions in _TKTT / _CANX of the special lines (explicit, not guessed from the amounts).
_PAIR_INDEX = 0  # first line of both lists (the pair has the same airline and amount)
_ZERO_COST_INDEXES = frozenset({30, 31})  # the two airline-495 lines at cost 0


def _build_lines() -> list[dict[str, Any]]:
    """The TKTT lines with the CANX lines interleaved, in a fixed made-up order."""
    # (trnc, airline, amount, special) with special in "", "pair", "zero".
    entries: list[tuple[str, str, int, str]] = []
    for i, (airline, amount) in enumerate(_TKTT):
        special = "pair" if i == _PAIR_INDEX else "zero" if i in _ZERO_COST_INDEXES else ""
        entries.append(("TKTT", airline, amount, special))
    cancelled = iter(
        ("CANX", airline, amount, "pair" if i == _PAIR_INDEX else "")
        for i, (airline, amount) in enumerate(_CANX)
    )
    merged: list[tuple[str, str, int, str]] = []
    for index, entry in enumerate(entries):
        merged.append(entry)
        if index % 4 == 3:
            nxt = next(cancelled, None)
            if nxt is not None:
                merged.append(nxt)
    merged.extend(cancelled)

    lines: list[dict[str, Any]] = []
    for n, (trnc, airline, amount, special) in enumerate(merged, start=1):
        pax = f"SYNTH{n:02d}/PAX"
        rloc = f"SY{n:04d}"
        if special == "pair":
            pax, rloc = PAIR_PAX, PAIR_RLOC
        elif special == "zero":
            rloc = ZERO_COST_RLOC
        if n == 7:
            pax = "SYNTH07/PAX (CHD)"
        lines.append(
            amadeus_line(
                seq=f"{n:06d}",
                airline=airline,
                doc=f"{airline}{2_600_000 + n:07d}",
                total=amount,
                name=pax,
                staff=_SIGNS[n % 3],
                trnc=trnc,
                rloc=rloc,
                CONFIRMED="OK",
                TAX=0 if amount == 0 else 1_200,
                COMM=amount // 20 if n % 5 == 0 else 0,
                FP="CA",
            )
        )
    return lines


LINES: Sequence[dict[str, Any]] = tuple(_build_lines())


def build_synthetic_20oct() -> bytes:
    """The 43 lines as an .xlsx in the 16-column layout."""
    return build_amadeus_xlsx(LINES, range_raw=REPORT_RANGE, current=CURRENT_DATE)
