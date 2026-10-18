"""Field-level validation and flagging in the Amadeus parser."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.parsing.amadeus import ParseError, parse_report
from tests.builders import build_workbook, ticket_row

# --- header dates given as real Excel cells -----------------------------------------


def test_current_date_as_excel_datetime_cell() -> None:
    result = parse_report(build_workbook([ticket_row()], current=datetime(2026, 10, 2, 8, 30)))
    assert result.header.generated_on == date(2026, 10, 2)
    assert result.header.report_date == date(2026, 10, 1)


def test_current_date_as_excel_date_cell_rolls_year() -> None:
    result = parse_report(
        build_workbook([ticket_row()], range_raw="31DEC", current=date(2027, 1, 1))
    )
    assert result.header.report_date == date(2026, 12, 31)


def test_current_date_impossible_text_date() -> None:
    with pytest.raises(ParseError, match="Current date"):
        parse_report(build_workbook([ticket_row()], current="31-FEB-2026"))


# --- blank / odd fields -------------------------------------------------------------


def test_blank_rloc_and_staff_are_flagged() -> None:
    result = parse_report(
        build_workbook(
            [
                ticket_row(doc="3940000001", rloc=None),
                ticket_row(doc="3940000002", staff=None),
                ticket_row(doc="3940000003", rloc="  ", staff=""),
            ]
        )
    )
    a, b, c = result.tickets
    assert a.flags == ("missing_booking_ref",)
    assert b.flags == ("missing_staff_sign",)
    assert c.flags == ("missing_booking_ref", "missing_staff_sign")
    assert (a.booking_ref, b.staff_sign) == ("", "")


@pytest.mark.parametrize("raw", ["DOE/JOHN (INFANT)", "DOE/JOHN (CHD", "DOE (INF) JR", "X (chd)Y"])
def test_unparsed_pax_marker_warns(raw: str) -> None:
    result = parse_report(build_workbook([ticket_row(name=raw)]))
    assert result.tickets[0].pax_type == "ADT"
    assert len(result.warnings) == 1
    assert "pax type" in result.warnings[0]


def test_normal_name_has_no_pax_warning() -> None:
    assert parse_report(build_workbook([ticket_row(name="CHDIRI/INF")])).warnings == ()


@pytest.mark.parametrize("doc", ["12345", "1234567", "abcdefghij", "3940-279110", "1" * 16])
def test_bad_ticket_number_row_skipped_with_warning(doc: str) -> None:
    result = parse_report(build_workbook([ticket_row(doc=doc), ticket_row(doc="3940000002")]))
    assert [t.ticket_no for t in result.tickets] == ["3940000002"]
    assert len(result.warnings) == 1
    assert "row 6" in result.warnings[0]


@pytest.mark.parametrize("doc", ["12345678", "1" * 15, "0394000000"])
def test_ticket_number_boundaries_accepted(doc: str) -> None:
    assert parse_report(build_workbook([ticket_row(doc=doc)])).tickets[0].ticket_no == doc


@pytest.mark.parametrize(
    ("field", "kwarg", "limit"),
    [
        ("airline", "airline", 8),
        ("staff_sign", "staff", 32),
        ("booking_ref", "rloc", 16),
        ("transaction_type", "trnc", 16),
        ("seq_no", "seq", 32),
    ],
)
def test_overlong_field_is_truncated_and_flagged(field: str, kwarg: str, limit: int) -> None:
    result = parse_report(build_workbook([ticket_row(**{kwarg: "X" * (limit + 5)})]))
    t = result.tickets[0]
    assert getattr(t, field) == "X" * limit
    assert "field_too_long" in t.flags
    assert len(result.warnings) == 1


def test_field_exactly_at_limit_not_flagged() -> None:
    t = parse_report(build_workbook([ticket_row(rloc="R" * 16)])).tickets[0]
    assert "field_too_long" not in t.flags


# --- cost ---------------------------------------------------------------------------


def test_three_decimal_cost_is_rounded_half_up_and_flagged() -> None:
    t = parse_report(build_workbook([ticket_row(cost="100.005")])).tickets[0]
    assert t.cost == Decimal("100.01")
    assert str(t.cost) == "100.01"
    assert t.flags == ("cost_rounded",)


def test_two_decimal_cost_not_flagged() -> None:
    t = parse_report(build_workbook([ticket_row(cost="100.50")])).tickets[0]
    assert t.cost == Decimal("100.50")
    assert t.flags == ()


@pytest.mark.parametrize(
    "cost", ["1e12", 1e12, "999999999999999", "1e999999999", "-1e12", "1E+400"]
)
def test_huge_cost_is_missing(cost: object) -> None:
    t = parse_report(build_workbook([ticket_row(cost=cost)])).tickets[0]
    assert t.cost is None
    assert "missing_cost" in t.flags


def test_largest_accepted_cost() -> None:
    t = parse_report(build_workbook([ticket_row(cost="999999999999.99")])).tickets[0]
    assert t.cost == Decimal("999999999999.99")


def test_tiny_cost_rounds_to_zero_and_flags() -> None:
    t = parse_report(build_workbook([ticket_row(cost="1e-999999")])).tickets[0]
    assert t.cost == Decimal("0.00")
    assert "cost_rounded" in t.flags


# --- warnings are bounded -----------------------------------------------------------


def test_warnings_capped_at_100_plus_summary() -> None:
    rows = [ticket_row(doc=f"{i}") for i in range(150)]
    result = parse_report(build_workbook(rows))
    assert result.tickets == ()
    assert len(result.warnings) == 101
    assert result.warnings[-1] == "... and 50 more warnings"


def test_exactly_100_warnings_has_no_summary() -> None:
    rows = [ticket_row(doc=f"{i}") for i in range(100)]
    assert len(parse_report(build_workbook(rows)).warnings) == 100


def test_cell_values_in_warnings_are_truncated() -> None:
    result = parse_report(build_workbook([ticket_row(doc="9" * 500)]))
    assert len(result.warnings) == 1
    assert len(result.warnings[0]) < 150
    assert "9" * 41 not in result.warnings[0]


# --- control characters and length in text cells ------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("DOE/JO\u202eHN", "DOE/JOHN"),  # bidi override (Cf)
        ("DOE/\u200bJOHN\u200f", "DOE/JOHN"),  # zero-width space, right-to-left mark
        ("DOE/JO\x7fHN\x9b", "DOE/JOHN"),  # DEL and a C1 control (Cc)
        ("DOE/JOHN\nMR", "DOE/JOHN MR"),  # a line break becomes a space
        ("DOE/JOHN\u2028MR", "DOE/JOHN MR"),  # line separator (Zl)
        ("\ufeffDOE/JOHN", "DOE/JOHN"),  # byte order mark
    ],
)
def test_passenger_names_lose_control_and_format_characters(raw: str, expected: str) -> None:
    result = parse_report(build_workbook([ticket_row(name=raw)]))
    assert result.tickets[0].passenger_name == expected


def test_a_very_long_passenger_name_is_capped_at_200_and_flagged() -> None:
    result = parse_report(build_workbook([ticket_row(name="A" * 5000)]))
    (ticket,) = result.tickets
    assert ticket.passenger_name == "A" * 200
    assert "field_too_long" in ticket.flags
    assert any("PAX NAME longer than 200" in w for w in result.warnings)


def test_other_text_fields_are_cleaned_too() -> None:
    result = parse_report(build_workbook([ticket_row(staff="00\u202e04SV", rloc="AB\x7fCDE")]))
    (ticket,) = result.tickets
    assert (ticket.staff_sign, ticket.booking_ref) == ("0004SV", "ABCDE")


def test_ordinary_non_ascii_names_survive() -> None:
    result = parse_report(build_workbook([ticket_row(name="BEN ALI/AMÉLIE ÉLÈVE")]))
    assert result.tickets[0].passenger_name == "BEN ALI/AMÉLIE ÉLÈVE"
