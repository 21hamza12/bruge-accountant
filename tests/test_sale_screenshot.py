"""Phase D web flow: POST /sales/screenshot with a FAKE client (no test ever calls the real API).

The fake answers with the synthetic PNR texts of tests/fixtures/pnr. Images are made in memory.
"""

from __future__ import annotations

import io
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai import pricing
from app.ai.client import AiUnavailable, Transcription
from app.ai.fake import FakeTranscribeClient
from app.ai.images import MAX_IMAGE_BYTES
from app.auth import ratelimit
from app.models import AgencyAiSettings, AiUsage, AuditEvent, Sale, SaleVersion
from tests.acceptance_support import fields_of, override, preview_then_confirm, sale_id_from
from tests.ai_support import GPS_MARK, jpeg_with_exif, layover_pages, png, pnr
from tests.page_support import Env, fr
from tests.test_sale_pages import SIGN, counts, table, text_of
from tests.test_sale_paste import one_passenger, values
from tests.web_support import WebDb, csrf_for, owner_session

BUDGET_MESSAGE = "Budget IA du mois atteint : collez le texte Amadeus."
DOWN_MESSAGE = "La lecture de l'image n'a pas abouti : collez le texte Amadeus."
HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 40
TRUNCATED_JPEG = b"\xff\xd8\xff" + b"\x00" * 10
HAIKU_COST = pricing.cost_micro_usd(HAIKU, 650, 400)
SONNET_COST = pricing.cost_micro_usd(SONNET, 650, 400)


@pytest.fixture
def env(web_db: WebDb) -> Env:
    e = Env(web_db)
    e.link(e.seeded.staff_a, SIGN)
    return e


def garbled() -> str:
    """A one-passenger read with an unreadable character in a ticketing line: a fallback reason."""
    return one_passenger().replace("FB PAX 0000000000", "FB PAX 00000?0000")


def with_ai(env: Env, replies: Any = ()) -> FakeTranscribeClient:
    fake = FakeTranscribeClient(list(replies) if not callable(replies) else replies)
    env.factory.app.state.ai_client = fake  # type: ignore[attr-defined]
    return fake


def shoot(
    c: TestClient,
    *blobs: bytes,
    names: tuple[str, ...] = (),
    token: str | None = None,
) -> Any:
    files = [
        ("images", (names[i] if i < len(names) else f"page{i}.png", blob, "image/png"))
        for i, blob in enumerate(blobs)
    ]
    data = {"csrf_token": token or csrf_for(c, "/sales/new")}
    return c.post("/sales/screenshot", data=data, files=files or None, follow_redirects=False)


def usage(env: Env) -> list[AiUsage]:
    return sorted(table(env, AiUsage), key=lambda u: u.id)


def set_budget(env: Env, micro: int, *, enabled: bool = True) -> None:
    with owner_session(env.db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, env.seeded.agency_a)
        s.add(
            AgencyAiSettings(
                agency_id=env.seeded.agency_a, enabled=enabled, monthly_budget_micro_usd=micro
            )
        )
        s.commit()


# --- the button --------------------------------------------------------------------------------


def test_no_client_no_button_and_the_route_is_hidden(env: Env) -> None:
    c = env.staff_a()
    html = c.get("/sales/new").text
    assert "/sales/screenshot" not in html and 'type="file"' not in html
    assert shoot(c, png()).status_code == 404


def test_the_button_is_there_with_a_client(env: Env) -> None:
    with_ai(env)
    html = env.staff_a().get("/sales/new").text
    assert 'action="/sales/screenshot"' in html and 'enctype="multipart/form-data"' in html
    assert html.count('accept="image/*"') == 2
    assert 'capture="environment"' in html and " multiple" in html
    assert "jamais enregistrée" in text_of(html)
    assert not re.search(r"<style|\sstyle=|<script(?![^>]*\ssrc=)|\son[a-z]+=", html)


def test_an_agency_switched_off_has_no_button_and_no_route(env: Env) -> None:
    with_ai(env, [one_passenger()])
    set_budget(env, 5_000_000, enabled=False)
    c = env.staff_a()
    assert "/sales/screenshot" not in c.get("/sales/new").text
    assert shoot(c, png()).status_code == 404


def test_the_button_is_not_on_the_edit_form(env: Env) -> None:
    from tests.test_sale_pages import D1, enter, tk

    with_ai(env, [])
    c = env.staff_a()
    sale_id = enter(c, tk(D1))
    page = c.get(f"/sales/{sale_id}/edit")
    assert page.status_code == 200 and "/sales/screenshot" not in page.text


# --- the three outcomes --------------------------------------------------------------------------


def test_one_passenger_gives_the_prefilled_form_labelled_screenshot(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    c = env.staff_a()
    before = counts(env)
    r = shoot(c, png())
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    html = r.text
    assert values(html, "doc_number") == ["3940299481"]
    assert values(html, "total_doc") == ["5022"]
    assert values(html, "pax_name") == ["SYNTHA/ONE"]
    assert values(html, "rloc") == ["ZX6AGG"]
    assert values(html, "pb") == ["5400"]
    assert values(html, "staff_sign") == [SIGN]
    assert values(html, "source") == ["screenshot"]
    assert 'action="/sales/preview"' in html
    assert counts(env) == before  # nothing is written but the usage row
    assert [c.model for c in fake.calls] == [HAIKU]


def test_three_adults_give_three_proposals(env: Env) -> None:
    with_ai(env, [pnr("three_adults_rm")])
    r = shoot(env.staff_a(), png())
    text = text_of(r.text)
    assert r.status_code == 200 and text.count("Saisir cette vente") == 3
    for name in ("SYNTHA/ONE", "SYNTHB/TWO", "SYNTHC/THREE"):
        assert name in text
    assert fr(5400) in text


def test_a_layover_of_two_pages_is_two_proposals_of_two_tickets_at_51300(env: Env) -> None:
    first, second = layover_pages()
    fake = with_ai(env, [first, second])
    r = shoot(env.staff_a(), png(), png(color=(1, 1, 1)))
    text = text_of(r.text)
    assert r.status_code == 200 and text.count("Saisir cette vente") == 2
    assert text.count(fr(51300)) == 2
    for doc in ("3940299469", "3940299470", "3940299471", "3940299472"):
        assert doc in text
    assert len(fake.calls) == 2  # both pages read cheaply: no fallback on a first page


def test_a_transcript_with_lost_indentation_and_fences_is_still_read(env: Env) -> None:
    noisy = "```\n" + "\n".join(x.lstrip() for x in pnr("three_adults_rm").splitlines()) + "\n```"
    fake = with_ai(env, [noisy])
    r = shoot(env.staff_a(), png())
    assert text_of(r.text).count("Saisir cette vente") == 3
    assert len(fake.calls) == 1


def test_four_images_at_most(env: Env) -> None:
    fake = with_ai(env, [])
    r = shoot(env.staff_a(), *[png() for _ in range(5)])
    assert r.status_code == 400 and "4 images au plus" in text_of(r.text)
    assert fake.calls == [] and usage(env) == []


def test_no_image_is_a_french_message(env: Env) -> None:
    with_ai(env, [])
    r = shoot(env.staff_a())
    assert r.status_code == 400 and "Choisissez une photo" in text_of(r.text)
    # an empty file input (a browser sends one with no name) is the same
    c = env.staff_a()
    empty = c.post(
        "/sales/screenshot",
        data={"csrf_token": csrf_for(c, "/sales/new")},
        files=[("images", ("", b"", "application/octet-stream"))],
    )
    assert empty.status_code == 400 and "Choisissez une photo" in text_of(empty.text)


# --- what is sent, and what is kept -------------------------------------------------------------


def test_the_model_gets_a_clean_downscaled_jpeg(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    shoot(env.staff_a(), jpeg_with_exif(size=(3000, 1500)))
    sent = fake.calls[0]
    assert sent.media_type == "image/jpeg"
    image = Image.open(io.BytesIO(sent.image))
    assert image.format == "JPEG" and max(image.size) == 1568
    assert len(image.getexif()) == 0
    assert GPS_MARK not in sent.image and b"SecretCamera" not in sent.image


def test_nothing_of_the_image_or_the_text_is_logged_or_audited(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with_ai(env, [one_passenger()])
    c = env.staff_a()
    with caplog.at_level(logging.DEBUG):
        r = shoot(c, jpeg_with_exif())
    assert r.status_code == 200
    for secret in ("3940299481", "SYNTHA", "SYNTHCLIENT", "ZX6AGG", "SecretCamera", "RP/NKC"):
        assert secret not in caplog.text, secret
    for event in table(env, AuditEvent):
        assert "SYNTH" not in str(event.details) and "3940299481" not in str(event.details)
    for row in usage(env):  # the usage row is numbers and a model name, nothing else
        assert set(vars(row)) >= {"cost_micro_usd"} and "SYNTH" not in str(vars(row))


def test_the_image_never_goes_to_a_temporary_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 3 MB upload would spill out of Starlette's 1 MB default spool; the app raises it."""
    import tempfile

    def no_disk(self: Any, *a: Any, **k: Any) -> None:
        raise AssertionError("an upload was written to a temporary file")

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", no_disk)
    with_ai(env, [one_passenger()])
    big = io.BytesIO()
    noise = Image.frombytes("RGB", (1400, 900), bytes(range(256)) * (1400 * 900 * 3 // 256 + 1))
    noise.save(big, format="BMP")  # not allowed: it must be refused, still in memory
    c = env.staff_a()
    r = shoot(c, big.getvalue())
    assert r.status_code == 400  # a BMP of 3.7 MB: refused as a format, never written out
    ok = io.BytesIO()
    rnd = Image.effect_noise((1600, 1200), 80).convert("RGB")
    rnd.save(ok, format="PNG")
    assert len(ok.getvalue()) > 1_100_000  # above the default 1 MB spool
    assert shoot(c, ok.getvalue()).status_code == 200


def test_the_usage_row_has_the_cost_from_the_tokens(env: Env) -> None:
    with_ai(env, [one_passenger()])
    shoot(env.staff_a(), png())
    (row,) = usage(env)
    assert (row.purpose, row.model, row.outcome) == ("screenshot", HAIKU, "ok")
    assert (row.input_tokens, row.output_tokens) == (650, 400)
    assert row.cost_micro_usd == HAIKU_COST == 265
    assert row.user_id == env.seeded.staff_a and row.agency_id == env.seeded.agency_a


# --- the fallback ------------------------------------------------------------------------------


def test_a_bad_cheap_read_is_read_again_by_the_stronger_model(env: Env) -> None:
    fake = with_ai(env, ["Je ne vois rien d'utile", one_passenger()])
    r = shoot(env.staff_a(), png())
    assert r.status_code == 200 and values(r.text, "doc_number") == ["3940299481"]
    assert [c.model for c in fake.calls] == [HAIKU, SONNET]
    assert fake.calls[0].image == fake.calls[1].image  # the same image, not a new one
    assert [(u.model, u.outcome, u.cost_micro_usd) for u in usage(env)] == [
        (HAIKU, "ok", HAIKU_COST),
        (SONNET, "fallback", SONNET_COST),
    ]


def test_when_the_stronger_model_fails_the_first_transcript_is_kept(env: Env) -> None:
    fake = with_ai(env, [garbled(), AiUnavailable("timeout")])
    r = shoot(env.staff_a(), png())
    assert r.status_code == 200 and values(r.text, "pax_name") == ["SYNTHA/ONE"]
    assert [c.model for c in fake.calls] == [HAIKU, SONNET]
    assert [(u.model, u.outcome) for u in usage(env)] == [(HAIKU, "ok"), (SONNET, "error")]


def test_a_good_read_costs_one_call(env: Env) -> None:
    fake = with_ai(env, [pnr("adults_with_infants")])
    shoot(env.staff_a(), png())
    assert len(fake.calls) == 1


def test_a_truncated_answer_is_read_again_even_when_it_reads_well(env: Env) -> None:
    cut = Transcription(one_passenger(), 650, 4096, HAIKU, truncated=True)
    fake = with_ai(env, [cut, one_passenger()])
    assert shoot(env.staff_a(), png()).status_code == 200
    assert [c.model for c in fake.calls] == [HAIKU, SONNET]


def test_the_fallback_is_logged_only_when_the_second_read_worked(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    with_ai(env, [garbled(), one_passenger()])
    with caplog.at_level(logging.INFO):
        shoot(env.staff_a(), png())
    assert [r.getMessage() for r in caplog.records if "fallback" in r.getMessage()] == [
        "ai fallback"
    ]
    caplog.clear()
    with_ai(env, [garbled(), AiUnavailable("server")])
    with caplog.at_level(logging.INFO):
        assert shoot(env.staff_a(), png()).status_code == 200  # the first read is kept
    messages = [r.getMessage() for r in caplog.records if "fallback" in r.getMessage()]
    assert messages == ["ai fallback failed, first read kept"]


# --- the budget --------------------------------------------------------------------------------


def test_a_spent_budget_refuses_before_any_call(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    set_budget(env, 1000)  # under the estimate of one Haiku call
    r = shoot(env.staff_a(), png())
    assert r.status_code == 403 and BUDGET_MESSAGE in text_of(r.text)
    assert 'name="pnr_text"' in r.text  # the paste box is right there
    assert fake.calls == []
    (row,) = usage(env)
    assert (row.outcome, row.cost_micro_usd, row.input_tokens, row.output_tokens) == (
        "refused_budget",
        0,
        0,
        0,
    )


def test_the_default_budget_is_5_dollars_and_comes_from_the_settings(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    with owner_session(env.db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, env.seeded.agency_a)
        s.add(
            AiUsage(
                agency_id=env.seeded.agency_a,
                user_id=env.seeded.staff_a,
                created_at=datetime(2026, 10, 2, tzinfo=UTC),
                purpose="screenshot",
                model=HAIKU,
                input_tokens=1,
                output_tokens=1,
                cost_micro_usd=5_000_000 - 100,  # 100 micro-USD left: under any estimate
                outcome="ok",
            )
        )
        s.commit()
    r = shoot(env.staff_a(), png())
    assert r.status_code == 403 and BUDGET_MESSAGE in text_of(r.text) and fake.calls == []


def test_last_months_spending_does_not_count(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    with owner_session(env.db.owner_url) as s:
        from app.tenancy import rebind_tenant

        rebind_tenant(s, env.seeded.agency_a)
        s.add(
            AiUsage(
                agency_id=env.seeded.agency_a,
                user_id=env.seeded.staff_a,
                created_at=datetime(2026, 9, 30, 23, 59, tzinfo=UTC),  # the clock says 18 Oct
                purpose="screenshot",
                model=SONNET,
                input_tokens=1,
                output_tokens=1,
                cost_micro_usd=9_000_000,
                outcome="ok",
            )
        )
        s.commit()
    assert shoot(env.staff_a(), png()).status_code == 200 and len(fake.calls) == 1


def test_spending_adds_up_across_the_pages_and_stops_the_one_over(env: Env) -> None:
    pages = layover_pages()
    fake = with_ai(env, [pages[0], pages[1], pages[0], pages[1]])
    set_budget(env, 2300)  # estimate 1650, each call 265: 530 + 1650 fits, 795 + 1650 does not
    r = shoot(env.staff_a(), png(), png(), png(), png())
    assert r.status_code == 403 and BUDGET_MESSAGE in text_of(r.text)
    assert len(fake.calls) == 3
    assert [u.outcome for u in usage(env)] == ["ok", "ok", "ok", "refused_budget"]
    assert sum(u.cost_micro_usd for u in usage(env)) == 3 * HAIKU_COST


def test_a_refused_fallback_keeps_the_first_transcript(env: Env) -> None:
    fake = with_ai(env, [garbled()])
    set_budget(env, 5000)  # Haiku estimate 1650 passes; after it 265 + Sonnet's 33000 does not
    r = shoot(env.staff_a(), png())
    assert r.status_code == 200 and len(fake.calls) == 1
    assert [u.outcome for u in usage(env)] == ["ok", "refused_budget"]


# --- errors are French messages, never a 500 -----------------------------------------------------


@pytest.mark.parametrize("kind", ["timeout", "connection", "server", "rate_limit", "auth"])
def test_an_api_failure_is_a_message_suggesting_the_paste(env: Env, kind: str) -> None:
    with_ai(env, [AiUnavailable(kind)])
    r = shoot(env.staff_a(), png())
    assert r.status_code == 503 and DOWN_MESSAGE in text_of(r.text)
    assert 'name="pnr_text"' in r.text
    assert [u.outcome for u in usage(env)] == ["error"]


def test_a_timeout_costs_the_estimate_other_failures_cost_nothing(env: Env) -> None:
    """A call that timed out may have been billed: it counts the estimate against the budget."""
    with_ai(env, [AiUnavailable("timeout"), AiUnavailable("server")])
    shoot(env.staff_a(), png())
    shoot(env.staff_a(), png())
    timeout, server = usage(env)
    assert (timeout.outcome, timeout.cost_micro_usd) == ("error", pricing.estimate_micro_usd(HAIKU))
    assert (server.outcome, server.cost_micro_usd) == ("error", 0)


def test_a_usage_row_that_cannot_be_committed_is_a_message_and_an_error_log(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    paid = {"yes": False, "failed": False}
    real_commit = Session.commit

    def commit(self: Session) -> None:
        if paid["yes"] and not paid["failed"]:
            paid["failed"] = True
            raise OperationalError("INSERT", {}, Exception("db down"))
        real_commit(self)

    def reply(*_: Any) -> str:
        paid["yes"] = True  # from now on, the next commit (the usage row) fails once
        return one_passenger()

    with_ai(env, reply)
    monkeypatch.setattr(Session, "commit", commit)
    with caplog.at_level(logging.ERROR):
        r = shoot(env.staff_a(), png())
    assert r.status_code == 503 and DOWN_MESSAGE in text_of(r.text)
    errors = [x for x in caplog.records if x.levelno == logging.ERROR]
    assert errors and errors[0].model == HAIKU and errors[0].cost_micro_usd == HAIKU_COST  # type: ignore[attr-defined]
    assert "SYNTH" not in caplog.text


def test_an_unexpected_client_bug_is_also_a_message(env: Env) -> None:
    with_ai(env, [RuntimeError("boom with PNR ABC123")])
    r = shoot(env.staff_a(), png())
    assert r.status_code == 503 and DOWN_MESSAGE in text_of(r.text)
    assert "ABC123" not in r.text


def test_a_transcript_with_no_sale_is_a_message_and_the_form_stays_blank(env: Env) -> None:
    with_ai(env, ["texte sans réservation", "toujours rien"])
    r = shoot(env.staff_a(), png())
    assert r.status_code == 400 and "Aucune vente lisible dans cette image" in text_of(r.text)
    assert "collez le texte Amadeus" in text_of(r.text)


@pytest.mark.parametrize(
    ("blob", "fragment"),
    [
        pytest.param(b"%PDF-1.4 hello", "Image 1 : le format n'est pas pris en charge", id="pdf"),
        pytest.param(HEIC, "HEIC", id="heic"),
        pytest.param(TRUNCATED_JPEG, "Image 1 :", id="truncated-jpeg"),
        pytest.param(b"a" * (8 * 1024 * 1024 + 1), "8 Mo", id="over-8-mb"),
    ],
)
def test_an_unusable_image_is_a_message_and_no_call_is_made(
    env: Env, blob: bytes, fragment: str
) -> None:
    fake = with_ai(env, [one_passenger()])
    r = shoot(env.staff_a(), blob)
    assert r.status_code in (400, 413) and fragment in text_of(r.text)
    assert fake.calls == [] and usage(env) == []


def test_the_bad_image_is_named_by_its_number(env: Env) -> None:
    fake = with_ai(env, [])
    r = shoot(env.staff_a(), png(), b"not an image")
    assert "Image 2 :" in text_of(r.text) and fake.calls == []  # nothing is paid for a bad set


def test_too_many_pixels_is_a_message(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai import images

    monkeypatch.setattr(images, "MAX_PIXELS", 100)
    fake = with_ai(env, [])
    r = shoot(env.staff_a(), png(size=(50, 50)))
    assert r.status_code == 400 and "dimensions" in text_of(r.text) and fake.calls == []


def test_a_body_over_the_screenshot_limit_is_the_413_page(env: Env) -> None:
    with_ai(env, [])
    c = env.staff_a()
    token = csrf_for(c, "/sales/new")
    blob = b"\xff\xd8" + b"0" * (34 * 1024 * 1024)  # four 8 MB photos plus a megabyte is the cap
    r = c.post(
        "/sales/screenshot",
        data={"csrf_token": token},
        files=[("images", ("big.jpg", blob, "image/jpeg"))],
    )
    assert r.status_code == 413


def test_one_image_over_8_mb_is_refused_with_its_own_message(env: Env) -> None:
    fake = with_ai(env, [])
    blob = b"\xff\xd8" + b"0" * (11 * 1024 * 1024)
    r = shoot(env.staff_a(), blob)
    assert r.status_code == 400 and "Image 1 :" in text_of(r.text) and "8 Mo" in text_of(r.text)
    assert fake.calls == []


def big_png(side: int = 1300) -> bytes:
    """About 5 MB: random pixels do not compress."""
    out = io.BytesIO()
    Image.frombytes("RGB", (side, side), os.urandom(side * side * 3)).save(out, format="PNG")
    assert 4_500_000 < len(out.getvalue()) < MAX_IMAGE_BYTES
    return out.getvalue()


def test_two_phone_photos_of_5_mb_reach_the_route(env: Env) -> None:
    """The global cap is 10 MB; the screenshot route has its own, for four 8 MB photos."""
    fake = with_ai(env, lambda *_: one_passenger())
    first, second = big_png(1500), big_png(1500)  # 6.8 MB each
    r = shoot(env.staff_a(), first, second)
    assert r.status_code == 200, r.status_code
    assert len(fake.calls) == 2
    assert len(first) + len(second) > env.factory.app.state.settings.max_request_bytes  # type: ignore[attr-defined]


def test_the_larger_cap_is_for_the_screenshot_path_only(env: Env) -> None:
    with_ai(env, [])
    c = env.staff_a()
    token = csrf_for(c, "/sales/new")
    r = c.post("/sales/paste", data={"csrf_token": token, "pnr_text": "A" * (11 * 1024 * 1024)})
    assert r.status_code == 413
    anonymous = env.factory().post(
        "/login", content=b"x" * (11 * 1024 * 1024), headers={"Content-Type": "text/plain"}
    )
    assert anonymous.status_code == 413


# --- the body is read only after the cheap gates ----------------------------------------------


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Records every multipart parse; a test asserts the list stayed empty where it must."""
    from starlette.formparsers import MultiPartParser

    seen: list[bool] = []
    real = MultiPartParser.parse

    async def spy(self: MultiPartParser) -> Any:
        seen.append(True)
        return await real(self)

    monkeypatch.setattr(MultiPartParser, "parse", spy)
    return seen


def test_an_anonymous_post_is_never_parsed(env: Env, parsed: list[bool]) -> None:
    with_ai(env, [one_passenger()])
    r = env.factory().post(
        "/sales/screenshot",
        files=[("images", ("a.png", png(), "image/png"))],
        data={"csrf_token": "x" * 43},
        follow_redirects=False,
    )
    assert r.status_code in (303, 401, 403) and parsed == []


def test_a_foreign_origin_is_refused_before_the_body_is_read(env: Env, parsed: list[bool]) -> None:
    fake = with_ai(env, [one_passenger()])
    c = env.staff_a()
    token = csrf_for(c, "/sales/new")
    r = c.post(
        "/sales/screenshot",
        data={"csrf_token": token},
        files=[("images", ("a.png", png(), "image/png"))],
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403 and parsed == [] and fake.calls == []


def test_a_wrong_header_token_is_refused_before_the_body_is_read(
    env: Env, parsed: list[bool]
) -> None:
    fake = with_ai(env, [one_passenger()])
    c = env.staff_a()
    r = c.post(
        "/sales/screenshot",
        files=[("images", ("a.png", png(), "image/png"))],
        headers={"X-CSRF-Token": "y" * 43},
    )
    assert r.status_code == 403 and parsed == [] and fake.calls == []


def test_a_good_header_token_works_without_the_form_field(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    c = env.staff_a()
    token = csrf_for(c, "/sales/new")
    r = c.post(
        "/sales/screenshot",
        files=[("images", ("a.png", png(), "image/png"))],
        headers={"X-CSRF-Token": token},
    )
    assert r.status_code == 200 and len(fake.calls) == 1


def test_a_wrong_form_token_is_refused_and_gives_the_rate_limit_hit_back(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "SCREENSHOT_USER", ratelimit.Limit("screenshot_user", 1, 3600))
    fake = with_ai(env, [one_passenger()])
    c = env.staff_a()
    assert shoot(c, png(), token="z" * 43).status_code == 403
    assert shoot(c, png()).status_code == 200  # the one hit of the hour was not spent
    assert len(fake.calls) == 1
    assert any(e.event_type == "csrf_failed" for e in table(env, AuditEvent))


def test_a_request_with_no_image_or_too_many_counts_one_hit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "SCREENSHOT_USER", ratelimit.Limit("screenshot_user", 2, 3600))
    with_ai(env, [one_passenger()])
    c = env.staff_a()
    assert shoot(c).status_code == 400  # no image: 1 of 2
    assert shoot(c, *[png() for _ in range(5)]).status_code == 400  # too many: 2 of 2
    assert shoot(c, png()).status_code == 429


def test_more_files_than_the_parser_allows_is_a_message(env: Env) -> None:
    fake = with_ai(env, [])
    r = shoot(env.staff_a(), *[png() for _ in range(9)])
    assert r.status_code == 400 and "4 images au plus" in text_of(r.text)
    assert fake.calls == []


def test_the_screenshot_parser_keeps_even_big_files_in_memory(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tempfile

    def no_disk(self: Any, *a: Any, **k: Any) -> None:
        raise AssertionError("an upload was written to a temporary file")

    monkeypatch.setattr(tempfile.SpooledTemporaryFile, "rollover", no_disk)
    with_ai(env, lambda *_: one_passenger())
    assert shoot(env.staff_a(), big_png(), big_png(), big_png()).status_code == 200


# --- who, csrf, rate limit ---------------------------------------------------------------------


def test_csrf_is_required(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    c = env.staff_a()
    no_token = c.post("/sales/screenshot", files=[("images", ("a.png", png(), "image/png"))])
    bad_token = shoot(c, png(), token="x" * 43)
    assert no_token.status_code == 403 and bad_token.status_code == 403
    assert fake.calls == []


def test_an_anonymous_visitor_is_turned_away(env: Env) -> None:
    fake = with_ai(env, [one_passenger()])
    r = env.factory().post(
        "/sales/screenshot",
        files=[("images", ("a.png", png(), "image/png"))],
        follow_redirects=False,
    )
    assert r.status_code in (303, 401, 403) and fake.calls == []


def test_the_manager_and_the_accountant_may_use_it_too(env: Env) -> None:
    from tests.test_sale_pages import accountant

    with_ai(env, [one_passenger(), one_passenger()])
    for c in (env.manager_a(), accountant(env)):
        assert shoot(c, png()).status_code == 200


def test_a_user_of_another_agency_is_charged_to_their_own_agency(env: Env) -> None:
    with_ai(env, [one_passenger()])
    shoot(env.manager_a(), png())
    assert len(usage(env)) == 1 and table(env, AiUsage, env.seeded.agency_b) == []


def test_each_image_counts_against_the_hourly_limit(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ratelimit, "SCREENSHOT_USER", ratelimit.Limit("screenshot_user", 3, 3600))
    fake = with_ai(env, lambda *_: one_passenger())
    c = env.staff_a()
    assert shoot(c, png(), png()).status_code == 200  # 2 of 3
    assert shoot(c, png()).status_code == 200  # 3 of 3
    r = shoot(c, png())
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert len(fake.calls) == 3


def test_the_default_limit_is_60_an_hour() -> None:
    assert (ratelimit.SCREENSHOT_USER.max_hits, ratelimit.SCREENSHOT_USER.window_seconds) == (
        60,
        3600,
    )


# --- the sale itself ------------------------------------------------------------------------------


def test_the_agency_question_is_enforced_and_the_sale_is_labelled_screenshot(env: Env) -> None:
    with_ai(env, [one_passenger()])
    c = env.staff_a()
    form = fields_of(shoot(c, png()).text, "/sales/preview")
    assert form["source"] == ["screenshot"]
    # the agent has to answer the agency question: without it the read-back is refused
    unanswered = c.post("/sales/preview", data=override(form, staff_sign=SIGN))
    assert unanswered.status_code == 400 and "Confirmez l'agence" in text_of(unanswered.text)
    assert table(env, Sale) == []
    answered = override(form, staff_sign=SIGN, payment_method="cash", ag_answer="direct")
    sale_id = sale_id_from(preview_then_confirm(c, answered))
    versions = [v for v in table(env, SaleVersion) if v.sale_id == sale_id]
    assert [v.source for v in versions] == ["screenshot"]


def test_a_forged_source_is_still_refused(env: Env) -> None:
    with_ai(env, [one_passenger()])
    c = env.staff_a()
    form = fields_of(shoot(c, png()).text, "/sales/preview")
    r = c.post("/sales/preview", data=override(form, staff_sign=SIGN, source="magic"))
    assert r.status_code == 400
