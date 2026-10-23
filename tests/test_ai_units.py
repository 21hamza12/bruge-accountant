"""Phase D units: prices, the fake client, the real client on a stubbed SDK, the fallback rule,
image validation and cleaning. No network and no database."""

from __future__ import annotations

import io
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from app.ai import fallback, images, pricing
from app.ai.client import PROMPT, AiUnavailable, AnthropicTranscriber, Transcription
from app.ai.fake import FakeTranscribeClient
from app.parsing.pnr import parse_pnr_text
from app.settings import Settings
from tests.ai_support import GPS_MARK, jpeg_with_exif, layover_pages, png, pnr, webp

HAIKU, SONNET = "claude-haiku-5-5", "claude-sonnet-5-5"


# --- prices ------------------------------------------------------------------------------------


def test_cost_in_micro_usd_from_tokens() -> None:
    # 1 USD per million tokens is exactly 1 micro-USD per token
    assert pricing.cost_micro_usd(SONNET, 650, 400) == 650 * 2 + 400 * 10 == 5300
    # the first real screenshot read by Haiku: 0.0005622 USD on the Anthropic bill
    assert pricing.cost_micro_usd(HAIKU, 662, 992) == 563  # 66.2 + 496 -> 563
    assert pricing.cost_micro_usd(HAIKU, 0, 0) == 0


def test_the_cost_is_rounded_up_never_down() -> None:
    assert pricing.cost_micro_usd(HAIKU, 1, 0) == 1  # 0.10 micro-USD counts as 1
    assert pricing.cost_micro_usd(HAIKU, 10, 1) == 2  # 1.0 + 0.5 = 1.5 -> 2


def test_an_unknown_model_is_priced_like_the_dearest() -> None:
    assert pricing.cost_micro_usd("claude-future-9", 100, 100) == pricing.cost_micro_usd(
        SONNET, 100, 100
    )


def test_negative_tokens_are_refused() -> None:
    with pytest.raises(ValueError):
        pricing.cost_micro_usd(HAIKU, -1, 0)


def test_the_measured_cost_per_screenshot_is_covered() -> None:
    """On the 16 real screenshots: Haiku about 0.00075 USD, Sonnet about 0.0081 USD (typical
    reads of about 650 tokens in and 1 300 out on Haiku, 700 out on Sonnet)."""
    assert 700 <= pricing.cost_micro_usd(HAIKU, 650, 1300) <= 1000
    assert 7500 <= pricing.cost_micro_usd(SONNET, 650, 700) <= 9000


def test_the_estimate_is_above_the_largest_real_read() -> None:
    # the largest of the 16 real screens: 904 tokens in, 2 561 out
    assert pricing.estimate_micro_usd(HAIKU) > pricing.cost_micro_usd(HAIKU, 904, 2561)
    assert pricing.estimate_micro_usd(SONNET) > pricing.cost_micro_usd(SONNET, 904, 2561)


def test_usd_conversions() -> None:
    assert pricing.usd_to_micro(Decimal("5")) == 5_000_000
    assert pricing.usd_to_micro(Decimal("0.0000001")) == 1  # up, never down
    assert pricing.micro_to_usd_text(1_234_567) == "1.234567"
    assert pricing.micro_to_usd_text(0) == "0"
    assert pricing.micro_to_usd_text(5_000_000) == "5"


# --- the fake client ---------------------------------------------------------------------------


def test_the_fake_answers_in_order_and_records_calls() -> None:
    fake = FakeTranscribeClient(["one", "two"])
    a = fake.transcribe(b"x", "image/jpeg", HAIKU)
    b = fake.transcribe(b"y", "image/jpeg", SONNET)
    assert (a.text, b.text) == ("one", "two")
    assert (a.input_tokens, a.output_tokens, a.model) == (650, 400, HAIKU)
    assert [c.model for c in fake.calls] == [HAIKU, SONNET]
    assert [c.image for c in fake.calls] == [b"x", b"y"]


def test_the_fake_raises_scripted_errors_and_runs_out() -> None:
    fake = FakeTranscribeClient([AiUnavailable("timeout")])
    with pytest.raises(AiUnavailable):
        fake.transcribe(b"x", "image/jpeg", HAIKU)
    with pytest.raises(AssertionError):
        fake.transcribe(b"x", "image/jpeg", HAIKU)


def test_the_fake_accepts_a_callable_and_a_transcription() -> None:
    fake = FakeTranscribeClient(lambda image, media, model: Transcription("t", 1, 2, model))
    assert fake.transcribe(b"", "image/png", SONNET) == Transcription("t", 1, 2, SONNET)


# --- the real client, on a stubbed SDK (no network) ---------------------------------------------


class _Messages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _real(outcome: Any) -> tuple[AnthropicTranscriber, _Messages]:
    transcriber = AnthropicTranscriber("sk-test-not-real", timeout_s=5, max_retries=1)
    messages = _Messages(outcome)
    transcriber._client = SimpleNamespace(messages=messages)  # type: ignore[assignment]
    return transcriber, messages


def _reply(
    text: str = "RP/X", tin: int = 600, tout: int = 300, stop_reason: str = "end_turn"
) -> Any:
    import anthropic

    return SimpleNamespace(
        content=[anthropic.types.TextBlock(type="text", text=text, citations=None)],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
        stop_reason=stop_reason,
    )


def test_the_real_client_sends_the_image_and_the_prompt_and_reads_the_usage() -> None:
    client, messages = _real(_reply("HELLO", 612, 87))
    result = client.transcribe(b"\xff\xd8jpegbytes", "image/jpeg", HAIKU)
    assert result == Transcription("HELLO", 612, 87, HAIKU)
    sent = messages.kwargs
    assert sent["model"] == HAIKU and 0 < sent["max_tokens"] <= 8192
    content = sent["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert content[0]["source"]["type"] == "base64"
    assert content[1] == {"type": "text", "text": PROMPT}


def test_a_cut_off_answer_is_reported_as_truncated() -> None:
    cut, _ = _real(_reply("RP/X", stop_reason="max_tokens"))
    assert cut.transcribe(b"x", "image/jpeg", HAIKU).truncated is True
    whole, _ = _real(_reply("RP/X"))
    assert whole.transcribe(b"x", "image/jpeg", HAIKU).truncated is False


def test_the_prompt_keeps_the_measured_wording() -> None:
    assert PROMPT.startswith(
        "This is a photo or screenshot of an Amadeus travel-agency terminal screen. Transcribe "
        "ALL the text exactly as shown, line by line, keeping each element's line number and the "
        "original spacing as far as possible. Do not interpret, correct, translate or summarise. "
        "If a character is unreadable write ? in its place. Output only the transcribed text"
    )


def _status_error(code: int) -> Exception:
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.invalid/v1/messages")
    response = httpx.Response(code, request=request, json={"error": {"message": "PNR ABC123"}})
    cls = {
        401: anthropic.AuthenticationError,
        429: anthropic.RateLimitError,
        400: anthropic.BadRequestError,
        500: anthropic.InternalServerError,
    }[code]
    return cls("secret body PNR ABC123", response=response, body=None)


@pytest.mark.parametrize(
    ("code", "kind"),
    [(401, "auth"), (429, "rate_limit"), (400, "request"), (500, "server")],
)
def test_sdk_errors_become_a_kind_without_the_message(code: int, kind: str) -> None:
    client, _ = _real(_status_error(code))
    with pytest.raises(AiUnavailable) as caught:
        client.transcribe(b"x", "image/png", HAIKU)
    assert caught.value.kind == kind
    assert "ABC123" not in str(caught.value) and caught.value.__cause__ is None


def test_timeouts_and_connection_errors() -> None:
    import anthropic
    import httpx2 as httpx

    request = httpx.Request("POST", "https://api.invalid/v1/messages")
    for error, kind in (
        (anthropic.APITimeoutError(request=request), "timeout"),
        (anthropic.APIConnectionError(request=request), "connection"),
    ):
        client, _ = _real(error)
        with pytest.raises(AiUnavailable) as caught:
            client.transcribe(b"x", "image/png", HAIKU)
        assert caught.value.kind == kind


def test_an_empty_answer_is_unavailable() -> None:
    client, _ = _real(_reply("   \n"))
    with pytest.raises(AiUnavailable) as caught:
        client.transcribe(b"x", "image/png", HAIKU)
    assert caught.value.kind == "empty"


def test_the_sdk_is_built_with_the_timeout_and_a_limited_retry() -> None:
    client = AnthropicTranscriber("sk-test-not-real", timeout_s=7, max_retries=2)
    assert client._client.max_retries == 2
    assert client._client.timeout == 7


# --- settings ----------------------------------------------------------------------------------


def _settings(**kw: Any) -> Settings:
    return Settings(**kw)


def test_ai_is_off_without_a_key_and_on_with_one() -> None:
    assert _settings().ai_active is False
    assert _settings(anthropic_api_key="sk-x").ai_active is True
    assert _settings(anthropic_api_key="  ").ai_active is False
    assert _settings(anthropic_api_key="sk-x", ai_enabled=False).ai_active is False


def test_defaults() -> None:
    s = _settings()
    assert (s.ai_screenshot_model, s.ai_fallback_model) == (HAIKU, SONNET)
    assert s.ai_monthly_budget_usd == 5
    assert s.ai_max_retries == 1


def _prod(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "production",
        "allowed_hosts": ["app.example.com"],
        "database_url": "postgresql://app:pw@db.internal/bruge",
    }
    return Settings(**{**base, **kw})


def test_production_refuses_ai_enabled_without_a_key() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        _prod(ai_enabled=True)
    assert _prod().ai_active is False  # unset and no key: the feature is simply hidden
    assert _prod(ai_enabled=False).ai_active is False
    assert _prod(ai_enabled=True, anthropic_api_key="sk-x").ai_active is True


def test_the_api_key_is_never_shown() -> None:
    s = _settings(anthropic_api_key="sk-very-secret")
    assert "sk-very-secret" not in repr(s) and "sk-very-secret" not in str(s.model_dump())


# --- the fallback rule -------------------------------------------------------------------------


def reason(text: str) -> str | None:
    return fallback.fallback_reason(text, parse_pnr_text(text))


@pytest.mark.parametrize(
    "name",
    ["three_adults_rm", "adult_and_child", "adults_with_infants", "emd", "exchange"],
)
def test_a_good_full_read_needs_no_fallback(name: str) -> None:
    assert reason(pnr(name)) is None


def test_the_first_page_of_a_layover_is_not_a_reason() -> None:
    first, second = layover_pages()
    assert reason(first) is None  # names and segments only: its tickets are on the next page
    assert reason(second) is None


def test_no_rp_line_is_a_reason() -> None:
    text = "\n".join(line for line in pnr("three_adults_rm").splitlines() if "RP/" not in line)
    assert reason(text) == fallback.NO_RP
    assert reason("I cannot read this image.") == fallback.NO_RP


def test_ticketing_elements_but_no_fa_ticket_is_a_reason() -> None:
    lines = [
        line
        for line in pnr("three_adults_rm").splitlines()
        if " FA " not in line and not line.startswith("      ")
    ]
    assert reason("\n".join(lines)) == fallback.NO_TICKET


def test_an_unreadable_amount_is_a_reason() -> None:
    text = pnr("three_adults_rm").replace("MRU5022.00", "MRU50?2.00", 1)
    assert reason(text) == fallback.NO_COST


def test_a_foreign_currency_is_not_a_reason_the_screen_says_so() -> None:
    text = pnr("three_adults_rm").replace("MRU5022.00", "EUR100.00", 1)
    assert reason(text) is None


def test_a_question_mark_in_a_name_is_not_a_reason() -> None:
    text = pnr("three_adults_rm").replace("SYNTHA/ONE", "SYNTH?/ONE", 1)
    assert reason(text) is None


def test_a_question_mark_in_a_ticketing_line_is_a_reason() -> None:
    text = pnr("three_adults_rm").replace("FB PAX 0000000000", "FB PAX 00000?0000", 1)
    assert reason(text) == fallback.GARBLED
    text = pnr("three_adults_rm").replace("SYNTHCLIENT", "SYNTH?LIENT", 1)  # an FP line
    assert reason(text) == fallback.GARBLED


def test_a_question_mark_inside_a_digit_run_is_a_reason() -> None:
    text = pnr("three_adults_rm").replace("37353400", "3735?400", 1)  # the AP phone
    assert reason(text) == fallback.GARBLED


def test_a_currency_warning_only_explains_its_own_ticket() -> None:
    import dataclasses

    text = pnr("three_adults_rm").replace("MRU5022.00", "MRU50?2.00", 1)
    reading = parse_pnr_text(text)
    other = "Billet 1111111111 : devise USD au lieu de MRU, coût à saisir."
    unrelated = dataclasses.replace(reading, warnings=(*reading.warnings, other))
    assert fallback.fallback_reason(text, unrelated) == fallback.NO_COST
    both = pnr("three_adults_rm").replace("MRU5022.00", "EUR100.00", 1)
    both = both.replace("MRU5022.00", "MRU50?2.00", 1)  # one in EUR, one unreadable
    assert reason(both) == fallback.NO_COST


def test_a_truncated_answer_is_a_reason_even_when_it_reads_well() -> None:
    text = pnr("three_adults_rm")
    reading = parse_pnr_text(text)
    assert fallback.fallback_reason(text, reading) is None
    assert (
        fallback.fallback_reason(text, reading, truncated=True) == fallback.TRUNCATED == "truncated"
    )


def test_an_fa_line_that_cannot_be_read_is_a_reason() -> None:
    text = pnr("three_adults_rm").replace("/ETL6/", "/ETL6//", 1)
    assert reason(text) in (fallback.GARBLED, fallback.NO_COST, fallback.NO_TICKET)
    assert reason(text) is not None


# --- images ------------------------------------------------------------------------------------


def decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_a_png_becomes_a_jpeg() -> None:
    prepared = images.prepare_image(png())
    assert prepared.media_type == "image/jpeg"
    assert decode(prepared.data).format == "JPEG"
    assert (prepared.width, prepared.height) == (320, 200)


def test_webp_is_accepted() -> None:
    assert decode(images.prepare_image(webp()).data).format == "JPEG"


def test_exif_gps_and_comments_are_removed() -> None:
    raw = jpeg_with_exif()
    assert GPS_MARK in raw and b"SecretCamera" in raw  # the input does carry them
    prepared = images.prepare_image(raw)
    out = decode(prepared.data)
    assert len(out.getexif()) == 0
    assert "exif" not in out.info and "comment" not in out.info and "icc_profile" not in out.info
    assert GPS_MARK not in prepared.data and b"SecretCamera" not in prepared.data
    assert b"private comment" not in prepared.data and b"Exif" not in prepared.data


def test_the_exif_orientation_is_applied_before_it_is_dropped() -> None:
    raw = jpeg_with_exif(size=(400, 100), orientation=6)  # "rotate 90 degrees to show it"
    prepared = images.prepare_image(raw)
    assert (prepared.width, prepared.height) == (100, 400)


def test_a_large_image_is_downscaled_to_1568_on_the_long_edge() -> None:
    prepared = images.prepare_image(png(size=(3136, 1000)))
    assert (prepared.width, prepared.height) == (1568, 500)
    tall = images.prepare_image(png(size=(900, 4000)))
    assert tall.height == 1568 and tall.width == round(900 * 1568 / 4000)


def test_a_small_image_is_not_enlarged() -> None:
    prepared = images.prepare_image(png(size=(100, 50)))
    assert (prepared.width, prepared.height) == (100, 50)


def test_transparency_is_laid_on_white() -> None:
    out = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(out, format="PNG")
    pixel = decode(images.prepare_image(out.getvalue()).data).getpixel((5, 5))
    assert isinstance(pixel, tuple) and min(pixel) > 240


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"", "vide"),
        (b"%PDF-1.4 not an image", "format"),
        (b"GIF89a" + b"\x00" * 40, "format"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "format"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "abîmée"),  # a PNG header and nothing after
    ],
)
def test_wrong_or_broken_files_are_refused_in_french(raw: bytes, fragment: str) -> None:
    with pytest.raises(images.ImageRejected) as caught:
        images.prepare_image(raw)
    assert fragment in str(caught.value)


def test_heic_gets_a_message_that_says_what_to_do() -> None:
    heic = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 50
    with pytest.raises(images.ImageRejected, match="JPEG"):
        images.prepare_image(heic)


def test_more_than_8_mb_is_refused_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("must not be decoded")

    monkeypatch.setattr(Image, "open", boom)
    with pytest.raises(images.ImageRejected, match="8 Mo"):
        images.prepare_image(b"\xff\xd8" + b"\x00" * images.MAX_IMAGE_BYTES)


def test_too_many_pixels_are_refused_before_decoding(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = png(size=(40, 40))
    monkeypatch.setattr(images, "MAX_PIXELS", 1000)  # 40 x 40 = 1600 pixels
    loaded: list[bool] = []
    real_load = Image.Image.load

    def spy(self: Image.Image, *a: Any, **k: Any) -> Any:
        loaded.append(True)
        return real_load(self, *a, **k)

    monkeypatch.setattr(Image.Image, "load", spy)
    with pytest.raises(images.ImageRejected, match="dimensions"):
        images.prepare_image(raw)
    assert loaded == []  # no pixel was decoded


def test_a_real_decompression_bomb_is_refused() -> None:
    """A PNG of 20 000 x 20 000 white pixels is a few hundred KB and 400 megapixels."""
    out = io.BytesIO()
    Image.new("1", (20000, 20000), 1).save(out, format="PNG", optimize=False)
    assert len(out.getvalue()) < images.MAX_IMAGE_BYTES
    with pytest.raises(images.ImageRejected, match="dimensions"):
        images.prepare_image(out.getvalue())


def test_the_pixel_bound_is_also_set_on_pillow_itself() -> None:
    assert Image.MAX_IMAGE_PIXELS == images.MAX_JPEG_PIXELS


def test_a_png_over_16_megapixels_is_refused_before_decoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mostly-blank PNG is tiny as a file and huge once decoded: PNG and WebP get a lower bound
    than JPEG (which is decoded at reduced size)."""
    assert images.MAX_PIXELS == 16_000_000 and images.MAX_JPEG_PIXELS > images.MAX_PIXELS
    out = io.BytesIO()
    Image.new("1", (4001, 4000), 1).save(out, format="PNG")  # 16.004 MP, a few KB
    loaded: list[bool] = []
    real_load = Image.Image.load

    def spy(self: Image.Image, *a: Any, **k: Any) -> Any:
        loaded.append(True)
        return real_load(self, *a, **k)

    monkeypatch.setattr(Image.Image, "load", spy)
    with pytest.raises(images.ImageRejected, match="dimensions"):
        images.prepare_image(out.getvalue())
    assert loaded == []


def test_a_big_but_allowed_png_comes_out_at_1568_and_is_shrunk_before_it_is_flattened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = io.BytesIO()
    Image.new("RGBA", (4000, 3000), (255, 0, 0, 128)).save(out, format="PNG")  # 12 MP
    seen: list[tuple[int, int]] = []
    real = images._flatten

    def spy(image: Image.Image) -> Image.Image:
        seen.append(image.size)
        return real(image)

    monkeypatch.setattr(images, "_flatten", spy)
    prepared = images.prepare_image(out.getvalue())
    assert (prepared.width, prepared.height) == (1568, 1176)
    assert seen and max(max(size) for size in seen) < 2 * images.MAX_EDGE  # never at 4000 px


@pytest.mark.parametrize("mode", ["P", "L", "1", "LA"])
def test_other_modes_are_shrunk_too(mode: str) -> None:
    out = io.BytesIO()
    Image.new(mode, (3300, 1000)).save(out, format="PNG")
    prepared = images.prepare_image(out.getvalue())
    assert max(prepared.width, prepared.height) == 1568


def test_an_orientation_is_still_applied_on_a_shrunk_image() -> None:
    prepared = images.prepare_image(jpeg_with_exif(size=(4000, 1000), orientation=6))
    assert (prepared.width, prepared.height) == (392, 1568)


def test_an_unexpected_codec_error_is_an_image_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(self: Image.Image, *a: Any, **k: Any) -> Any:
        raise RuntimeError("codec exploded")

    raw = png()
    monkeypatch.setattr(Image.Image, "load", boom)
    with pytest.raises(images.ImageRejected, match="illisible"):
        images.prepare_image(raw)


def test_at_most_two_images_are_decoded_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    lock = threading.Lock()
    now = peak = 0
    real = images._flatten

    def slow(image: Image.Image) -> Image.Image:
        nonlocal now, peak
        with lock:
            now += 1
            peak = max(peak, now)
        time.sleep(0.05)
        with lock:
            now -= 1
        return real(image)

    monkeypatch.setattr(images, "_flatten", slow)
    raw = png()
    threads = [threading.Thread(target=images.prepare_image, args=(raw,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


def test_nothing_is_written_to_disk(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    import tempfile

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("no temporary file")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
    monkeypatch.setattr(tempfile, "TemporaryFile", boom)
    monkeypatch.setattr(tempfile, "mkstemp", boom)
    images.prepare_image(jpeg_with_exif())
