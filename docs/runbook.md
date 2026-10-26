# Runbook: first deploy on Render (founder checklist)

Phase 2, PR 1, step 8. Nothing here has been run yet: every box below is **unverified** until you
tick it and write the result in the "Results" section at the bottom. The code side (Dockerfile,
`render.yaml`, `/healthz`) is covered by tests and by the `docker` job in CI; the Render side can
only be checked on Render.

Prerequisites: a Render account (Hobby workspace is fine), the GitHub repo connected to Render,
and the branch you want to deploy merged or selected in the blueprint.

## 1. Create the services from the blueprint

- [ ] Render dashboard, **New**, **Blueprint**, pick this repository and branch. Render reads
      `render.yaml` and proposes a web service `bruge-accountant`, a cron job `bruge-purge` (the
      hourly purge, see "Routine housekeeping") and a Postgres `bruge-db`.
- [ ] Check the plan before confirming: web **Starter**, cron **Starter**, database
      **Basic-256mb**, all region **Frankfurt**. (Render's current Blueprint reference lists plan
      ids such as `free` and `0.5c-512mb` and no longer mentions `starter`; the names in
      `render.yaml` are the older ones. If the dashboard rejects or renames a plan, change the web
      and the cron plan together and note it in Results. Unverified until the first sync.) The database must show no public access (`ipAllowList: []`).
- [ ] The dashboard asks for the `sync: false` variables (next section). Fill them in before the
      first deploy; otherwise the first deploy fails on purpose rather than starting
      half-configured.
- [ ] Postgres version: `render.yaml` pins major version 16, the same as CI. If Render now
      offers a newer major in Frankfurt, change both together (`postgresMajorVersion` and the
      `image: postgres:NN` lines in `.github/workflows/ci.yml`) and avoid features that need the
      newest version only.

## 2. Environment variables (dashboard)

Set on the web service, **Environment** tab.

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | the **Internal** connection string of `bruge-db` | `sync: false`. Separate role: the `bruge_app` URL. Single-role mode: the owner URL (same as `MIGRATION_DATABASE_URL`); row-level security is then FORCEd but the web process holds owner rights (section 3 lists the residual risk). **No real agency data may be loaded before migration 0003 has run.** Never paste it into chat, tickets or the repo. |
| `APP_DB_ROLE` | the app role name (e.g. `bruge_app`), or unset | Read only by migration 0003 (`init-db`, and its downgrade), never by the web process. Set: grants that role its privileges, and fails if the role is missing, a superuser or BYPASSRLS. Unset: single-role mode with a warning. Section 3. |
| `SENTRY_DSN` | the project DSN from Sentry (Settings, Client Keys), entered in the dashboard of **both** the web service and the cron job `bruge-purge` | `sync: false`. Empty means no alerts and nothing is sent. Section 9 says what is sent (errors only, scrubbed) and how to test it. |
| `ANTHROPIC_API_KEY` | the production key, entered **only in the dashboard** of the web service | Not in `render.yaml` on purpose (the feature is hidden without it), never in the repo, a ticket or the chat. All AI variables with their production values, and the key check after the first deploy (`python -m app ai-smoke`): "AI settings" below. Add it to the web service only. `AI_ENABLED=true` without a key makes production refuse to start. Monthly budget per agency: `AI_MONTHLY_BUDGET_USD` (default 5), or per agency with `DATABASE_URL="$MIGRATION_DATABASE_URL" python -m app ai-budget <slug> --usd N [--disable]`. See `docs/design/11-screenshots.md`. The same key also feeds the chat's free questions (`AI_CHAT_ENABLED=false` switches only the chat off; `AI_CHAT_MODEL`, `AI_CHAT_FALLBACK_MODEL`, `AI_CHAT_BUDGET_SHARE`, `AI_CHAT_TIMEOUT_S`: see `docs/design/13-chat-ai.md`); migration 0016 must have run before the first chat question is asked. |
| `ALLOWED_HOSTS` | the real public host name(s), comma separated, e.g. `bruge-accountant.onrender.com` | `sync: false` in `render.yaml`, so the blueprint asks for it and a sync never resets it. If it is missing the app refuses to start; if it is wrong every request, health check included, is rejected with 400 and the deploy fails. No wildcards: production refuses `*` and `*.example.com`. |
| `CORS_ALLOWED_ORIGINS` | leave unset | Same-origin app, so no CORS middleware is installed. Production refuses `*`. |
| `MIGRATION_DATABASE_URL` | injected from the database | Do not edit. Render env vars apply to the running service, so this owner URL is present in the container; that is the accepted pilot trade-off (approved decision 2 in `docs/phase2-plan.md`). The web process never reads it; only `init-db` (the `preDeployCommand`) and the admin CLI do, and a test enforces that. |
| `ENV`, `COOKIE_SECURE`, `TRUSTED_PROXY_HOPS` | already in `render.yaml` | `TRUSTED_PROXY_HOPS` is confirmed in section 4. |

### AI settings (screenshots and chat)

Only `ANTHROPIC_API_KEY` must be entered. Every other variable has the default shown, which is
also the production value; set one only to change it (web service Environment tab). The cron job
and `init-db` need none of them. Models must be names in `app/ai/pricing.py` (a typo fails at
start-up, not on the first call).

| Variable | Production value | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | a **new key named `bruge-prod`** (not the dev key), with a monthly spend limit in the Anthropic Console (e.g. 20 USD) | Dashboard only, web service only. Never printed, logged or sent to Sentry. |
| `AI_ENABLED` | `true` | Master switch. Unset means "on when there is a key". `true` without a key makes production refuse to start (a loud failure, preferred to a silently hidden feature). `false` hides screenshots and the chat's model calls. |
| `AI_MONTHLY_BUDGET_USD` | `5` | Default budget per agency and month, in USD. One agency can differ: `DATABASE_URL="$MIGRATION_DATABASE_URL" python -m app ai-budget <slug> --usd N [--disable]`. Measured: about 0.0002 USD per chat question, 0.0008 per screenshot. |
| `AI_SCREENSHOT_MODEL` | `claude-haiku-5-5` | The cheap model that reads a screenshot first. |
| `AI_FALLBACK_MODEL` | `claude-sonnet-5-5` | The stronger model tried when the first could not read it. Empty switches the second try off. |
| `AI_TIMEOUT_S`, `AI_MAX_RETRIES` | `30`, `1` | One screenshot call: timeout, and SDK retries on 429/5xx/network errors only. |
| `AI_CHAT_ENABLED` | `true` | `false` switches only the chat's model off (the chat still answers from code). |
| `AI_CHAT_MODEL` | `claude-haiku-5-5` | First model for a free chat question. Also the one `ai-smoke` uses. |
| `AI_CHAT_FALLBACK_MODEL` | `claude-sonnet-5-5` | Stronger second model. Empty disables escalation. |
| `AI_CHAT_BUDGET_SHARE` | `0.8` | The chat stops at this share of the month's budget; the rest is kept for screenshots. |
| `AI_CHAT_TIMEOUT_S` | `20` | One chat API call. |
| `AI_CHAT_MAX_RETRIES` | `0` | SDK retries inside a chat call. Zero on purpose: a retry doubles cost and wait; the turn deadline and the stronger model are the second chance. |
| `AI_CHAT_TURN_DEADLINE_S` | `45` | A whole chat turn, all calls included. |
| `AI_CHAT_FALLBACK_DAILY_CAP` | `20` | Calls to the stronger model one agency may cost in a UTC day. `0` never escalates. |

**Refused in production.** `ANTHROPIC_BASE_URL` (the key and every question would go to another
host), `ANTHROPIC_AUTH_TOKEN` (replaces the key) and `ANTHROPIC_LOG` (`debug` makes the SDK log
request bodies, a base64 screenshot included). If any is set, the service and `ai-smoke` refuse
to start and name the variable, never its value. Do not set them in the dashboard.

**Check the key after the first deploy** (and after any key change). In Render Shell on the web
service:

```bash
python -m app ai-smoke
```

It makes **one** tiny call (a fixed word, no database, no agency data, a few dozen tokens, well
under a cent) through the same client the chat uses, and prints one line:

- `ai-smoke: ok model=claude-haiku-5-5 input_tokens=N output_tokens=N`, exit 0;
- `ai-smoke: failed kind=auth model=...`, exit 1. The kind is `auth` (wrong or revoked key),
  `rate_limit`, `timeout`, `connection`, `server` or `request`. Or a configuration message when
  the key is missing, `AI_ENABLED=false`, or a refused variable above.

It never prints the key or the model's answer, and it is not counted against any agency's budget.
It is unverified against the real API until run (the tests use a fake client). Then, in the
browser, as a real user, ask one free chat question and read one screenshot (Bruge's own test
ticket), and confirm the Anthropic Console usage page shows the calls.

- [ ] `ai-smoke` prints `ok`. Record the date, the model and the token counts in Results.
- [ ] One chat question and one screenshot work on the live site.
- [ ] The Anthropic key has a monthly spend limit set in the Console.

- [ ] The cron job `bruge-purge` needs the same `sync: false` variables, entered on **its** own
      Environment tab (a cron job does not share the web service's variables): `ALLOWED_HOSTS`,
      `DATABASE_URL` (the same app-role URL as the web service) and `SENTRY_DSN`. Production
      settings refuse to load without `ALLOWED_HOSTS` and `DATABASE_URL`, so a missing value makes
      the purge fail on purpose. It gets no `MIGRATION_DATABASE_URL`: the purge never reads it.
- [ ] `ALLOWED_HOSTS` set to the real host (note the exact `*.onrender.com` name Render assigned).
- [ ] `DATABASE_URL` set (internal URL).
- [ ] If the service refuses to start, read the deploy log: the settings validator prints which
      rule failed (never the values).

Health check host: Render's health checker may call `/healthz` with a Host header that is not the
public name. If the deploy keeps failing on the health check while `ALLOWED_HOSTS` is correct,
record the Host it sends (section 4 shows how) and add it to `ALLOWED_HOSTS`.

## 3. Database roles: separate app role, or single-role mode

Tenant isolation is row-level security (migration 0003), FORCEd so it binds the table owner too.
It works in two modes; pick one with the check below. The application code is the same.

| | Separate role (preferred) | Single-role mode |
|---|---|---|
| Needs `CREATE ROLE` | yes | no |
| `APP_DB_ROLE` | the role name, e.g. `bruge_app` | unset |
| `DATABASE_URL` (web app) | the app role | the owner (same as `MIGRATION_DATABASE_URL`) |
| `MIGRATION_DATABASE_URL` | the owner | the owner |
| What the web process can do | only what 0003 granted: no DELETE on tickets or uploads, no CREATE, read-only agencies | everything the owner can |
| Isolation between agencies | RLS, and the role cannot switch it off | RLS (FORCE), but the owner can switch it off |

**What row-level security protects against, and what it does not.** It contains **IDOR and
forgotten-filter bugs**: a query that forgot `WHERE agency_id = ...` still sees one agency. It does
**not** contain SQL injection: a query an attacker controls can run `set_config('app.agency_id',
...)` and pick its own tenant. The control for SQL injection is different: all SQL goes through
the ORM, and a code guard test bans raw SQL (`text(`, `exec_driver_sql`, ...) and `set_config`
outside `app/tenancy.py`.

**Residual risk of single-role mode.** The owner can run `ALTER TABLE ... NO FORCE ROW LEVEL
SECURITY` or `DISABLE ROW LEVEL SECURITY`, and can `TRUNCATE audit_events` (no policy applies to
TRUNCATE). So an attacker who gets code execution on the server (and with it the owner
credentials) defeats the isolation and can erase the audit trail; the owner can also DELETE
tickets and change the schema. Shipping audit events out of the database (so a database owner
cannot erase them) is PR 5; until then treat the audit table as evidence only against bugs, not
against an attacker holding the owner credentials. In separate-role mode the app role can do
none of this.

**Privileged roles are refused.** A superuser or BYPASSRLS role skips every policy. Migration
0003 and `grant-app-role` refuse such an `APP_DB_ROLE`, and in production the web app refuses to
start when `DATABASE_URL` connects as one (it prints which rule failed, never the URL). If your
host only provides a superuser, you cannot get isolation from RLS there.

Record in the Results table which mode is in use.

- [ ] Find out whether the default Postgres user can `CREATE ROLE`. Open Render Shell on
      `bruge-accountant` and run:

```bash
python - <<'PY'
import os
import psycopg
url = os.environ["MIGRATION_DATABASE_URL"]
with psycopg.connect(url, autocommit=True) as conn:
    cur = conn.cursor()
    cur.execute("SELECT current_user, rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user")
    print("current role:", cur.fetchone())
    try:
        cur.execute("CREATE ROLE bruge_probe NOLOGIN")
        print("CREATE ROLE: OK")
        cur.execute("DROP ROLE bruge_probe")
    except Exception as exc:
        print("CREATE ROLE: FAILED:", type(exc).__name__, exc)
PY
```

  (`psycopg` accepts a plain `postgresql://` URL here. This prints no password.)

- [ ] Record the output in Results.

### If CREATE ROLE works (separate role)

Do this **before the release that runs migration 0003**: the migration only grants privileges to
the role named by `APP_DB_ROLE` while it runs. A role created afterwards has no grants until you
run `python -m app grant-app-role bruge_app` as the owner (below); it is safe to repeat.

1. In Render Shell, create the role. Never build the `CREATE ROLE` statement with string
   formatting (a password with a quote would break it, and the password would sit in shell
   history). Let psql ask for it, which also keeps it out of history and logs:

```bash
psql "$MIGRATION_DATABASE_URL" -c "CREATE ROLE bruge_app LOGIN NOSUPERUSER NOBYPASSRLS"
psql "$MIGRATION_DATABASE_URL"        # then, at the psql prompt:  \password bruge_app
```

   `\password` asks twice and sends only a hash. If you must script it, pass the password as a
   psql variable and let psql quote it (`:'var'`). A `-v` value is visible in the process list,
   which is acceptable in your own Render Shell but not on a shared machine, so prefer
   `\password` when in doubt:

```bash
PGPASSWORD_NEW="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
printf '%s\n' "ALTER ROLE bruge_app PASSWORD :'newpw'" |
  psql "$MIGRATION_DATABASE_URL" -v "newpw=$PGPASSWORD_NEW"
echo "$PGPASSWORD_NEW"   # build the app URL (user bruge_app) from this, then forget it
```

2. Dashboard, web service environment: set `APP_DB_ROLE=bruge_app` and change `DATABASE_URL` to the
   same internal URL with the user `bruge_app` and the new password. Keep `MIGRATION_DATABASE_URL`.
3. Deploy. `preDeployCommand: python -m app init-db` runs 0003 as the owner and grants `bruge_app`
   its privileges; the migration fails loudly if `APP_DB_ROLE` names a role that does not exist.
4. Check: the deploy log shows no "single-role mode" warning, and `/healthz` is 200.
5. A role created after the migration ran (or one that lost its grants): as the owner,
   `DATABASE_URL="$MIGRATION_DATABASE_URL" python -m app grant-app-role bruge_app`. It validates
   the name, refuses a superuser or BYPASSRLS role, and can be run again at any time.

### If CREATE ROLE is refused (single-role mode)

Leave `APP_DB_ROLE` unset. `DATABASE_URL` stays the owner URL. The migration logs a warning
("APP_DB_ROLE is not set: single-role mode"); that is expected. Isolation relies on FORCE RLS alone
(residual risk above). Revisit if Render or another host offers a second role.

**Production must use the separate app role.** The original Amadeus report (`amadeus_lines`,
migration 0006) is append-only, but that is fully enforced only in app-role mode: the app role has
no UPDATE, DELETE or TRUNCATE privilege on it. In single-role mode the owner role can still
TRUNCATE the table, DROP POLICY or DISABLE ROW LEVEL SECURITY, so the "never modified" guarantee
does not hold there. Also note that once lines exist, an upload or an agency cannot be deleted
(`ON DELETE RESTRICT` plus append-only); a privileged, audited retention or erasure path is a
decision for a later phase.

### Creating the first agency and manager

`add-agency` and `bootstrap-agency` insert into `agencies`, which the app role may not do, so run
them with the owner connection, from Render Shell (the password is read from a prompt, asked
twice; it is never an argument and never printed):

```bash
DATABASE_URL="$MIGRATION_DATABASE_URL" python -m app bootstrap-agency bruge-voyages "Bruge Voyages" \
    --manager-phone "+222 12 34 56 78" --manager-name "Full Name"
```

It prints the new agency and user ids, nothing else, and records an `agency_bootstrapped` audit
event (agency, new manager id, the OS user who ran it; no password, hash or phone number). It
refuses (exit code 2) when the slug or the phone number already exists, or when the password is
too short, too common (a bundled list of common passwords, any case) or one repeated character.
The manager does not have to change the password at first login (you chose it); pick a long one.
`import` runs with the normal `DATABASE_URL`.

Prompts: Ctrl-C or end of input at the prompt ends cleanly (exit code 130 / 2), nothing created.
**Non-interactive use (scripts, pipes, and anything on Windows): `--password-stdin`** reads the
first line of standard input. The Windows prompt reads the console keyboard and ignores
redirected input, so a script must use `--password-stdin`. A command line mistake prints the usage
and "invalid command line (exit code 2)", never the tokens you typed; `-p` and `--password...` are
refused.

### Routine housekeeping

`python -m app purge-expired` (any connection, no tenant) deletes expired or revoked sessions,
used or expired temporary passwords, audit events older than 400 days and finished rate-limit
windows, and prints the counts.

**It runs hourly as the Render cron job `bruge-purge`** (`render.yaml`: `type: cron`,
`schedule: "0 * * * *"`, `dockerCommand: python -m app purge-expired`, the same Dockerfile and the
same environment as the web service, minus the owner URL). Hourly keeps each run small: the
rate-limit table gets a row per login attempt window, and the job deletes in chunks of 10 000 rows
(one statement and one commit per chunk) so a backlog never holds a long lock. The three
row-level-security tables are deleted by their DELETE-only policies in one statement each (a
chunked DELETE would need read access the policies deliberately do not grant); an hourly run only
ever finds what expired since the last one.

- [ ] After the first deploy, open `bruge-purge` in the dashboard, **Trigger Run**, and check the
      log for the JSON counts and exit code 0. Then wait for one scheduled run.
- [ ] Cost: a cron job is billed for the time it runs (a few seconds an hour), with a monthly
      minimum for its plan. Confirm the figure on the pricing page; it is not in the cost estimate
      in `docs/phase2-plan.md`.
- Schema keys used for the cron service (`type`, `schedule`, `dockerCommand`, `dockerfilePath`,
  `runtime`, `region`, `plan`, `autoDeploy`, `envVars`) were checked against Render's Blueprint
  Reference. Two things could **not** be confirmed without a real sync and stay unverified: that
  the plan name `starter` is still accepted for a cron job (see the plan note in section 1), and
  that `dockerCommand` overrides the image's `CMD` for a cron job the same way it does for a web
  service.

### A manager is locked out (forgot the password)

A manager can reset **staff** passwords from the web (page "Equipe", button "Emettre un mot de
passe temporaire"), never another manager's and never their own. A manager who is locked out is
reset by you, the platform owner, from the Render Shell of the web service, with the owner
connection (like `bootstrap-agency`):

```bash
DATABASE_URL="$MIGRATION_DATABASE_URL" python -m app reset-password --phone "+222 12 34 56 78"
```

It prints one JSON line on standard output with `temporary_password` (and the user id, role and
masked phone), and a reminder on standard error. **Read it from the terminal and hand it to the
manager over a channel you trust (a phone call, not a group chat); it is never printed again.**
The password is valid for 30 minutes and works once; the manager must choose a new password at
login, and has 30 minutes from logging in to do so. The command revokes all of that user's
sessions and records a `temp_password_issued` audit event (no actor, `via: cli`, the OS user who
ran it, no password). It works for staff too, and refuses (exit code 2, nothing changed) an
unknown phone number or a deactivated account. There is no password option: the value is
generated, never typed.

- [ ] Rehearse once on a scratch database: reset a test manager, log in with the printed password,
      change it, and check the audit row (Results).

## 4. How does `X-Forwarded-For` arrive, and what is `TRUSTED_PROXY_HOPS`?

The login rate limiter (PR 3) uses the client IP. The Dockerfile does **not** pass uvicorn's
`--proxy-headers` (it would trust only `127.0.0.1`, so behind Render the scheme would stay `http`
and every client would look like the proxy). Instead `app/web/proxy.py` applies these rules:

- `TRUSTED_PROXY_HOPS=N` with N > 0: the client address is the N-th entry from the **right** of
  `X-Forwarded-For` (the left side is client-supplied and never used). With fewer than N entries,
  or an entry that is not an IP address, the socket peer is kept. The scheme is the rightmost
  `X-Forwarded-Proto` value, only if it is `http` or `https`.
- `TRUSTED_PROXY_HOPS=0` (the code default, the safe one): both headers are ignored.
- `render.yaml` ships `TRUSTED_PROXY_HOPS=1`, which is a guess until this step confirms it.

We must know the real hop count and whether a client can forge the left side.

- [ ] The request log deliberately does not print headers. Ask the developer for a one-off
      debug commit on a **scratch branch** (never `main`) that logs `X-Forwarded-For`,
      `X-Forwarded-Proto` and `Host` for `/nope`. Deploy it, then from your laptop run
      `curl -H 'X-Forwarded-For: 1.2.3.4' https://<host>/nope` and read the log line.
      Record the full `X-Forwarded-For` value the app saw.
- [ ] Expected: your forged `1.2.3.4` is on the left, Render appends your real IP on the right. If
      so the rightmost entry is trustworthy and `TRUSTED_PROXY_HOPS=1` is correct. If there are
      two proxies (for example Cloudflare in front later), set `TRUSTED_PROXY_HOPS=2`.
- [ ] Record whether `X-Forwarded-Proto` is `https`, and which `Host` header the health check sent.
- [ ] Remove the debug line afterwards; do not leave header logging on.

## 5. Rollback behaviour

Render has zero-downtime deploys and one-click rollback, but not true blue/green. We need to know
exactly what a rollback does before PR 5's drill.

**Migration 0003 is a contract migration, not expand-only.** After it has run, a release that
does not bind a tenant (release N, before this one) sees zero rows and cannot insert: every
policy compares `agency_id` with a setting only the new code sets. Consequences:

- 0003 can only ship in the same release as the tenant-binding code. It is never deployed ahead
  of that code, and no earlier release can be run against a database that already has it.
- **Rolling back past it:** run `alembic downgrade 0002` as the owner *before* redeploying release
  N (never the other way round: a released N against an un-downgraded database is a total
  outage). In Render Shell, with the same `APP_DB_ROLE` the upgrade used, so the grants are
  revoked too:

```bash
DATABASE_URL="$MIGRATION_DATABASE_URL" APP_DB_ROLE=bruge_app python -m alembic downgrade 0002
```

  Leave `APP_DB_ROLE` unset in single-role mode. Then roll the service back in the dashboard.
  To go forward again, redeploy the new release (`init-db` upgrades to head).
- A rollback between two releases that both include 0003 is an ordinary redeploy: nothing to
  downgrade. Later migrations must be expand-only (plan step 36).
- The « Questions de prix » page was removed without a migration: `tickets.price_status` (and its
  CHECK) stay in the schema, written as `awaiting_price` by the import, and nothing reads them. A
  rollback to the release before is therefore safe.
- Proof: `tests/test_rollback_invariant.py` shows an unbound session seeing 0 rows after 0003,
  and working again after `downgrade 0002`, in both deployment modes.

- [ ] Deploy twice (any harmless change, version N then N+1). Confirm `/healthz` shows the new
      commit: `curl https://<host>/healthz` returns `{"status":"ok","version":"<git sha>"}`.
- [ ] In the dashboard, **Rollback** to N. Record:
  - does `preDeployCommand` (`python -m app init-db`) run again on rollback? (check the deploy
    log for "database ready")
  - does `/healthz` report version N afterwards?
  - are environment variables restored to their old values, or do they stay current? (Render
    keeps current env vars; confirm.)
- [ ] Roll forward again to N+1 and confirm `/healthz`.
- [ ] Drill the rollback that crosses 0003 (once, on a scratch database): `alembic downgrade 0002`,
      redeploy the previous release, confirm it works, then redeploy the new release and confirm
      `init-db` upgrades again. Record the output in Results.
- [ ] Failure path: set `ALLOWED_HOSTS` to something invalid on a scratch deploy and confirm the
      failed deploy leaves the previous version serving traffic and sends the deploy-failure email.

## 6. Uptime monitor on `/healthz`

- [ ] Create a free UptimeRobot or Better Stack monitor: HTTPS, URL `https://<host>/healthz`,
      interval 5 minutes (free tier), keyword check `"status":"ok"`, alert by email to the founder.
- [ ] Pause the service (or break `DATABASE_URL` on a scratch deploy) to trigger one alert, confirm
      the email arrives, then restore and confirm the recovery email. Record both times.
- [ ] Check the keyword text before relying on it: `curl -s https://<host>/healthz` must print
      exactly `{"status":"ok","version":"<sha>"}` (no spaces; a test pins the `"status":"ok"`
      text, and HEAD answers 200 for monitors that default to it).
- [ ] The monitor must call the **public** host name (it is in `ALLOWED_HOSTS`); one that calls
      another name gets 400 and alerts falsely.
- [ ] Alert contacts: the founder's e-mail and one second person or channel, so one inbox is not
      a single point of failure. Record both in Results.
- [ ] Turn on Render deploy-failure emails (Account, Notifications).

## 7. Smoke test after the first deploy

- [ ] `curl -i https://<host>/healthz` is `200` with `{"status":"ok","version":"<sha>"}`.
- [ ] `curl -i https://<host>/nope` is `404`, shows the French "Page introuvable" page, has
      `Content-Security-Policy`, `Strict-Transport-Security`, `X-Content-Type-Options`,
      `Referrer-Policy: same-origin`, `Permissions-Policy`, `X-Request-ID`, and **no** `Server`
      header.
- [ ] `https://<host>/docs` and `/openapi.json` are 404.
- [ ] `curl -i -H 'Host: evil.example.com' https://<host>/healthz` is rejected (400).
- [ ] In the browser: `/offline` renders; in DevTools, Application, Service Workers shows `/sw.js`
      activated; with the network set to Offline, reloading any page shows the offline page. (The
      worker logic is unit-tested under node; the browser behaviour is only verified here.)

## 8. Backups and data migrations

**Backups.** Forced row-level security applies to `pg_dump` too. Run as the table owner, `pg_dump`
either fails ("query would be affected by row-level security policy") or, depending on the
settings, dumps **empty tables** without an error. Do not trust a dump taken with the web or
owner credentials.

- Use the host's managed backups (Render Postgres snapshots and point-in-time recovery on paid
  plans), which work at the storage level and are not affected by RLS; or
- create a dedicated maintenance role with `BYPASSRLS` (and nothing else: `NOLOGIN` until a dump
  is taken, no use by the application) and dump as that role. Never give BYPASSRLS to the owner
  or the app role: both would then skip every policy, and the guards above refuse it.

**Data migrations** (an Alembic migration or a script that rewrites tenant rows) run under the
same policies. They must either bind one agency at a time (`bind_tenant`/`rebind_tenant` per
agency, in a loop over agencies found with the slug lookup or the owner CLI), or run as the
maintenance role above. A data migration that "works" as the owner without binding silently
changes zero rows. Test it on a copy with at least two agencies.

- [ ] **Restore drill** (once before go-live, then after every change to the backup setup):
      restore the latest managed backup into a scratch database; check `alembic_version`, that
      the row counts of `agencies`, `users`, `uploads` and `tickets` are not zero when the
      source is not empty (an empty restore is the RLS symptom), and that a login and an import
      work against it. Record the date and the counts in Results.

## 9. Error alerts (Sentry)

Release checklist item 8. The code is built and tested (`app/alerting.py`,
`tests/test_alerting.py`); it is **unverified live** until the drill below is done.

**What it does.** With `SENTRY_DSN` set, the web service sends one event for each unhandled
exception, and the jobs `init-db` and `purge-expired` send one when they fail (tag `job`). With
`SENTRY_DSN` empty nothing starts and nothing is sent (development, tests, any service without it).

**What is sent.** The exception type, its message, the stack as file names, function
names and line numbers, the route template (`/sales/{sale_id}`), the HTTP method, the release (the
git commit, the same value `/healthz` shows) and the environment (`ENV`).

**What is never sent.** Request bodies, cookies, headers, query strings, local variables, source
lines, the user (not even the id), the host name, log records, breadcrumbs, SQL statements and
their parameters, traces, profiles, sessions. A last scrubber (`before_send`) masks passwords,
tokens, CSRF values, API keys (`sk-...`), e-mail addresses and numbers of 8 digits or more (phone
and ticket numbers) in whatever text is left. This is tested with a fake transport; no test
reaches Sentry.

The event is built from an allow-list (event id, time, level, release, environment, route
template, method, URL without query, the `job` tag, and per exception its type, message and stack
as file, function and line); every other field is dropped. A database error is sent as its
constraint name and SQLSTATE only, and a validation error as its field names and error types
(drivers and pydantic quote the data, e.g. `DETAIL: Key (...)=(3, <name>)` or `input_value=`).
Other messages are cut at `DETAIL`, `Failing row contains` and `input_value=` and scrubbed.
Handled HTTP errors (a 503 with a `detail` text) and a failing `/healthz` send nothing: the
request log has them, and the uptime monitor alerts on `/healthz` (one event per probe would
exhaust the free quota in an outage).

Residual risk: an exception message written by our own code could contain a name or other free
text; messages are scrubbed for the patterns above only. Keep personal data out of exception
messages (the rule for log lines too), and read the first real events with that in mind.

- [ ] Sentry account (free), new project, platform **FastAPI** (or Python). Copy the DSN.
- [ ] In Sentry, Project Settings, Security & Privacy: turn on **Data Scrubber** and **Prevent
      Storing of IP Addresses**.
- [ ] Paste the DSN in the Render Environment tab of `bruge-accountant` **and** `bruge-purge`
      (`SENTRY_DSN`, `sync: false`). Redeploy the web service. The DSN only allows sending, but
      it still stays out of the repo and the chat.
- [ ] Alert rule: Alerts, new rule, "A new issue is created", notify the founder by e-mail (and
      the second contact of section 6).
- [ ] **Drill (C3): send one alert and receive it.** In Render Shell on the web service:

```bash
python - <<'PY'
from app import alerting
from app.settings import Settings
alerting.init_alerting(Settings())
alerting.capture_exception(RuntimeError("alert drill, ignore"), job="drill")
alerting.flush()
print("sent")
PY
```

  Confirm the e-mail arrives, and that the event shows `release` equal to the commit on
  `/healthz`, environment `production`, and **no** request, user, cookie or breadcrumb section.
  Resolve the issue. Record the time in Results.
- [ ] Failed job: trigger `bruge-purge` with a wrong `DATABASE_URL` on a scratch deploy; confirm
      one event tagged `job: purge-expired`.

## 10. Durable logs (Better Stack) and finding one request

Release checklist item 8. Render keeps service logs only for a short time, so the JSON logs are
streamed to a log service. Better Stack has a free tier; any service that accepts a Render log
stream (syslog) works the same way.

**Shape of a line.** Every line on stdout is one JSON object: `ts` (UTC, millisecond ISO 8601),
`level`, `logger`, `message`, and `request_id` on every line written while a request is handled
(lines outside a request, such as start-up and the cron job, have none). A request ends with one
`app.request` line (`method`, `route` template, `status`, `duration_ms`; never the raw path or the
query). An error adds `exception` (the traceback, inside the same line). uvicorn's own messages
are JSON too; its access log is off (`--no-access-log` and the logger is disabled) because it
prints the raw URL, query string included. Passwords, tokens, cookies, CSRF values, API keys and
URL passwords are masked before the line is written, and request bodies are never logged (all in
`tests/test_observability.py`).

Setup (the Render and Better Stack screens below were **not verified**; names may differ):

- [ ] Better Stack: create a source of type **Render** (or **Syslog** if it is not listed) and
      note the endpoint (`host:port`) and source token. The token is a secret.
- [ ] Render dashboard, Workspace Settings, **Log Streams**, add the Better Stack endpoint (and
      the token where Render asks for it). One workspace stream covers the web service, the cron
      job and the database. Render forwards new logs only; it does not backfill.
- [ ] Send traffic (`curl -i https://<host>/healthz`) and check in Better Stack, Live tail, that
      lines arrive with their fields parsed (`ts`, `level`, `logger`, `message`, `request_id`). If
      the whole line arrives as one text field, add a JSON parsing step on the source.
- [ ] Note the retention of the plan used, and add an alert on `level:ERROR` if the plan allows.
- [ ] Region: pick the log service's EU region if offered. The lines hold route templates, status
      codes and request ids, no personal data by design.

**Finding every line of one request.** Every response carries `X-Request-ID` (the 500 page too).
Browser: DevTools, Network, the request, Response Headers. Terminal:
`curl -si https://<host>/healthz | grep -i x-request-id`.

- Better Stack: Live tail or Search, filter `request_id:"<the 32 hex characters>"`. All lines of
  that request appear in time order: the error with its traceback, then the closing `app.request`
  line with the status and duration.
- Render dashboard, service, Logs: search for the id as text (short retention only).
- Sentry events carry no request id (no headers are sent). To go from an alert to the logs, use
  the time of the event and the route, then look for the `ERROR` line in the log service.

- [ ] Take the id of one real request, find its lines in the log service, and record the count and
      the time in Results.

## Results

Fill in as you go. Leave "not done" until you have really checked it.

| Check | Date | Result | Notes |
|---|---|---|---|
| Blueprint created, plans and region as expected | | not done | |
| Postgres major version used | | not done | |
| First deploy healthy (`/healthz` 200) | | not done | |
| Default Postgres user can `CREATE ROLE` | | not done | paste the Shell output |
| Mode in use: separate role or single-role | | not done | section 3 |
| `X-Forwarded-For` format and rightmost hop | | not done | |
| `TRUSTED_PROXY_HOPS` confirmed value | | not done | |
| `X-Forwarded-Proto` value | | not done | |
| Host header sent by the health check | | not done | |
| Rollback: `preDeployCommand` re-runs? | | not done | |
| Rollback: env vars restored or current? | | not done | |
| Uptime monitor created, alert and recovery received | | not done | |
| Deploy-failure email received | | not done | |
| Offline page in a real browser | | not done | |
| Rollback across 0003 (`downgrade 0002`, redeploy N, upgrade again) | | not done | section 5 |
| Restore drill (managed backup restored, counts non-zero, login works) | | not done | section 8 |
| `purge-expired` hourly cron (`bruge-purge`) triggered by hand, then once on schedule | | not done | section 3 |
| `reset-password` rehearsed for a locked-out manager | | not done | section 3 |
| Plan names in `render.yaml` still accepted (web, cron) | | not done | section 1 |
| Sentry alert drill: event sent and e-mail received, no request/user data in it | | not done | section 9 |
| Failed `bruge-purge` produces one Sentry event tagged `job` | | not done | section 9 |
| Log stream to Better Stack working, one request found by `request_id` | | not done | section 10 |
| `python -m app ai-smoke` prints ok; one chat question and one screenshot work | | not done | section 2, AI settings |
