"""The PNR reader on what a model's transcription of a screenshot looks like: indentation lost,
Markdown fences, a sentence around the text. Every fixture reads the same as the original."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.parsing.pnr import parse_pnr_text

FIXTURES = Path(__file__).parent / "fixtures" / "pnr"
NAMES = sorted(p.stem for p in FIXTURES.glob("*.txt"))


def raw(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


def deindent(text: str) -> str:
    return "\n".join(line.lstrip(" ") for line in text.splitlines())


def fenced(text: str, tag: str = "") -> str:
    return f"```{tag}\n{text.rstrip()}\n```\n"


def test_fixtures_exist() -> None:
    assert len(NAMES) >= 7


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("variant", ["deindented", "fenced", "both", "tagged", "prose"])
def test_same_reading_after_transcription_noise(name: str, variant: str) -> None:
    original = raw(name)
    noisy = {
        "deindented": deindent(original),
        "fenced": fenced(original),
        "both": fenced(deindent(original)),
        "tagged": fenced(original, "text"),
        "prose": "Voici la transcription de l'écran :\n\n"
        + fenced(deindent(original))
        + "\nLet me know if you need anything else.\n",
    }[variant]
    assert parse_pnr_text(noisy, transcript=True) == parse_pnr_text(original)


def test_two_pages_one_deindented() -> None:
    original = raw("layover_two_pages")
    first, second = original.split("--- TST", 2)[1:]
    mixed = "--- TST" + first + "--- TST" + deindent(second)
    assert parse_pnr_text(mixed, transcript=True) == parse_pnr_text(original)
    assert len(parse_pnr_text(mixed, transcript=True).tickets) == 4


def test_trailing_prose_is_not_joined_to_the_last_element() -> None:
    text = raw("three_adults_rm").rstrip() + "\nThis is the whole screen.\n"
    assert parse_pnr_text(text, transcript=True) == parse_pnr_text(raw("three_adults_rm"))


@pytest.mark.parametrize("trailer", ["NO MORE ITEMS", "END OF DISPLAY", "TOTAL 3", "see above"])
def test_a_typed_paste_with_a_column_0_trailer_reads_the_same(trailer: str) -> None:
    """Typed pastes keep the strict rule: only an indented line continues an element, so a line at
    column 0 after the last element is not glued to it (the loose rule is for transcripts)."""
    original = raw("three_adults_rm")
    text = original.rstrip() + f"\n{trailer}\n"
    assert parse_pnr_text(text) == parse_pnr_text(original)
    fenced_text = original.rstrip() + f"\n```\n{trailer}\n"
    assert parse_pnr_text(fenced_text) == parse_pnr_text(original)


def test_the_loose_rule_is_only_for_transcripts() -> None:
    head = "RP/NKC262213/NKC262213 SV/SU 21OCT26/0000Z ZX6AGG"
    lost = f"{head}\n1.SYNTHA/ONE\n7 RM 5400\nMRU\n"
    assert parse_pnr_text(lost, transcript=True).rm_raw == "5400MRU"
    assert parse_pnr_text(lost).rm_raw == "5400"


@pytest.mark.parametrize("transcript", [False, True])
def test_an_indented_lowercase_continuation_is_kept(transcript: bool) -> None:
    # Amadeus wraps a long element with an indent of 3 or more; an agent's remark or e-mail in
    # lowercase is still part of it (only a de-indented line of prose is dropped).
    original = raw("three_adults_rm")
    wrapped = original.replace(
        "  7 RM 5400 MRU\n", "  7 RM 5400 MRU paye par\n       client.nom@gmail.com\n       cash\n"
    )
    assert wrapped != original
    reading = parse_pnr_text(wrapped, transcript=transcript)
    assert "client.nom@gmail.com" in reading.rm_raw and "cash" in reading.rm_raw
    assert reading.tickets == parse_pnr_text(original).tickets
