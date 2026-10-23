"""From up to four images (the pages of one booking) to Amadeus text.

``read_screenshots`` is the whole AI part of ``POST /sales/screenshot``; the page then treats the
text exactly as a paste. For each image: validate and clean it (``images``), transcribe it with the
cheap model, read the transcript with ``parse_pnr_text`` and, when ``fallback_reason`` says it is
not good enough, transcribe the SAME image again with the stronger model and use that one (if the
second call is refused or fails, the first transcript is kept). The pages are joined in the order
they were sent.

Every call is preceded by the budget check and followed by a row in ``ai_usage`` (committed at
once: money spent is logged even when a later page fails; see ``app.ai.calls``). Images and
transcripts live in
variables of this function only: nothing is stored, logged or audited, and they are dropped when
it returns. A call that timed out is recorded at the estimated cost (it may have been billed); any
other failure costs 0. A usage row that cannot be committed after a paid call is logged as an error
(model and cost) and answered like an API failure. A failure is a ``ScreenshotError`` whose
message is the French sentence to show.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai import budget, fallback, pricing
from app.ai.calls import BudgetRefused, Caller, CallFailed, Usage
from app.ai.client import TranscribeClient, Transcription
from app.ai.images import ImageRejected, prepare_image
from app.models import User
from app.parsing.pnr import parse_pnr_text
from app.settings import Settings

log = logging.getLogger(__name__)

MAX_IMAGES = 4
NO_IMAGE = "Choisissez une photo ou une capture d'écran Amadeus."
TOO_MANY = f"{MAX_IMAGES} images au plus par envoi (les pages d'une même réservation)."
API_DOWN = "La lecture de l'image n'a pas abouti : collez le texte Amadeus."
NOTHING = (
    "Aucune vente lisible dans cette image : essayez une photo plus nette de l'écran complet, "
    "ou collez le texte Amadeus."
)


class ScreenshotError(Exception):
    """``message`` is shown to the user; ``status`` is the HTTP status of the page."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class ScreenshotRead:
    text: str
    pages: int
    fallbacks: int


class _Caller:
    """One submit's calls: the shared check-call-record (``app.ai.calls``), in screenshot words."""

    def __init__(
        self,
        db: Session,
        client: TranscribeClient,
        settings: Settings,
        user: User,
        now: datetime,
    ) -> None:
        self.client = client
        self.calls = Caller(
            db,
            user,
            now,
            default_budget_usd=settings.ai_monthly_budget_usd,
            purpose=budget.PURPOSE_SCREENSHOT,
            estimate=pricing.estimate_micro_usd,
        )

    def transcribe(self, image: bytes, media_type: str, model: str, outcome: str) -> Transcription:
        """The transcription, or ``ScreenshotError``. ``outcome`` is the usage row's on success."""
        try:
            return self.calls.call(
                model,
                outcome,
                lambda: self.client.transcribe(image, media_type, model),
                lambda result: Usage(result.input_tokens, result.output_tokens),
            )
        except BudgetRefused:
            raise ScreenshotError(budget.BUDGET_MESSAGE, 403) from None
        except CallFailed:
            raise ScreenshotError(API_DOWN, 503) from None


def read_screenshots(
    db: Session,
    *,
    client: TranscribeClient,
    settings: Settings,
    user: User,
    now: datetime,
    images: list[bytes],
) -> ScreenshotRead:
    if not images:
        raise ScreenshotError(NO_IMAGE)
    if len(images) > MAX_IMAGES:
        raise ScreenshotError(TOO_MANY)
    prepared = []
    for number, raw in enumerate(images, start=1):
        try:
            prepared.append(prepare_image(raw))
        except ImageRejected as exc:
            raise ScreenshotError(f"Image {number} : {exc}") from None
    caller = _Caller(db, client, settings, user, now)
    primary, second = settings.ai_screenshot_model, settings.ai_fallback_model
    pages: list[str] = []
    fallbacks = 0
    for image in prepared:
        first = caller.transcribe(image.data, image.media_type, primary, budget.OUTCOME_OK)
        text = first.text
        reading = parse_pnr_text(text, transcript=True)
        reason = fallback.fallback_reason(text, reading, truncated=first.truncated)
        if reason is not None and second and second != primary:
            try:
                text = caller.transcribe(
                    image.data, image.media_type, second, budget.OUTCOME_FALLBACK
                ).text
            except ScreenshotError:  # the refusal or failure is in ai_usage (or the error log)
                log.warning("ai fallback failed, first read kept", extra={"reason": reason})
            else:
                fallbacks += 1
                log.info("ai fallback", extra={"reason": reason})
        pages.append(text.strip("\n"))
    return ScreenshotRead("\n".join(pages), len(pages), fallbacks)
