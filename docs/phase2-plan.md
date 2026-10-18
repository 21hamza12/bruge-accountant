# Phase 2 plan: logins, web upload, first deploy (Render)

Status: **approved 2026-10-18 with the recommended defaults below**. Produced by the ECC planner.

User decisions: host on **Render** (Frankfurt, Docker web service plus managed Postgres);
**manager-created accounts** (phone plus password, manager-issued temporary passwords);
**any staff member can upload**. No price chat, AI, voice or reports yet.

## Key design decisions

1. **Parsing runs in a sandboxed subprocess inside the web service**, not a Render background worker.
   - The upload is a spawned child with `RLIMIT_AS`/`RLIMIT_CPU` (Linux) and a 30 s timeout; only the
     `ParsedReport` comes back, and all DB writes happen in the parent.
   - At most `PARSE_CONCURRENCY` (default 2) parses at once per process; one that finds every slot
     taken waits up to `PARSE_QUEUE_WAIT_S` (5 s) and then gets a friendly 503 "busy, retry".
   - Saves a second $7+/month service for one upload a day. A queue can replace it later behind
     `parse_in_sandbox()`.
2. **Server-side sessions in Postgres** (the cookie holds a random token; the DB stores its sha256).
   - Revocation is instant on a password reset or deactivation.
   - Cookie `__Host-session`: HttpOnly, Secure, SameSite=Lax. 12 h idle / 7 d absolute.
   - The token is rotated at login and on password change.
3. **CSRF**:
   - Logged-in forms use a synchronizer token on the session row (htmx sends it via the
     `X-CSRF-Token` header).
   - The login form uses a double-submit cookie.
   - Origin/Referer are checked against the allowlist.
4. **Row-level security** on every agency-scoped table (migration 0003, built in PR 2):
   - **0003 is a deliberate *contract* migration, not expand-only.** Once it has run, a release
     that does no tenant binding (the previous one) sees zero rows and cannot insert. So it
     ships in the same release as the tenant-binding code (PR 2) and can never be deployed
     ahead of it. Rolling back past it means `alembic downgrade 0002` *before* redeploying the
     previous release (runbook section 5; `tests/test_rollback_invariant.py` proves it).
   - What RLS buys: it contains **IDOR and forgotten-filter bugs** (a query that forgot
     `WHERE agency_id = ...` still sees one agency). It does **not** contain SQL injection: a
     query an attacker controls can run `set_config` and pick its own tenant. The SQL injection
     control is ORM-only code plus the AST code guard, which also bans `set_config` outside
     `app/tenancy.py`.
   - ENABLE **and FORCE** row-level security, so the policies also bind the table owner.
   - Policy `agency_id = app_current_agency_id()` (`id = …` on `agencies`), set per transaction
     with `set_config('app.agency_id', …, true)`. An unset tenant means zero rows (fails closed).
   - Login, session and CLI lookups use narrow SELECT policies keyed on an exact phone, token
     hash or agency slug. `audit_events` is append-only; a row with no agency (failed login for
     an unknown phone) can be inserted by anyone and is never readable.
   - Three narrow DELETE-only purge policies let `purge-expired` (any session, no tenant bound)
     remove expired or revoked sessions, used or expired temporary passwords, and audit events
     older than 400 days. Nothing else can be deleted from `audit_events`.
   - The migration refuses an `APP_DB_ROLE` that is a superuser or has BYPASSRLS, and the web app
     refuses to start in production connected as one (both would skip every policy).
   - No admin bypass setting exists: even the CLI binds a tenant (a new agency gets its id from
     the sequence first, and is bound to it before the INSERT).
   - **Two deployment modes, same isolation code:**
     - *Separate role* (preferred): migrations and admin commands run as the owner
       (`MIGRATION_DATABASE_URL`); the web app connects as a non-owner role without BYPASSRLS
       (`DATABASE_URL`). Migration 0003 grants that role (named by `APP_DB_ROLE`) only what it
       needs: no DELETE on tickets or uploads, no CREATE, read-only `agencies`.
     - *Single role* (when the host cannot `CREATE ROLE`): `APP_DB_ROLE` is unset, nothing is
       granted, and the app connects as the owner. FORCE RLS still isolates tenants. Residual
       risk: the owner can run `ALTER TABLE … NO FORCE ROW LEVEL SECURITY` or `DISABLE`, so an
       attacker with code execution on the server defeats it, and the owner keeps DELETE and DDL
       rights. Residual risk in this mode, listed plainly: the owner can also `TRUNCATE
       audit_events` and `ALTER ... NO FORCE`. Shipping audit events out of the database is
       PR 5. IDOR and forgotten-filter bugs in the application remain contained; SQL injection
       is not an RLS matter (see above).
5. **Tenant binding** lives in the SQLAlchemy session (an `after_begin` listener re-applies it in
   every transaction). It does nothing on SQLite.
6. **Status codes:** another agency's record returns 404; a role violation in your own agency
   returns 403.
7. **Alerting (free tier):**
   - Sentry EU (PII scrubbed) for 5xx.
   - An uptime monitor on `/healthz`.
   - Render deploy-failure emails.
   - An `audit_events` table plus JSON logs.

## PRs (each step = one test-first commit)

### PR 1: Web skeleton and first deploy (no tenant data exposed)
1. Settings (pydantic-settings).
   - `postgresql://` → `postgresql+psycopg://` normalization; psycopg moves to the main dependencies.
   - Production validators: no `*` in CORS, secure cookies, explicit hosts.
2. FastAPI app factory (docs/openapi disabled) and `/healthz` (`select(1)`, version = git commit).
3. Custom error pages 400/401/403/404/429/500 plus an offline page.
   - No tracebacks, framework names or server header; each page shows a random reference code.
4. Security headers:
   - Strict CSP, HSTS (production), nosniff, Referrer-Policy and Permissions-Policy.
   - Explicit CORS allowlist and TrustedHost.
   - Code-guard tests banning `text(`, `|safe` and `Markup(` in `app/`.
5. Structured JSON logging with a request id and secret redaction; uvicorn's access log is off.
6. i18n/RTL/PWA groundwork:
   - Jinja i18n with null translations, `<html lang dir>`, logical CSS properties.
   - htmx vendored with a CSP-safe config, plus a manifest and a service worker that only caches
     static files and the offline page.
7. Dockerfile (python 3.12-slim, non-root, no tests in the image) and `render.yaml`:
   - Frankfurt, Starter web, Basic-256mb Postgres, `preDeployCommand: init-db`, secrets `sync:false`.
   - A CI job builds the image and curls `/healthz`. Migration 0001 is frozen from here on.
8. Ops spike and first deploy:
   - Verify Render Postgres allows `CREATE ROLE`, how X-Forwarded-For arrives, and rollback
     behaviour.
   - Set up the uptime monitor and write `docs/runbook.md`.

### PR 2: Tenancy and auth schema (database only)
9. Two-role Postgres test setup (owner plus `bruge_app`); the CI Postgres job runs the full suite.
10. Migration 0002:
    - `users` (phone E.164, role manager|staff), `staff_sign_links`, `user_sessions`,
      `temporary_passwords`, `audit_events`, `rate_limit_buckets`.
    - `uploads` gains inserted/updated counts and the uploader.
11. Tenant binding (`bind_tenant`, `lookup_scope`, after_begin listener).
12. Migration 0003: RLS policies and grants for the app role (a contract migration, decision 4).
    - The app role cannot DELETE tickets or uploads.
    - Purge policies, the privileged-role guard and the `grant-app-role` / `purge-expired`
      commands.
    - A guard test fails CI if any future agency-scoped table lacks RLS.
13. Repository and CLI working under RLS; all Phase 1 tests still green on both engines.
14. Phone normalization (+222) and passwords:
    - Argon2id (OWASP parameters), dummy-hash timing protection.
    - Minimum 10 characters, not equal to the phone; temporary-password generator.
15. CLI `bootstrap-agency` (first agency plus manager; the password is read via getpass and is
    never an argument).

### PR 3: Login and account management (security-reviewer blocking)
16. Postgres-backed rate limiter (atomic upsert), shared across instances:
    - login: 5 **failures** per 15 min per phone, 100 failures per 15 min per IP (a coarse
      flood guard: an office shares one NAT address); uploads: 30 per hour per agency and 10 per hour per user.
    - Failures only: an attempt is counted before it is checked and given back on success
      (the phone bucket is cleared, the IP hit refunded), so five successful logins in a row stay
      allowed and the 6th failure is the first 429. A password change counts wrong current
      passwords only.
    - Anonymous floods write no database rows: `csrf_failed` goes to the JSON log only, and
      `rate_limited` is one audit row per bucket window.
17. Server-side sessions: create, lookup, touch, revoke, revoke-all.
18. CSRF middleware and tests.
19. Login and logout:
    - One generic error; unknown phone goes through the dummy hash; masked phones in logs; 429 page.
    - Audit events.
20. Auth dependencies: 303 to login, htmx 401, staff on manager pages get 403 (audited).
21. Forced password change (rotates the token, revokes other sessions).
22. Manager staff admin:
    - List, create, link and unlink Amadeus signs, deactivate.
    - The last manager can't be removed.
23. Temporary password:
    - Argon2-hashed, 30 min, atomic single use.
    - Shown once in the POST response with no-store; never in a URL, log or the DB as plaintext.

### PR 4: Uploads and the questions page (security-reviewer blocking)
24. Shared `app/ingest.py` (web and CLI use one path).
    - Filename sanitizing (basename, NFC, no control/bidi characters, `.xlsx`) and a zip magic check.
25. Parse sandbox (timeout, memory cap, crash → ParseError; the fixture's peak memory is measured).
26. Request body limit (413 before reading; streamed bodies are counted).
    - **Already built in PR 3** as `app/web/bodylimit.py`: raw ASGI middleware, placed first among
      the innermost layers (just inside the error boundary, before any caller `extra_middleware`),
      so no dependency parses a byte past `MAX_REQUEST_BYTES` (default 10 MB + 64 KB). A
      declared `Content-Length` over the limit is the custom 413 page before the body is read;
      streamed bodies are counted and cut off (a `BodyTooLarge` HTTP 413 exception, which FastAPI
      lets through where it would turn any other body-parsing error into a 400). The anonymous
      login form is bounded the same way. PR 4 only has to use it.
    - **CSRF for uploads:** `csrf_protect` reads the token from the `X-CSRF-Token` header first
      and parses the body only when there is no header. The upload route must send the token in
      that header (a `fetch`/htmx request does; a plain `<form>` cannot) so the check never
      spools the file to a temporary file. A test pins that the body is not parsed.
27. Upload page and POST:
    - CSRF and the rate limit; agency from the session only (a forged field is ignored);
      303 to the result page.
28. Upload result page plus recent uploads; XSS test with `<script>` in a passenger name; another
    agency's upload returns 404.
29. Questions page (`/questions?date=`):
    - Managers see all signs; staff see their own; unlinked signs are flagged.
    - It shows the fixture's numbers.
30. Hot-query EXPLAIN tests under RLS (sessions, phone lookup, questions, uploads, rate limits).
31. `seed-demo` (two demo agencies) and an HTTP end-to-end suite against the Docker image covering:
    - login, staff creation, temporary password and forced change
    - upload and re-upload, the questions page
    - cross-agency 404, staff-to-manager 403, missing CSRF 403
    - the 6th bad login → 429, an 11 MB upload → 413, error pages and headers

#### PR 4 as built (measured results and deviations)

- **Sandbox design (step 25), after the review fixes.** The child is
  `python -I -B -c <bootstrap> <memory_mb> <cpu_s> <target>`, started with `subprocess.Popen` and a
  MINIMAL environment (`PATH`, `SYSTEMROOT`/`SYSTEMDRIVE` on Windows, `LANG` on Linux, plus
  `PYTHON*` isolation variables; `-I` makes it ignore `PYTHON*`, the user site and the current
  directory). It cannot read `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `SENTRY_DSN` or any other
  variable of the web process (tested). The workbook goes to its stdin, the answer comes back on
  its stdout (`app/parsing/worker.py`); there is no `multiprocessing` and no pickling.
  - *One monotonic deadline.* A reader thread collects stdout (at most `MAX_MESSAGE_BYTES`) and the
    parent waits for it with the time that is left. At the deadline, or when the answer is too big,
    the child is killed (on Windows its whole process tree with `taskkill /T`: the venv launcher
    starts the real interpreter as a child), which also ends a half-sent message. The original
    design (`poll(timeout)` then `recv_bytes()`) could block forever on a partial message; a test
    now sends a partial message and then hangs, and the parse fails near the timeout with the slot
    free. If `Popen` itself raises there is no child to join or kill and the original error
    propagates (the slot is released by a `finally`). `Popen` has no `close()`: `wait()` reaps it.
  - *Linux limits*, applied in the child just before parsing: `RLIMIT_AS`, `RLIMIT_CPU`,
    `RLIMIT_NPROC` (0: no new process) and `RLIMIT_FSIZE` (0: no file writes), plus no core file.
  - *Slots.* `PARSE_CONCURRENCY` (default 2) parses at once; a parse that finds none free waits up
    to `PARSE_QUEUE_WAIT_S` (default 5) and then raises `SandboxBusy` (the 503). Each child needs
    about 31-49 MB resident (below), so 2 children plus the web process fit a 512 MB instance with
    room to spare; size `PARSE_CONCURRENCY` as roughly `(instance memory - 250 MB) / 80 MB`.
  - *Strict reading of the answer* (`app/parsing/wire.py`): exact types; string lengths from the
    column sizes; cost a plain decimal string with |cost| < 10^12 and at most two decimals (no
    NaN/Infinity); `source_row` 1..10^7; at most 50 000 tickets and 1 000 warnings; known flags
    only; no control, format, surrogate or private-use characters anywhere. Anything else is a
    `ParseError`. The parser itself strips such characters from every text cell and caps passenger
    names at 200 characters (flagged `field_too_long`).
- **Sandbox memory (step 25).** Peak resident memory of the spawned child, measured on Windows
  (Python 3.14, `PeakWorkingSetSize`, read inside the child around `parse_report`):
  about **31 MB** for the 48-ticket synthetic fixture, about **49 MB** for a synthetic 20,000-row
  report (0.7 MB file). The interpreter plus openpyxl is most of it (about 31 MB before parsing).
  A 50,000-row workbook cannot be built as a valid test file: it expands past the 30 MB
  uncompressed limit, so 20,000 rows is the largest realistic measurement. `RLIMIT_AS` limits
  *virtual* address space, which is several times the resident size, so the default
  `PARSE_MEMORY_MB` is 768 (not 512). The CI `docker-e2e` job now runs the app container with
  `--memory 512m` (no swap), runs real uploads and one sandbox parse inside it under the real
  Linux limits, and prints the child's peak RSS and the container's cgroup `memory.peak`; see the
  numbers recorded below.
- **Upload route order (step 27).** `POST /uploads` declares no `UploadFile`: FastAPI parses a
  declared upload before it runs dependencies, so an anonymous, CSRF-less or rate-limited POST
  would still spool up to 10 MB. The route runs, in order: login (session), Origin, the
  `X-CSRF-Token` header if present, the rate limits (10/hour/user then 30/hour/agency), and only
  then `request.form(max_files=1, max_fields=5, max_part_size=64 KB)`; without a header the form's
  `csrf_token` field is checked before the file is used (a wrong token refunds the hit). A spy test
  proves that anonymous, bad-Origin, bad-header-token and rate-limited POSTs never construct
  `MultiPartParser`. The plain HTML form (no JavaScript) still works. The quota is refunded on the
  503, a database error, any unexpected error and a wrong form token; a bad file still counts. A
  database error during the import is a friendly 500 audited as `upload_failed` / `db_error`, and a
  failing success audit is logged without turning a committed upload into a 500.
- **Upload visibility (decision 5).** Managers see every upload with its warnings. Staff see only
  uploads they made, with counts and dates but no warnings (warnings quote ticket numbers and name
  fragments); a colleague's upload id is a 404, like a missing one. Uploads without an uploader
  (CLI imports) are manager-only.
- **Re-upload under another date.** Tickets that already exist keep the day they were first stored
  on. The result page and the `import` command now show the day(s) the tickets of that upload
  really belong to and warn when they differ from the upload's own date.
- **Upload CSRF.** The decision above (header only, so the check never spools the file) was
  relaxed: the upload form is a plain multipart form with the hidden `csrf_token` field, so it
  works without JavaScript. The header still works first and is checked before the body is read.
  The body limit runs before any parsing, so a form is never bigger than `MAX_REQUEST_BYTES`.
- **Hot-query plans (step 30): no migration 0005.** All queries already have their index
  (`tests/test_query_plans.py`, run as the app role, under row-level security, on tables padded to
  realistic volume and analysed), including the staff views of uploads and the days of one upload's
  tickets. Notes from writing it: on a two-row table every index path costs
  the same, so the test pads the tables first. For "recent uploads" the planner walks the primary
  key backwards for an agency that owns nearly every row and uses an agency-leading index for a
  small agency; the unique `(agency_id, id)` constraint covers both, so no new index is needed.
- **`seed-demo` (step 31).** `--fixture` is loaded into `demo-b` only, so the two agencies hold
  overlapping ticket numbers while `demo-a` stays empty for the person trying the upload and for
  the end-to-end journey that checks 48 inserted, then 0 inserted and 48 updated. It pre-checks
  both slugs and all four phones (naming each collision), creates everything in ONE transaction,
  refuses `ENV=production` always, and refuses a database that is neither SQLite nor local unless
  `--i-know-this-is-not-production` is given.
- **End-to-end in CI** runs the app container as a separate non-superuser role with
  `COOKIE_SECURE=false` over plain http (cookies cannot be `Secure` there). Production mode and
  HSTS stay covered by the existing `docker` job. The suite is rerunnable: it creates a fresh staff
  member per run for the uploads (a fresh per-user budget); the agency budget of 30 an hour allows
  about 10 runs an hour against one seeded database. The full 11 MB body test was dropped (client
  side TCP races); the raw-socket test and the in-process streamed test cover it.

### PR 5: Production readiness, go-live, rollback drill
32. Sentry with scrubbing (no cookies, headers, bodies or passenger names).
33. Alert rules (Sentry, uptime, Render deploy failures), each triggered once and recorded.
34. Production cutover:
    - If `CREATE ROLE` works: create the `bruge_app` role in Render Shell *before* the release that
      runs migration 0003, set `APP_DB_ROLE` and point `DATABASE_URL` at it. Otherwise stay in
      single-role mode (runbook section 3).
    - Run `bootstrap-agency`, then smoke-test.
35. Rollback drill, plus production `EXPLAIN ANALYZE`.
    - **Both releases of the drill pair must include 0003** (N+1 → N → N+1, where N is already
      the tenant-binding release): a rollback to a release without the tenant-binding code is
      not possible by redeploy alone.
    - The one rollback that crosses 0003 is drilled separately and includes the downgrade:
      `alembic downgrade 0002`, then redeploy the pre-0003 release, then upgrade again.
    - Recorded in `docs/release-checklist-phase2.md`.
36. Docs: decisions, roles, the expand/contract migration rule, and a PR checkbox.
    - The rule: a migration is **expand-only** (additions, all nullable or new) so the previous
      release keeps working against it and a rollback is a redeploy. A **contract** migration
      (anything the previous release cannot work against: 0003's row-level security) is allowed
      only when it ships in the same release as the code that needs it, is labelled as such in
      its docstring, and its rollback is `alembic downgrade <previous revision>` before
      redeploying the previous release. A contract migration is never deployed ahead of its code.

## Release checklist mapping

| # | Gate | Evidence |
|---|---|---|
| 1 | Tenancy (IDOR) | RLS tests, cross-agency 404 web tests, e2e journey 5 |
| 2 | Reset expiry 30 min | Temp-password tests (30:01 expiry, second use, no plaintext anywhere) |
| 3 | Sanitization | ORM-only code guards, Jinja autoescape, XSS test, CSP |
| 4 | Origin restriction | CORS allowlist validators, CSRF and Origin tests |
| 5 | Rate limiting | Postgres limiter shared by two engines, e2e 429 |
| 6 | Error screens | Error page tests, e2e 404; the offline page is checked manually (unverified until done) |
| 7 | Indexes | EXPLAIN tests in CI plus production EXPLAIN ANALYZE |
| 8 | Logging and alerting | Log redaction tests, audit table, each alert triggered once |
| 9 | Rollback | Recorded Render rollback drill. Render offers zero-downtime deploys, not true blue/green |

## Decisions with recommended defaults

1. Phone numbers unique across the whole platform, so login is phone plus password with no agency
   field.
2. The owner DB URL sits in the web service environment so `preDeployCommand` can migrate.
   Accepted for the pilot and documented. With a separate app role, `DATABASE_URL` is the app
   role and `MIGRATION_DATABASE_URL` the owner, which only `init-db` and the admin CLI read (the
   admin commands `add-agency` and `bootstrap-agency` need it: run them with
   `DATABASE_URL="$MIGRATION_DATABASE_URL"`; `grant-app-role` too). In single-role mode both URLs are the owner, so the
   web process holds owner rights and relies on FORCE RLS alone (see decision 4).
3. Postgres version: the newest Render offers in Frankfurt, with CI set to match; avoid
   PG18-only features.
4. UI source language: **French** (Arabic RTL later).
5. Questions page: managers see all staff signs; staff see only their own.
6. Managers can create other managers; the last active manager can't be removed.
7. Passwords at least 10 characters. Staff signs: uppercase alphanumeric, 2–32 characters.
8. Sentry EU as error processor, with passenger names scrubbed.
9. Render Hobby workspace ($0); upgrade to Professional only if logs need longer retention.
10. Alerts by email; the onrender.com address is fine for the pilot.
11. Manager resets: a manager may issue temporary passwords for **staff only**, never for another
    manager or themselves (decided: "No, only staff"). The platform owner resets a locked-out
    manager with `python -m app reset-password --phone <phone>` (runbook).

## Risks
- Render Postgres may not allow `CREATE ROLE` (checked in step 8). PR 2 no longer depends on it:
  without a second role the app runs in single-role mode with FORCE RLS (decision 4).
- A role created after migration 0003 has run gets no grants (the migration only grants when
  `APP_DB_ROLE` is set while it runs). Run `python -m app grant-app-role <role>` as the owner: it
  validates the name, refuses a superuser or BYPASSRLS role and is safe to repeat.
- The memory cap needs tuning on the 512 MB instance.
- X-Forwarded-For spoofing (use the rightmost-hop IP and also limit per phone).
- htmx versus a strict CSP.
- A future table could miss RLS (guard test).
- A rollback runs old code against the new schema. Expand-only migrations make that safe; 0003
  is the deliberate exception and needs `alembic downgrade 0002` first (decision 4, runbook
  section 5).
- Env vars are not rolled back (safe defaults).

## Cost (Render Frankfurt, check current prices)
- Web Starter: about $7
- Postgres Basic-256mb: about $6, plus storage
- Sentry, uptime monitor and Hobby workspace: free
- Purge cron job (`bruge-purge`, hourly): billed for its few seconds of run time, with a monthly
  minimum for its plan; check the current figure.
- **Total about $13–15/month.** The free Postgres plan is not an option: free databases expire
  after 30 days.
