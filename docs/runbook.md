# Runbook: first deploy on Render (founder checklist)

Phase 2, PR 1, step 8. Nothing here has been run yet: every box below is **unverified** until you
tick it and write the result in the "Results" section at the bottom. The code side (Dockerfile,
`render.yaml`, `/healthz`) is covered by tests and by the `docker` job in CI; the Render side can
only be checked on Render.

Prerequisites: a Render account (Hobby workspace is fine), the GitHub repo connected to Render,
and the branch you want to deploy merged or selected in the blueprint.

## 1. Create the services from the blueprint

- [ ] Render dashboard, **New**, **Blueprint**, pick this repository and branch. Render reads
      `render.yaml` and proposes a web service `bruge-accountant` and a Postgres `bruge-db`.
- [ ] Check the plan before confirming: web **Starter**, database **Basic-256mb**, both region
      **Frankfurt**. The database must show no public access (`ipAllowList: []`).
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
| `DATABASE_URL` | the **Internal** connection string of `bruge-db` | `sync: false`. For now this is the owner URL (same as `MIGRATION_DATABASE_URL`). After PR 2 it becomes the non-owner `bruge_app` role. Never paste it into chat, tickets or the repo. |
| `SENTRY_DSN` | leave empty for now | `sync: false`. Sentry is wired in PR 5. |
| `ALLOWED_HOSTS` | the real public host name(s), comma separated, e.g. `bruge-accountant.onrender.com` | `render.yaml` ships the placeholder `replace-me.onrender.com`. **Replace it.** With the placeholder every request, health check included, is rejected with 400 and the deploy fails. No `*` (production refuses it). |
| `CORS_ALLOWED_ORIGINS` | leave unset | Same-origin app, so no CORS middleware is installed. Production refuses `*`. |
| `MIGRATION_DATABASE_URL` | injected from the database | Do not edit. Used only by `preDeployCommand`. |
| `ENV`, `COOKIE_SECURE`, `TRUSTED_PROXY_HOPS` | already in `render.yaml` | `TRUSTED_PROXY_HOPS` is confirmed in section 4. |

- [ ] `ALLOWED_HOSTS` set to the real host (note the exact `*.onrender.com` name Render assigned).
- [ ] `DATABASE_URL` set (internal URL).
- [ ] If the service refuses to start, read the deploy log: the settings validator prints which
      rule failed (never the values).

Health check host: Render's health checker may call `/healthz` with a Host header that is not the
public name. If the deploy keeps failing on the health check while `ALLOWED_HOSTS` is correct,
record the Host it sends (section 4 shows how) and add it to `ALLOWED_HOSTS`.

## 3. Can the default Render Postgres user `CREATE ROLE`?

PR 2 needs a second, non-owner database role (`bruge_app`) so row-level security applies. This
check decides whether that design works on Render. Use **Shell** on the web service (the database
is not reachable from your laptop).

- [ ] Open Render Shell on `bruge-accountant` and run:

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

- [ ] Record the output in Results. If it says FAILED, stop and tell the developer: PR 2 needs a
      different plan (for example a Render-provided second user, or a different host).

## 4. How does `X-Forwarded-For` arrive, and what is `TRUSTED_PROXY_HOPS`?

The login rate limiter (PR 3) uses the client IP. Uvicorn's `--proxy-headers` only trusts
`127.0.0.1` by default, so the app itself reads the header and counts `TRUSTED_PROXY_HOPS` proxies
from the right. We must know the real hop count and whether a client can forge the left side.

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

- [ ] Deploy twice (any harmless change, version N then N+1). Confirm `/healthz` shows the new
      commit: `curl https://<host>/healthz` returns `{"status":"ok","version":"<git sha>"}`.
- [ ] In the dashboard, **Rollback** to N. Record:
  - does `preDeployCommand` (`python -m app init-db`) run again on rollback? (check the deploy
    log for "database ready")
  - does `/healthz` report version N afterwards?
  - are environment variables restored to their old values, or do they stay current? (Render
    keeps current env vars; confirm.)
- [ ] Roll forward again to N+1 and confirm `/healthz`.
- [ ] Failure path: set `ALLOWED_HOSTS` to something invalid on a scratch deploy and confirm the
      failed deploy leaves the previous version serving traffic and sends the deploy-failure email.

## 6. Uptime monitor on `/healthz`

- [ ] Create a free UptimeRobot or Better Stack monitor: HTTPS, URL `https://<host>/healthz`,
      interval 5 minutes (free tier), keyword check `"status":"ok"`, alert by email to the founder.
- [ ] Pause the service (or break `DATABASE_URL` on a scratch deploy) to trigger one alert, confirm
      the email arrives, then restore and confirm the recovery email. Record both times.
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

## Results

Fill in as you go. Leave "not done" until you have really checked it.

| Check | Date | Result | Notes |
|---|---|---|---|
| Blueprint created, plans and region as expected | | not done | |
| Postgres major version used | | not done | |
| First deploy healthy (`/healthz` 200) | | not done | |
| Default Postgres user can `CREATE ROLE` | | not done | paste the Shell output |
| `X-Forwarded-For` format and rightmost hop | | not done | |
| `TRUSTED_PROXY_HOPS` confirmed value | | not done | |
| `X-Forwarded-Proto` value | | not done | |
| Host header sent by the health check | | not done | |
| Rollback: `preDeployCommand` re-runs? | | not done | |
| Rollback: env vars restored or current? | | not done | |
| Uptime monitor created, alert and recovery received | | not done | |
| Deploy-failure email received | | not done | |
| Offline page in a real browser | | not done | |
