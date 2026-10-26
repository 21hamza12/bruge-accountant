"""Signed issue dates (security review): the signature binds a date to user, agency and ticket."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import SecretStr
from starlette.datastructures import FormData

from app.issue_sig import IssueSigner, doc_key
from app.settings import Settings
from app.web import sale_forms

KEY = b"k" * 32
DAY = date(2026, 10, 20)
DOC = "1993940281465"[-10:]


def posted(*pairs: tuple[str, str]) -> FormData:
    return FormData(list(pairs))


def test_a_signature_verifies_only_for_the_same_agency_user_doc_and_date() -> None:
    signer = IssueSigner(KEY, 1, 7)
    good = signer.sign(DOC, DAY)
    assert signer.verify(DOC, DAY, good)
    assert not signer.verify(DOC, date(2026, 10, 19), good)
    assert not signer.verify("3940281466", DAY, good)
    assert not IssueSigner(KEY, 1, 8).verify(DOC, DAY, good)  # another user
    assert not IssueSigner(KEY, 2, 7).verify(DOC, DAY, good)  # another agency
    assert not IssueSigner(b"x" * 32, 1, 7).verify(DOC, DAY, good)  # another key


@pytest.mark.parametrize("bad", ["", " ", "00", "g" * 64, "é" * 64, "a" * 2000])
def test_garbage_signatures_are_refused_without_raising(bad: str) -> None:
    assert not IssueSigner(KEY, 1, 7).verify(DOC, DAY, bad)


def test_the_ticket_number_is_normalised_before_signing() -> None:
    signer = IssueSigner(KEY, 1, 7)
    assert signer.sign("199-3940281465", DAY) == signer.sign("3940281465", DAY)
    assert signer.sign("199 394/0281465", DAY) == signer.sign("3940281465", DAY)
    assert doc_key("3940281465") == "3940281465"
    assert doc_key("12345") == "12345"  # not a ticket number: kept (it will not validate anyway)


def test_the_signing_key_is_the_configured_secret_else_a_random_one_per_process() -> None:
    fixed = Settings(issue_signing_key=SecretStr("from-the-dashboard"))
    assert fixed.issue_key() == b"from-the-dashboard"
    first, second = Settings(), Settings()
    assert len(first.issue_key()) == 32 and first.issue_key() == first.issue_key()
    assert first.issue_key() != second.issue_key()
    assert Settings(issue_signing_key=SecretStr("  ")).issue_key() != b""


def test_reading_a_staff_form_keeps_only_signed_dates() -> None:
    signer = IssueSigner(KEY, 1, 7)
    rows = (
        ("doc_number", DOC),
        ("doc_number", "3940281466"),
        ("issue_date", "2026-10-20"),
        ("issue_date", "2026-10-20"),
        ("issue_sig", signer.sign(DOC, DAY)),
        ("issue_sig", signer.sign(DOC, DAY)),  # the first row's signature on the second ticket
    )
    form = sale_forms.read(posted(*rows), signer=signer)
    assert [t.issue_date for t in form.tickets] == ["2026-10-20", ""]
    assert [t.issue_sig for t in form.tickets] == [signer.sign(DOC, DAY), ""]
    assert form.issue_day == DAY


def test_reading_without_a_signer_keeps_the_dates_as_posted() -> None:
    form = sale_forms.read(posted(("doc_number", DOC), ("issue_date", "2026-10-20")))
    assert form.tickets[0].issue_date == "2026-10-20"
