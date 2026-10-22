"""app/errors.py (one base for user-facing errors) and app/reasons.py (typed reasons)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app import days
from app.days import DayClosed, DayError
from app.errors import UserError
from app.partners import PartnerError
from app.reasons import MAX_REASON_LENGTH, ReasonError, clean_reason, required_reason
from app.sales import DuplicateTicket, InvalidSale, SaleError, SaleForbidden, SaleNotFound


def test_every_service_error_is_a_user_error() -> None:
    for error in (DayError, DayClosed, PartnerError, SaleError, SaleForbidden, SaleNotFound):
        assert issubclass(error, UserError)
    assert issubclass(InvalidSale, SaleError) and issubclass(DuplicateTicket, SaleError)
    assert not issubclass(ReasonError, UserError)  # converted by the service that asked


def test_a_reason_is_cleaned_and_bounded() -> None:
    assert clean_reason(None) is None
    assert clean_reason("   ") is None
    assert clean_reason("  deux   mots​ ici ") == "deux mots ici"
    assert clean_reason("a" * MAX_REASON_LENGTH) == "a" * MAX_REASON_LENGTH
    for bad in ("abcd", "a" * (MAX_REASON_LENGTH + 1), 12345):
        with pytest.raises(ReasonError):
            clean_reason(bad)  # type: ignore[arg-type]


def test_a_required_reason_must_be_given() -> None:
    assert required_reason(" valable ") == "valable"
    for missing in (None, "", "   "):
        with pytest.raises(ReasonError, match="motif"):
            required_reason(missing)
    with pytest.raises(ReasonError, match="500"):
        required_reason("a" * (MAX_REASON_LENGTH + 1))


def test_check_day_is_the_one_rule_for_dates() -> None:
    today = date(2026, 10, 22)
    days.check_day(today, today)
    days.check_day(today - timedelta(days=5 * 366), today)
    for bad, word in (
        (today + timedelta(days=1), "futur"),
        (today - timedelta(days=5 * 366 + 1), "ancienne"),
        (datetime(2026, 10, 20, 12, 0), "invalide"),
        ("2026-10-20", "invalide"),
    ):
        with pytest.raises(DayError, match=word):
            days.check_day(bad, today)  # type: ignore[arg-type]
