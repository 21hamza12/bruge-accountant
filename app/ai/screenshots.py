"""From up to four images (the pages of one booking) to Amadeus text.

``read_screenshots`` is the whole AI part of ``POST /sales/screenshot``; the page then treats the
text exactly as a paste. For each image: validate and clean it (``images``), transcribe it with the
cheap model, read the transcript with ``parse_pnr_text`` and, when ``fallback_reason`` says it is
not good enough, transcribe the SAME image again with the stronger model and use that one (if the
second call is refused or fails, the first transcript is kept). The pages are joined in the order
they were sent.

Every call is preceded by the budget check and followed by a row in ``ai_usage`` (committed at
once: money spent is logged even when a later page fails). Images and transcripts live in
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

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import budget, fallback, pricing
from app.ai.client import AiUnavailable, TranscribeClient, Transcription
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
    """One submit's calls: the budget check before, the usage row after."""

    def __init__(
        self,
        db: Session,
        client: TranscribeClient,
        settings: Settings,
        user: User,
        now: datetime,
    ) -> None:
        self.db, self.client, self.settings, self.user, self.now = db, client, settings, user, now
        self.agency_id = user.agency_id

    def transcribe(self, image: bytes, media_type: str, model: str, outcome: str) -> Transcription:
        """The transcription, or ``ScreenshotError``. ``outcome`` is the usage row's on success."""
        db, user = self.db, self.user
        if not budget.within_budget(
            db, self.agency_id, self.now, model, self.settings.ai_monthly_budget_usd
        ):
            budget.record(
                db, user, now=self.now, model=model, input_tokens=0, output_tokens=0,
                outcome=budget.OUTCOME_REFUSED,
            )  # fmt: skip
            db.commit()
            raise ScreenshotError(budget.BUDGET_MESSAGE, 403)
        try:
            result = self.client.transcribe(image, media_type, model)
        except AiUnavailable as exc:
            kind = exc.kind
        except Exception as exc:  # noqa: BLE001 - a client bug must not become a 500
            kind = type(exc).__name__  # the class only: a message could quote the content
        else:
            self._record_paid_call(model, result, outcome)
            return result
        log.warning("ai call failed", extra={"kind": kind, "model": model})
        # a timeout may still have been billed: count the estimate; other failures were refused
        cost = pricing.estimate_micro_usd(model) if kind == "timeout" else 0
        budget.record(
            db, user, now=self.now, model=model, input_tokens=0, output_tokens=0,
            outcome=budget.OUTCOME_ERROR, cost_micro_usd=cost,
        )  # fmt: skip
        db.commit()
        raise ScreenshotError(API_DOWN, 503)

    def _record_paid_call(self, model: str, result: Transcription, outcome: str) -> None:
        db = self.db
        try:
            cost = budget.record(
                db, self.user, now=self.now, model=model, input_tokens=result.input_tokens,
                output_tokens=result.output_tokens, outcome=outcome,
            )  # fmt: skip
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            cost = pricing.cost_micro_usd(model, result.input_tokens, result.output_tokens)
            # the call was paid and is not in ai_usage: the log is the only trace of it
            log.error(
                "ai usage row lost after a paid call",
                exc_info=True,
                extra={"model": model, "cost_micro_usd": cost, "agency_id": self.agency_id},
            )
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
