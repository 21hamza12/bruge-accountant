"""Field-level validation and flagging in the Amadeus parser."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.parsing.amadeus import ParseError, parse_report
from tests.builders import (
    AMADEUS_16_HEADERS,
    amadeus_line,
    build_amadeus_xlsx,
    build_workbook,
    ticket_row,
)

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


# --- optional columns of the 16-column export ---------------------------------------


def _line(**kw: Any) -> dict[str, Any]:
    return amadeus_line(**kw)


def test_optional_columns_are_read_when_present() -> None:
    data = build_amadeus_xlsx(
        [_line(TAX=1200, COMM="350.5", FP="CA", CONFIRMED="OK", FEE=99, AGENT="X")]
    )
    result = parse_report(data)
    ticket = result.tickets[0]
    assert (ticket.tax, ticket.comm) == (Decimal("1200.00"), Decimal("350.50"))
    assert (ticket.form_of_payment, ticket.confirmed) == ("CA", "OK")
    assert isinstance(ticket.tax, Decimal)
    assert result.warnings == ()  # FEE, AGENT, REPORT IND and NDC are known and ignored


def test_optional_columns_are_none_when_absent_from_an_8_column_file() -> None:
    result = parse_report(build_workbook([ticket_row()]))
    ticket = result.tickets[0]
    assert (ticket.tax, ticket.comm, ticket.form_of_payment, ticket.confirmed) == (
        None,
        None,
        None,
        None,
    )
    assert result.warnings == ()


def test_empty_optional_cells_are_none() -> None:
    result = parse_report(build_amadeus_xlsx([_line(TAX=None, COMM="  ", FP="", CONFIRMED=None)]))
    ticket = result.tickets[0]
    assert (ticket.tax, ticket.comm, ticket.form_of_payment, ticket.confirmed) == (
        None,
        None,
        None,
        None,
    )
    assert result.warnings == ()


def test_optional_money_follows_the_cost_rules() -> None:
    result = parse_report(
        build_amadeus_xlsx(
            [
                _line(doc="1990000001", TAX=0.1, COMM="1 234,5"),
                _line(doc="1990000002", TAX="abc", COMM=10**13),
                _line(doc="1990000003", TAX=True, COMM=Decimal("1.005")),
            ]
        )
    )
    a, b, c = result.tickets
    assert (a.tax, a.comm) == (Decimal("0.10"), Decimal("1234.50"))
    assert (b.tax, b.comm) == (None, None)
    assert (c.tax, c.comm) == (None, Decimal("1.01"))
    assert sum("is not a valid amount" in w for w in result.warnings) == 3
    assert [w for w in result.warnings if "rounded" in w] == ["row 8: COMM rounded to 2 decimals"]
    assert all(t.flags == () for t in result.tickets)  # the cost flags are not touched


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("350,5", "350.50"),
        ("290,99", "290.99"),
        ("1 234,56", "1234.56"),
        ("1 234,5", "1234.50"),
        ("1,234", "1234.00"),
        ("12,345,678", "12345678.00"),
        ("1,234.56", "1234.56"),
        ("350.5", "350.50"),
        ("-3,5", "-3.50"),
        ("0,05", "0.05"),
    ],
)
def test_optional_amounts_read_a_decimal_comma(raw: str, expected: str) -> None:
    result = parse_report(build_amadeus_xlsx([_line(TAX=raw, COMM=raw)]))
    assert result.tickets[0].tax == Decimal(expected)
    assert result.tickets[0].comm == Decimal(expected)
    assert result.warnings == ()


@pytest.mark.parametrize("raw", ["1.234,56", "1,2,3", "12,3456", "1,23,456", "1e5", "12,", ",5"])
def test_ambiguous_optional_amounts_are_ignored_with_a_warning(raw: str) -> None:
    result = parse_report(build_amadeus_xlsx([_line(TAX=raw)]))
    assert result.tickets[0].tax is None
    assert result.warnings == ("row 6: TAX is not a valid amount, ignored",)


def test_total_doc_keeps_its_thousands_comma_rule() -> None:
    result = parse_report(build_amadeus_xlsx([_line(total="290,99")]))
    assert result.tickets[0].cost == Decimal("29099.00")


def test_a_header_that_fills_the_30_read_columns_warns_that_more_may_be_ignored() -> None:
    header = [t for t in AMADEUS_16_HEADERS if t != "FP"]
    header += [f"PAD{n:02d}" for n in range(30 - len(header))]
    header.append("FP")  # column 31: past the last column that is read
    assert len(header) == 31
    result = parse_report(build_amadeus_xlsx([_line(FP="CA")], header=header))
    assert result.tickets[0].form_of_payment is None
    assert (
        "only the first 30 columns were read; columns after the 30th were ignored"
        in result.warnings
    )
    assert sum("only the first" in w for w in result.warnings) == 1


def test_a_30_column_header_that_is_not_full_does_not_warn() -> None:
    result = parse_report(build_amadeus_xlsx([_line()], extra_columns={"X": ["v"]}))
    assert not any("only the first" in w for w in result.warnings)


def test_optional_text_is_cleaned_and_capped() -> None:
    result = parse_report(build_amadeus_xlsx([_line(FP="C‮A\n" + "X" * 40, CONFIRMED="O" * 60)]))
    ticket = result.tickets[0]
    assert ticket.form_of_payment == ("CA X" + "X" * 40)[:16]
    assert ticket.confirmed == "O" * 32
    assert ticket.flags == ("field_too_long",)
    assert sum("truncated" in w for w in result.warnings) == 2


def test_shuffled_column_order_still_works() -> None:
    header = [
        "NDC",
        "FP",
        "TRNC",
        "TOTAL DOC",
        "RLOC",
        "COMM",
        "DOC NUMBER",
        "AS",
        "CONFIRMED",
        "PAX NAME",
        "SEQ NO",
        "A/L",
        "TAX",
    ]
    data = build_amadeus_xlsx(
        [_line(TAX=5, COMM=7, FP="CA", CONFIRMED="OK", doc="1990000009", total=42)],
        header=header,
    )
    ticket = parse_report(data).tickets[0]
    assert ticket.ticket_no == "1990000009"
    assert ticket.cost == Decimal("42.00")
    assert (ticket.tax, ticket.comm, ticket.form_of_payment, ticket.confirmed) == (
        Decimal("5.00"),
        Decimal("7.00"),
        "CA",
        "OK",
    )


def test_unknown_columns_give_exactly_one_warning_and_the_import_succeeds() -> None:
    data = build_amadeus_xlsx(
        [_line(), _line(doc="1990000002")],
        extra_columns={"XYZ": ["a", "b"], "ABC": [1, 2], "xyz": ["c", "d"]},
    )
    result = parse_report(data)
    assert len(result.tickets) == 2
    assert result.warnings == ("unknown columns ignored: XYZ, ABC",)


def test_the_unknown_columns_warning_is_capped_and_cleaned() -> None:
    extra = {f"COL{n:02d}": ["v"] for n in range(12)}
    extra["BAD‮\nNAME" + "Z" * 80] = ["v"]
    result = parse_report(build_amadeus_xlsx([_line()], extra_columns=extra))
    (warning,) = result.warnings
    assert warning.startswith("unknown columns ignored: COL00, COL01")
    assert "COL09" in warning
    assert "COL10" not in warning
    assert warning.endswith("(+3 more)")
    assert "‮" not in warning
    assert len(warning) < 200


def test_the_accountants_hand_typed_columns_are_known_and_not_warned_about() -> None:
    result = parse_report(build_workbook([ticket_row()], hand_typed=True))
    assert result.warnings == ()


def test_a_row_with_only_an_optional_value_is_still_blank() -> None:
    data = build_amadeus_xlsx([_line(), {"COMM": 5}, _line(doc="1990000003")])
    result = parse_report(data)
    assert [t.ticket_no for t in result.tickets] == ["1990000001", "1990000003"]
    assert result.warnings == ()
