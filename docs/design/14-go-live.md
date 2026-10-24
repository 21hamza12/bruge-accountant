# Phase C — go live and parallel run

Status: plan for approval (24 Oct 2026). Phases B, D and E are merged. Phase C puts the app on the
internet for Bruge Voyages, proves it is safe (the nine-item release checklist, with evidence),
and runs it **beside the accountant's Excel for 2 to 4 weeks** until every day's TJQ APRES matches.

The deployment itself is already prepared: `render.yaml` (web service, hourly purge job, private
Postgres 16 in Frankfurt), the Dockerfile, `/healthz`, and the founder checklist in
[`docs/runbook.md`](../runbook.md). Nothing has been run on Render yet.

## 1. Steps

| # | Step | Who | Time |
|---|---|---|---|
| C1 | **Code before going live** (§2) | Claude | ~1 day |
| C2 | **First deploy** on Render from the blueprint; environment variables; database roles | Owner clicks, Claude guides (runbook §1–3) | ~1 hour together |
| C3 | **Drills**: rollback (twice), restore of a backup, an alert sent and received, deploy failure | Together (runbook §5–8) | ~1 hour |
| C4 | **Release checklist** run against the live site, every item with evidence (§3) | Claude | ~2 hours |
| C5 | **Bruge set up**: agency and manager, staff with phones and Amadeus signs, partners and fees, AI budget | Claude, from the owner's lists | ~1 hour |
| C6 | **Training**: one session for agents (paste, screenshot, chat) and one for the accountant and manager (upload, flags, approvals, closing a day, export) | Owner on site, Claude prepares a one-page guide in French | ~2 × 1 hour |
| C7 | **Parallel run**, 2–4 weeks: every closed day, the app's TJQ APRES is compared cell by cell with the accountant's (§4) | Accountant sends her file; Claude reports | daily, a few minutes |
| C8 | **Switch**: the accountant stops her Excel for the daily sheet once 10 working days in a row match | Owner decides | – |

C1 can start at once. C2 needs the Render account (§5).

## 2. Code before going live (C1)

1. **Error alerts (release item 8).** Sentry is only a setting today; nothing sends errors. Wire the
   Sentry SDK (free tier) behind `SENTRY_DSN`: unhandled errors and failed jobs, **no request
   bodies, no local variables, no personal data** (scrubbing tested), release = git commit.
2. **Durable logs (item 8).** Render keeps logs only a short time. Send the JSON logs to a log
   service through a Render log stream (Better Stack free tier, or similar) and document the
   search for a request id. Secrets and bodies are already kept out of the logs (tested).
3. **CHD/INF name padding in the TJQ APRES** (**built**, 24 Oct 2026; the real-file check now shows 0 differing cells; the one difference found by the B11 real-file check,
   `09-phase-b-plan.md` §3d): reproduce the Amadeus padding, so the parallel run compares equal.
4. **Parallel-run comparison** (**built**, 24 Oct 2026, see [`15-parallel-run.md`](15-parallel-run.md)): a manager/accountant upload of her APRES Excel for a closed day; the
   app compares it with its own stored export (same 16 columns) and shows the cells that differ.
   Reuses the B11 comparison; nothing is changed by it.
5. **Production AI settings**: `ANTHROPIC_API_KEY` stays out of `render.yaml` (entered in the
   dashboard); add the chat and screenshot variables to the runbook table with production values;
   a smoke check that one screenshot and one chat question work on the live site.
6. **French one-page guides** for agents and for the accountant/manager (`docs/guides/`).

Each item: tests first, reviews (security review for 1, 2, 4), CI green, merged.

## 3. Release checklist — where each item stands today

The global checklist (`~/.claude/release-checklist.md`) is run for real in C4, on the live site.
Today, from the code and the tests only:

| # | Item | Today | Evidence so far | Still to show in C4 |
|---|---|---|---|---|
| 1 | Tenancy isolation (IDOR) | strong in code | FORCE RLS on every agency table; `test_rls.py`, `test_tenancy.py`, `test_repository_tenancy.py`; chat isolation tests | log in as agency A, request agency B's sale/flag/export by id on the live site: 404 |
| 2 | Password-reset expiry (30 min) | in code | no e-mail reset links exist; manager-issued temporary passwords are Argon2-hashed, single use, **30 minutes** (`app/auth/temp_passwords.py`, `test_temp_passwords.py`) | use one after 30 minutes on the live site: refused |
| 3 | Input sanitisation (SQLi, XSS) | in code | SQLAlchemy only, no SQL built from input; Jinja autoescape; strict CSP | header and payload checks on the live site |
| 4 | API origin restriction | in code | no CORS (same-origin app, production refuses `*`); CSRF token + Origin check on every POST (`test_csrf.py`); `ALLOWED_HOSTS` | foreign Origin and foreign Host refused on the live site |
| 5 | Rate limiting | in code | buckets in Postgres, shared across instances (`test_ratelimit.py`): login, temporary passwords, uploads, screenshots, chat | repeated logins from one address blocked on the live site |
| 6 | Custom error screens | in code | French 401/403/404/500 and offline pages, no stack traces (`test_error_pages.py`) | runbook §7 smoke test |
| 7 | Database indexes | in code | query plans checked on Postgres for the hot queries (`test_query_plans.py`) | none extra |
| 8 | Logging and alerting | **fails today** | JSON logs and audit trail exist; **no error alerting, no uptime monitor, no durable log store** | C1 items 1–2, then one alert sent and received in C3 |
| 9 | Rollback | **unverified** | Render zero-downtime deploys (new version must pass `/healthz` before traffic moves) and one-click rollback; `test_rollback_invariant.py` | the rollback drilled twice (C3). Note: this is Render's zero-downtime switch, **not a strict blue/green** with two live copies; the report will say so |

## 4. Parallel run (C7)

- Bruge works in the app from day one (paste, screenshot or chat for every ticket; Amadeus report
  uploaded; flags answered; day closed). The accountant keeps doing her Excel APRES as today.
- Each day she uploads her APRES in the app (C1 item 4). The app shows the differing cells; each
  difference is explained (her mistake, an app bug, or a rule to agree) and fixed the same week.
- **Exit:** 10 working days in a row with no unexplained difference. Then the daily Excel stops
  (phase F and G take over partners and invoices later, with their own switch-over days).

## 5. Needed from the owner

**To start the deploy (C2):**
1. A **Render account** (sign up with GitHub) with a **payment method**. Cost about 15–20 USD a
   month (web Starter, purge job, Postgres Basic). Claude cannot enter card details.
2. **Connect GitHub to Render** for `21hamza12/bruge-accountant` (Render asks during sign-up).
3. **One hour together** for the blueprint, the variables and the drills (C2–C3).
4. A **production Anthropic key** (Console, API keys: a new key named e.g. `bruge-prod`, separate
   from the dev key) with a **monthly spend limit** (e.g. 20 USD). It is pasted only into the
   Render dashboard, never into the chat.
5. Free accounts for alerts: **Sentry** (errors) and **Better Stack** or **UptimeRobot** (uptime,
   and logs if Better Stack). Their keys also go only into the Render dashboard.

**Decisions:**
6. **Web address**: `bruge-accountant.onrender.com` (free) or your own domain (about 10–15 USD a
   year, e.g. `app.<your-domain>`).
7. **Repository**: back to **private** before real data (as agreed), which needs either the free
   monthly minutes or a small Actions budget (about 10 USD).
8. **AI budget per agency**: 5 USD a month (default; measured cost about 0.0002 USD per chat
   question and 0.0008 per screenshot) or another figure.

**From Bruge:**
9. **Staff list**: for each person, name, phone, role (agent, accountant, manager) and Amadeus
   sign (e.g. 0004SV). The manager's phone first.
10. **Partners and fees**: each partner (BADI, ABSHER, REND, WORLD GATE, …), the usual spellings
    agents type, and the fee per ticket.
11. **The accountant's answers**: the second office's TJQ, unusual fees, layovers, airline 495.
12. **A start date** and who on site runs the training.
13. **A short written agreement** with Bruge: price, data kept and who can see it, AI processing
    (Anthropic), and what happens if they leave (export of their data).

## 6. Done when

The live site passed the nine release items with evidence (or each exception is written down and
accepted by the owner), a backup was restored once, an alert was received once, Bruge's staff use
it every day, and 10 working days in a row matched the accountant's APRES.
