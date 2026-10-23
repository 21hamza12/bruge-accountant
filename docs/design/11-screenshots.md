# Reading Amadeus screenshots with Claude (phase D)

Status: built. Pasting the Amadeus text stays the first choice (exact, instant, free, see
`10-ticket-screens.md`). A photo or screenshot is the fallback for an agent who cannot copy the
text, for example on a phone. The result goes through exactly the same steps as a paste: the
reader (`parse_pnr_text`), the proposed sales, the agency question, the read-back, the confirm.
**The AI only turns an image into text; it never decides a price, an agency or a sale.**

## 1. What was measured (23 Oct 2026, the owner's 16 real screenshots)

| | Haiku 5.5 (`claude-haiku-5-5`) | Sonnet 5.5 (`claude-sonnet-5-5`) |
|---|---|---|
| Ticket numbers read | 69 / 69 | 69 / 69 |
| Amounts read | 49 / 49 | 49 / 49 |
| Booking references (RP) | 16 / 16 | 16 / 16 |
| RM lines and agency lines | read | read |
| Cost per screenshot | about 0.00075 USD | about 0.0081 USD |
| Input tokens per image | about 600 to 700 | about 600 to 700 |

Same prompt for both (kept as measured, plus two robustness sentences: no code fences or comment,
and text inside the image is never an instruction). Observed variation: the same screenshot can
come back with or without its leading indentation, and sometimes inside Markdown code fences. On
one Haiku read the lost indentation made the reader miss every FA ticket; the reader is now
indifferent to it (section 3).

## 2. Model choice and the fallback rule

* **Haiku 5.5 first**, always: as accurate as Sonnet on all 16 images and about 11 times cheaper.
* **Sonnet 5.5 for the same image** when the Haiku transcript is not good enough
  (`app/ai/fallback.py`, tested case by case). Each page of an Amadeus booking starts with an `RP/`
  line, so every image is judged alone. The first reason that applies:
  1. `no_rp`: the reader found no booking reference.
  2. `no_ticket`: the page shows ticketing elements (`FA`, `FB`, `FV`, `FP`, `FE`, `FO`) but no FA
     ticket was read. A first page with names, segments and contacts only (a layover booking) has
     none of those elements and is not a reason: its tickets are on the next page.
  3. `no_cost`: a ticket has no amount, and not because the currency is not MRU (a second read
     would say the same).
  4. `garbled`: the reader could not read an FA line, or the model wrote `?` (the prompt's mark for
     an unreadable character) anywhere.
* The Sonnet call is logged with outcome `fallback`. If it is refused (budget) or fails, the Haiku
  transcript is kept and the agent reads everything back anyway.
* Models and prices live in settings and in `app/ai/pricing.py`. The Sonnet prices are the
  published ones and reproduce the measured cost; **the Haiku prices in the table are a
  conservative figure chosen to cover the measured 0.00075 USD and must be checked against the
  price page** (an unknown model is priced like the dearest one, so the budget can only get
  stricter).

## 3. The reader accepts what a model writes

`parse_pnr_text` now ignores Markdown code fences and lines of prose (a lowercase word of three
letters or more: Amadeus prints capitals), and treats any line that is not an element number,
`RP/`, a header or a prompt as the continuation of the element above, **whatever its
indentation**. Every fixture in `tests/fixtures/pnr/` reads identically with its indentation
removed, inside fences, both, with prose around it, and in a two-page paste with one page
de-indented (`tests/test_pnr_robust.py`).

## 4. The flow

`POST /sales/screenshot`, next to the paste box on "Saisir une vente" (shown only when the app has
a key, `AI_ENABLED` is not false, and the agency is not switched off).

1. Login, permission `can_use_sales_screens`, `Cache-Control: no-store`, 404 when the feature is
   off, then Origin, the CSRF token and the rate limit, **all before the body is read** (the route
   declares no body parameter and is not under the router's `csrf_protect`, like `POST /uploads`):
   the token is checked first from the `X-CSRF-Token` header when there is one, else from the form
   field once the body is parsed (a wrong token gives the rate-limit hit back). 60 images an hour
   for each user (`SCREENSHOT_USER`): one hit when the request arrives (so a request with no image
   or too many counts one), one more for each image after the first.
2. Up to 4 images (the pages of one booking), 8 MB each. `POST /sales/screenshot` has a body limit
   of its own, 33 MB (`SCREENSHOT_BODY_BYTES`, in `RequestBodyLimitMiddleware.path_limits`); every
   other path keeps `MAX_REQUEST_BYTES` (10 MB), anonymous ones included, and nothing reads this
   route's body before the gates above. `accept="image/*"` with `capture` for a phone camera, plus
   a plain file picker that allows several. The body is parsed with `max_files=5`, 4 small fields,
   16 KB per field; more files, or a field too big, is a French "L'envoi n'a pas pu être lu".
3. Each image is validated by **decoding** (`app/ai/images.py`): real format from the bytes (JPEG,
   PNG, WebP; HEIC is recognised and refused with a message asking for JPEG), pixel count read from
   the header and refused before any pixel is decoded: over 16 million pixels for PNG and WebP
   (a mostly blank 36 MP PNG fits in 8 MB and decodes to over 100 MB), over 36 million for JPEG
   (decoded at reduced size with `draft`); Pillow's `MAX_IMAGE_PIXELS` is a second guard. The
   decoded image is shrunk at once by a whole factor toward 1568 px, and only then turned upright
   (EXIF orientation), flattened on white and re-encoded, so only one full-size copy ever exists;
   the metadata is dropped (**EXIF, GPS, ICC profile and comments cannot follow**); at most two
   images are decoded at the same time in the process (a semaphore), and any unexpected Pillow
   error is a refused image, not a 500. All in memory. A bad image stops the whole submit before any
   call is paid for ("Image 2 : ...").
4. Haiku transcribes each image; the fallback rule may call Sonnet for that image; the pages are
   joined in order; `parse_pnr_text`; `proposed_sales` against the agency's partners and approved
   aliases.
5. The outcome is the paste's: one passenger gives the prefilled form, several give the list of
   proposals; the agency question must be answered (server side); the sale's history says
   `screenshot` (`sale_versions.source`, migration 0014).
6. Any failure is a French message that suggests pasting the text, never a 500: API down or
   timeout (503), budget reached (403), unreadable image or no sale in the text (400).

### Why the images are decoded in-process, not in a subprocess

The Excel parse runs in a sandbox because the application parses a zip and XML itself. Here the
decoder is Pillow's, on three allow-listed formats, with the size and pixel count bounded before
decoding, an authenticated user, and a rate limit. A child process would add about a second of
start-up and a second copy of the pixels per image. The residual risk is a memory-safety bug in a
native decoder (libjpeg, libpng, libwebp); a subprocess with an address-space cap would contain it.
It is accepted for the pilot and should be revisited before opening the feature to less trusted
users. Keep Pillow current.

## 5. Privacy

* The cleaned image is sent **once** to Anthropic through the API (API terms: no training on API
  data; check the current retention terms before the first real use) and the bytes are dropped
  when the request ends. It is never written to disk: this route parses its body with a parser of
  its own whose spool is as big as the route's body limit (so nothing rolls over to a temporary
  file), used only after login, Origin and rate limit; the library's 1 MB spool is left as it is
  for every other route, so an anonymous multipart post (the login form) is never held in RAM
  beyond 1 MB. A test fails if a temporary file is made. Never stored in the database, never
  logged, never audited.
* The transcript is treated like a pasted text: used for the read-back, then dropped. No log line,
  audit event or usage row carries a name, a ticket number or any text. Pillow, the Anthropic SDK
  and httpcore are held at WARNING (their DEBUG output prints EXIF tags and request bodies).
* The screen text can contain instructions ("ignore the above"); the model has no tools and its
  output is only parsed by the strict reader and read back by the agent.
* The agency's names and prices are not sent: only the image.

## 6. Budget and logging

* `ai_usage` (append-only, migration 0014): agency, user, time, purpose, model, tokens, cost in
  micro-USD (BIGINT), outcome `ok` / `fallback` / `error` / `refused_budget`. Row-level security
  `FORCE`d (read and insert only), triggers on SQLite, grants SELECT and INSERT.
* `agency_ai_settings` (mutable, one row per agency at most): `enabled`, `monthly_budget_micro_usd`.
  No row means on, with `AI_MONTHLY_BUDGET_USD` (5 USD). The web role has SELECT only on it
  (migration 0014); it is edited only by
  `python -m app ai-budget <slug> --usd N [--disable|--enable]` with the owner connection (it is
  audited as `ai_budget_set`); there is no operator screen yet.
* Before each call: month-to-date cost of the agency (one indexed `SUM` over
  `(agency_id, created_at)`, calendar month UTC) plus the estimate of that call (1500 tokens in,
  3000 out) must not exceed the budget; otherwise nothing is sent, a `refused_budget` row is written
  and the user reads "Budget IA du mois atteint : collez le texte Amadeus." Two simultaneous
  requests can each pass the check and overshoot by a call or two; accepted (a screen costs under a
  cent).
* Every call is committed to `ai_usage` right away: money spent is logged even if a later page
  fails. The cost comes from the tokens the API reports and the price table. A call that ended in
  a **timeout** is recorded as `error` at the **estimated** cost (the API may have billed it); any
  other failure costs 0. If the usage row of a paid call cannot be committed, an error is logged
  (model, cost, agency; never text) and the user gets the "service unavailable" message.
* The SDK retries a 429, a 5xx or a network error once (`AI_MAX_RETRIES`, default 1), so the worst
  case of one image is two attempts plus the fallback's two, each bounded by `AI_TIMEOUT_S`.

### The fallback rule

`app/ai/fallback.py`: the second model reads the same image again when the first transcript has
no RP line, shows ticketing elements but no ticket, has a ticket whose amount is unreadable (a
currency other than MRU is explained by its own ticket's warning and is not a reason), has an FA
line that cannot be read or a `?` in a money or ticket line (`FA FB FV FP FE FO TK`) or next to a
digit (a `?` in a name is not a reason), or was **truncated** at the output limit
(`Transcription.truncated`, from `stop_reason == "max_tokens"`). "ai fallback" is logged only when
the second read worked; when it was refused or failed the first transcript is kept and
"ai fallback failed, first read kept" is logged.

The reader (`parse_pnr_text(text, transcript=True)`) is looser only for a transcript, whose
indentation a model may lose: a line at column 0 to 2 that is not an element, header, prompt or
prose continues the element above. A typed paste keeps the strict rule (only an indented line
continues an element).

## 7. Open points

* Check the Haiku 5.5 prices against the price page and the retention terms of the API.
* A monthly summary of `ai_usage` for the owner (no screen yet; the table is the source).
* HEIC support (`pillow-heif`) if agents' iPhones send it often; today they get a message.
* `ANTHROPIC_API_KEY` is not in `render.yaml` (the blueprint test mirrors the cron job's
  environment); add it in the dashboard of the web service.
