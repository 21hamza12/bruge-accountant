# Phase 2 plan: logins, web upload, first deploy (Render)

Status: **approved 2026-10-18 with the recommended defaults below**. Produced by the ECC planner.

User decisions: host on **Render** (Frankfurt, Docker web service plus managed Postgres);
**manager-created accounts** (phone plus password, manager-issued temporary passwords);
**any staff member can upload**. No price chat, AI, voice or reports yet.

## Key design decisions

1. **Parsing runs in a sandboxed subprocess inside the web service**, not a Render background worker.
   - The upload is a spawned child with `RLIMIT_AS`/`RLIMIT_CPU` (Linux) and a 30 s timeout; only the
     `ParsedReport` comes back, and all DB writes happen in the parent.
   - One parse at a time per process; a second concurrent upload gets a friendly 503 "busy, retry".
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
   - ENABLE **and FORCE** row-level security, so the policies also bind the table owner.
   - Policy `agency_id = app_current_agency_id()` (`id = …` on `agencies`), set per transaction
     with `set_config('app.agency_id', …, true)`. An unset tenant means zero rows (fails closed).
   - Login, session and CLI lookups use narrow SELECT policies keyed on an exact phone, token
     hash or agency slug. `audit_events` is append-only; a row with no agency (failed login for
     an unknown phone) can be inserted by anyone and is never readable.
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
       rights. SQL injection and IDOR bugs in the application remain contained.
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
12. Migration 0003: RLS policies and grants for the app role.
    - The app role cannot DELETE tickets or uploads.
    - A guard test fails CI if any future agency-scoped table lacks RLS.
13. Repository and CLI working under RLS; all Phase 1 tests still green on both engines.
14. Phone normalization (+222) and passwords:
    - Argon2id (OWASP parameters), dummy-hash timing protection.
    - Minimum 10 characters, not equal to the phone; temporary-password generator.
15. CLI `bootstrap-agency` (first agency plus manager; the password is read via getpass and is
    never an argument).

### PR 3: Login and account management (security-reviewer blocking)
16. Postgres-backed rate limiter (atomic upsert), shared across instances:
    - login: 5 per 15 min per phone, 20 per 15 min per IP; uploads: 30 per hour per agency.
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

### PR 5: Production readiness, go-live, rollback drill
32. Sentry with scrubbing (no cookies, headers, bodies or passenger names).
33. Alert rules (Sentry, uptime, Render deploy failures), each triggered once and recorded.
34. Production cutover:
    - If `CREATE ROLE` works: create the `bruge_app` role in Render Shell *before* the release that
      runs migration 0003, set `APP_DB_ROLE` and point `DATABASE_URL` at it. Otherwise stay in
      single-role mode (runbook section 3).
    - Run `bootstrap-agency`, then smoke-test.
35. Rollback drill (N+1 → N → N+1) with expand-only migrations, plus production `EXPLAIN ANALYZE`.
    - Recorded in `docs/release-checklist-phase2.md`.
36. Docs: decisions, roles, the expand/contract migration rule, and a PR checkbox.

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
   `DATABASE_URL="$MIGRATION_DATABASE_URL"`). In single-role mode both URLs are the owner, so the
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

## Risks
- Render Postgres may not allow `CREATE ROLE` (checked in step 8). PR 2 no longer depends on it:
  without a second role the app runs in single-role mode with FORCE RLS (decision 4).
- A role created after migration 0003 has run gets no grants (the migration only grants when
  `APP_DB_ROLE` is set while it runs). Create the role first, or add a grant-only step.
- The memory cap needs tuning on the 512 MB instance.
- X-Forwarded-For spoofing (use the rightmost-hop IP and also limit per phone).
- htmx versus a strict CSP.
- A future table could miss RLS (guard test).
- A rollback runs old code against the new schema (expand-only migrations).
- Env vars are not rolled back (safe defaults).

## Cost (Render Frankfurt, check current prices)
- Web Starter: about $7
- Postgres Basic-256mb: about $6, plus storage
- Sentry, uptime monitor and Hobby workspace: free
- **Total about $13–15/month.** The free Postgres plan is not an option: free databases expire
  after 30 days.
