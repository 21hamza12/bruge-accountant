"""The parent's strict reader of a child's message (``app.parsing.wire``).

The child is untrusted: everything it says is checked for type, range, length and characters, and
the message is never unpickled.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import pytest

from app.parsing import wire
from app.parsing.amadeus import FIELD_LIMITS, ParsedReport, ParseError
from app.parsing.sandbox import decode_message, encode_report


def message(parsed: ParsedReport) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(encode_report(parsed))
    return out


def decode(payload: dict[str, Any]) -> ParsedReport:
    return decode_message(json.dumps(payload).encode())


def with_ticket(parsed: ParsedReport, **changes: Any) -> dict[str, Any]:
    payload = message(parsed)
    payload["report"]["tickets"][0].update(changes)
    return payload


# --- a pickle is never executed ----------------------------------------------------------------


class _Evil:
    """Unpickling this would run code: write a file and raise."""

    def __init__(self, sentinel: Path) -> None:
        self.sentinel = sentinel

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        return (_write_sentinel, (str(self.sentinel),))


def _write_sentinel(path: str) -> None:  # pragma: no cover - must never run
    Path(path).write_text("pwned", encoding="utf-8")
    raise RuntimeError("SENTINEL-RAISED")


@pytest.mark.parametrize("protocol", [0, 2, 4, 5])
def test_a_malicious_pickle_is_a_parse_error_and_runs_nothing(
    tmp_path: Path, protocol: int
) -> None:
    sentinel = tmp_path / "pwned.txt"
    payload = pickle.dumps(_Evil(sentinel), protocol=protocol)
    with pytest.raises(ParseError):
        decode_message(payload)
    assert not sentinel.exists()


# --- numbers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cost",
    [
        "NaN",
        "-NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "inf",
        "1e5",
        "1E+2",
        " 12",
        "12 ",
        "1_000",
        "1,000.00",
        "12.345",
        "1000000000000",  # 10^12: out of range
        "-1000000000000.00",
        "99999999999999",
        "",
        "-",
        ".5",
        5,
        12.5,
        True,
        [],
    ],
)
def test_a_bad_cost_is_refused(parsed_fixture: ParsedReport, cost: Any) -> None:
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, cost=cost))


@pytest.mark.parametrize("cost", ["0", "0.00", "12.5", "26.00", "-5.50", "999999999999.99", None])
def test_a_plain_cost_inside_the_range_is_accepted(parsed_fixture: ParsedReport, cost: Any) -> None:
    ticket = decode(with_ticket(parsed_fixture, cost=cost)).tickets[0]
    assert (ticket.cost is None) == (cost is None)


def test_json_nan_and_infinity_constants_are_refused() -> None:
    for constant in (b"NaN", b"Infinity", b"-Infinity"):
        with pytest.raises(ParseError):
            decode_message(b'{"status": "error", "message": ' + constant + b"}")


@pytest.mark.parametrize("row", [True, False, 0, -1, 10_000_001, 1.0, "5", None, 10**30])
def test_a_bad_source_row_is_refused(parsed_fixture: ParsedReport, row: Any) -> None:
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, source_row=row))


@pytest.mark.parametrize("row", [1, 2, 10_000_000])
def test_a_source_row_in_range_is_accepted(parsed_fixture: ParsedReport, row: int) -> None:
    assert decode(with_ticket(parsed_fixture, source_row=row)).tickets[0].source_row == row


# --- strings ------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(FIELD_LIMITS))
def test_each_field_obeys_its_column_length(parsed_fixture: ParsedReport, field: str) -> None:
    limit = FIELD_LIMITS[field]
    assert decode(with_ticket(parsed_fixture, **{field: "A" * limit})).tickets
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, **{field: "A" * (limit + 1)}))


def test_the_passenger_name_and_ticket_number_lengths(parsed_fixture: ParsedReport) -> None:
    assert decode(with_ticket(parsed_fixture, passenger_name="N" * 200))
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, passenger_name="N" * 201))
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, ticket_no="1" * 33))
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, ticket_no=""))


@pytest.mark.parametrize(
    "bad",
    ["a\x00b", "a\nb", "a\tb", "a\x7fb", "a‮b", "a​b", "a b", "a\ud800b", "ab"],
)
def test_control_format_and_surrogate_characters_are_refused_everywhere(
    parsed_fixture: ParsedReport, bad: str
) -> None:
    for field in ("passenger_name", "staff_sign", "booking_ref", "airline", "seq_no"):
        with pytest.raises(ParseError):
            decode_message(json.dumps(with_ticket(parsed_fixture, **{field: bad})).encode())
    payload = message(parsed_fixture)
    payload["report"]["warnings"] = [bad]
    with pytest.raises(ParseError):
        decode_message(json.dumps(payload).encode())
    payload = message(parsed_fixture)
    payload["report"]["header"]["report_range_raw"] = bad
    with pytest.raises(ParseError):
        decode_message(json.dumps(payload).encode())


def test_ordinary_unicode_text_is_accepted(parsed_fixture: ParsedReport) -> None:
    ticket = decode(with_ticket(parsed_fixture, passenger_name="BEN ALI/AMÉLIE محمد"))
    assert "AMÉLIE" in ticket.tickets[0].passenger_name


def test_an_error_text_is_cut_and_sanitized_not_refused() -> None:
    with pytest.raises(ParseError) as caught:
        decode({"status": "error", "message": "bad‮ file\n" + "x" * 1000})
    text = str(caught.value)
    assert len(text) <= wire.MAX_ERROR_CHARS
    assert "‮" not in text
    assert text.startswith("bad file")


@pytest.mark.parametrize("flags", [["nope"], ["missing_cost"] * 17, [5], "missing_cost", [None]])
def test_unknown_or_malformed_flags_are_refused(parsed_fixture: ParsedReport, flags: Any) -> None:
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, flags=flags))


def test_a_ticket_with_extra_or_missing_keys_is_refused(parsed_fixture: ParsedReport) -> None:
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, extra="x"))
    payload = message(parsed_fixture)
    del payload["report"]["tickets"][0]["airline"]
    with pytest.raises(ParseError):
        decode(payload)


# --- counts and size ----------------------------------------------------------------------------


def test_ticket_and_warning_counts_are_bounded(
    parsed_fixture: ParsedReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    one = message(parsed_fixture)["report"]["tickets"][0]
    payload = message(parsed_fixture)
    payload["report"]["tickets"] = [one] * wire.MAX_TICKETS
    assert len(decode(payload).tickets) == wire.MAX_TICKETS
    payload["report"]["tickets"] = [one] * (wire.MAX_TICKETS + 1)
    with pytest.raises(ParseError):
        decode(payload)
    payload = message(parsed_fixture)
    payload["report"]["warnings"] = ["w"] * wire.MAX_WARNING_COUNT
    assert len(decode(payload).warnings) == wire.MAX_WARNING_COUNT
    payload["report"]["warnings"] = ["w"] * (wire.MAX_WARNING_COUNT + 1)
    with pytest.raises(ParseError):
        decode(payload)
    payload["report"]["warnings"] = ["w" * (wire.MAX_WARNING_CHARS + 1)]
    with pytest.raises(ParseError):
        decode(payload)


def test_the_documented_limits_are_the_ones_in_force() -> None:
    assert wire.MAX_TICKETS == 50_000
    assert wire.MAX_WARNING_COUNT == 1_000
    assert wire.MAX_SOURCE_ROW == 10_000_000


def test_an_oversized_message_is_refused_before_it_is_parsed(
    parsed_fixture: ParsedReport, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = encode_report(parsed_fixture)
    assert decode_message(payload) == parsed_fixture
    monkeypatch.setattr(wire, "MAX_MESSAGE_BYTES", len(payload) - 1)
    with pytest.raises(ParseError):
        decode_message(payload)
    monkeypatch.setattr(wire, "MAX_MESSAGE_BYTES", len(payload))
    assert decode_message(payload) == parsed_fixture


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"not json",
        b"[]",
        b"null",
        b'{"header": {}}',
        b'{"status": "ok"}',
        b'{"status": "ok", "report": []}',
        b'{"status": "weird"}',
        b'{"status": "error"}',
        b'{"status": "error", "message": 5}',
        b"\xff\xfe",
        pytest.param(b"[" * 100_000, id="deeply-nested"),
        b'{"status": "ok", "report": {"header": {"report_range_raw": "01OCT", '
        b'"report_date": "nope", "generated_on": "2026-10-02"}, "tickets": [], "warnings": []}}',
    ],
)
def test_a_malformed_child_message_is_a_parse_error_not_a_crash(payload: bytes) -> None:
    with pytest.raises(ParseError):
        decode_message(payload)


def test_a_ticket_with_an_unknown_passenger_type_is_refused(parsed_fixture: ParsedReport) -> None:
    with pytest.raises(ParseError):
        decode(with_ticket(parsed_fixture, pax_type="VIP"))
