# Bruge Accountant

AI accountant for travel agencies: imports the daily Amadeus sales report, collects each
ticket's selling price from the staff member who issued it, and reports tickets sold,
revenue, profit and margin.

Status: Phase 1 (Amadeus import → per-staff price questions). See [PLAN.md](PLAN.md).

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                                    # install dependencies
uv run python -m app init-db                               # create the database (sqlite:///bruge.db)
uv run python -m app add-agency bruge-voyages "Bruge Voyages"
uv run python -m app import tests/fixtures/1OCT.xlsx --agency bruge-voyages
```

`import` stores the tickets and prints JSON: the upload id, how many tickets were inserted
or updated, any parser warnings, and one price question per staff sign and booking reference.
Importing the same file again updates the tickets in place and never resets their price status.

- Database: set `DATABASE_URL` (preferred) or pass `--db <url>` (default `sqlite:///bruge.db`).
  A URL given with `--db` may contain a password and ends up in your shell history and in process
  listings, so use the environment variable for anything but a throwaway SQLite file.
- All money in the JSON output is a string (`"746423"`, `"1234.50"`; `null` when unknown), never a
  float. Each question also has `known_cost_total` and `missing_cost_count`; `total_cost` is `null`
  as soon as one ticket has no cost.
- Database errors are reported as `error: ...` with exit code 1 (`run: init-db` only when the tables
  are missing); no tracebacks for expected failures.
- Limits: files up to 10 MB, 50,000 data rows, 30 MB uncompressed; larger or hostile workbooks are
  rejected with a parse error.
- Unknown agencies are rejected (exit code 2); create them first with `add-agency`.
- `bootstrap-agency <slug> <name> --manager-phone <phone> --manager-name <name>` creates an agency
  and its first manager in one transaction (`--password-stdin` reads the password from standard
  input; otherwise it is asked twice at a prompt, never taken as an argument). It prints ids only
  and exits with code 2 when the slug or the phone already exists.
- On Postgres, `add-agency` and `bootstrap-agency` need the owner connection (the app role may not
  insert agencies): run them with `DATABASE_URL` set to `MIGRATION_DATABASE_URL`.
- Unreadable files are reported with exit code 1.
- `--date YYYY-MM-DD` overrides the report date derived from the file.

## Run the web app locally

```bash
uv run python -m app init-db                       # creates sqlite:///bruge.db (or use DATABASE_URL)
uv run uvicorn --factory app.web.main:create_app --reload           # http://127.0.0.1:8000
curl -i http://127.0.0.1:8000/healthz              # 200 {"status":"ok","version":"dev"}
curl -i http://127.0.0.1:8000/nope                 # the French 404 page, with security headers
```

Configuration comes from environment variables (see `app/settings.py`): `ENV` (`development` by
default, `production` on Render), `DATABASE_URL`, `MIGRATION_DATABASE_URL` (used by `init-db` when
set), `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `COOKIE_SECURE`, `TRUSTED_PROXY_HOPS`, `SENTRY_DSN`.
List variables take `a,b` or a JSON array (`["a","b"]`). `TRUSTED_PROXY_HOPS` defaults to 0 (the
`X-Forwarded-*` headers are ignored); on Render it is 1. A wildcard CORS origin is refused in every
environment. In production the app also refuses to start without an explicit non-SQLite
`DATABASE_URL`, with insecure cookies, `http://` or `null` origins, wildcard hosts, or an empty
`ALLOWED_HOSTS`; and with `RENDER` set but `ENV` not `production`. The app is an application
factory: run it with `uvicorn --factory app.web.main:create_app`. Logs are JSON lines on stdout (one per request, secrets redacted).
Deploying to Render: [docs/runbook.md](docs/runbook.md).

Development checks:

```bash
uv run pytest                 # tests + 80% coverage gate
uv run ruff check .
uv run mypy                   # strict, over app, tests and migrations
```

Tests run on a temporary SQLite file by default. To run the whole suite against Postgres, point
`TEST_DATABASE_URL` at an empty database whose name contains `test` (each test drops and
recreates its `public` schema) and `TEST_APP_DATABASE_URL` at the same database as a second,
non-owner role. Both roles must be plain: a superuser or `BYPASSRLS` role skips every
row-level security policy (the fixtures refuse to run with one).

Two roles on a local Postgres (run as a superuser; pick your own passwords, never commit them):

```sql
CREATE ROLE bruge_owner LOGIN PASSWORD '<owner-password>';       -- runs migrations, owns the tables
CREATE ROLE bruge_app LOGIN PASSWORD '<app-password>' NOBYPASSRLS;  -- what the application uses
CREATE DATABASE bruge_test OWNER bruge_owner;
```

```bash
export TEST_DATABASE_URL='postgresql+psycopg://bruge_owner:<owner-password>@localhost/bruge_test'
export TEST_APP_DATABASE_URL='postgresql+psycopg://bruge_app:<app-password>@localhost/bruge_test'
uv run pytest --no-cov -rs
```

Row-level security (migration 0003) is tested in **both** deployment modes: the fixtures migrate
once with `APP_DB_ROLE=bruge_app` (the app connects as `bruge_app`, which holds only the granted
privileges) and once without it (single-role mode: the app connects as `bruge_owner` and only
`FORCE ROW LEVEL SECURITY` isolates the agencies). To migrate a real database yourself:

```bash
APP_DB_ROLE=bruge_app uv run python -m app init-db      # grants bruge_app its privileges (the role must exist)
uv run python -m app init-db                            # no APP_DB_ROLE: single-role mode, a warning is logged
```

Without `TEST_DATABASE_URL`, the Postgres-only tests are skipped.
`tests/test_postgres_concurrency.py` (two connections importing overlapping tickets at once)
only runs on Postgres. CI (`.github/workflows/ci.yml`) runs the full suite on SQLite (with the
coverage gate), and on `postgres:16` the Alembic upgrade/downgrade/upgrade cycle (with and
without `APP_DB_ROLE`) plus the full suite with both roles.
