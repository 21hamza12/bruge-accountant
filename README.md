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
- Non-interactive password: `--password-stdin` (the only way from a pipe or script on Windows).
  A command line error prints the usage, never the tokens typed; `-p` and `--password...` are
  refused. Ctrl-C at the prompt exits with code 130.
- `grant-app-role <role>` (owner connection) re-applies the app role's privileges, idempotently,
  for a role created after migration 0003; it refuses a superuser or BYPASSRLS role.
- `purge-expired` deletes expired sessions and temporary passwords and audit events older than
  400 days, and prints the counts.
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
set), `ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `COOKIE_SECURE`, `TRUSTED_PROXY_HOPS`, `SESSION_IDLE_HOURS` (default 12),
`SESSION_ABSOLUTE_DAYS` (default 7), `DB_POOL_SIZE` (default 5) and `DB_MAX_OVERFLOW` (default 5),
`MAX_REQUEST_BYTES` (default 10 MB + 64 KB), `PARSE_TIMEOUT_S` (default 30, at least 5),
`PARSE_MEMORY_MB` (default 768, Linux only), `PARSE_CONCURRENCY` (parses at once, default 2),
`PARSE_QUEUE_WAIT_S` (how long a parse waits for a free slot before the 503, default 5),
`SENTRY_DSN`.
List variables take `a,b` or a JSON array (`["a","b"]`). `TRUSTED_PROXY_HOPS` defaults to 0 (the
`X-Forwarded-*` headers are ignored); on Render it is 1. A wildcard CORS origin is refused in every
environment. In production the app also refuses to start without an explicit non-SQLite
`DATABASE_URL`, with insecure cookies, `http://` or `null` origins, wildcard hosts, or an empty
`ALLOWED_HOSTS`; and with `RENDER` set but `ENV` not `production`. The app is an application
factory: run it with `uvicorn --factory app.web.main:create_app`. Logs are JSON lines on stdout (one per request, secrets redacted).
Deploying to Render: [docs/runbook.md](docs/runbook.md).

## Try the login locally

Accounts are created by a manager (phone plus password). Locally, create the first agency and
manager from the command line, then start the app. Everything below uses placeholders: pick your
own values and never commit them.

```bash
uv run python -m app init-db
printf '%s
' '<a password of at least 10 characters>' | uv run python -m app bootstrap-agency     my-agency "My Agency" --manager-phone <8-digit phone> --manager-name "<your name>" --password-stdin
COOKIE_SECURE=false uv run uvicorn --factory app.web.main:create_app     # http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000/login` and sign in with that phone and password.

- `COOKIE_SECURE=false` is for plain-http local use only. Browsers drop a `__Host-` cookie that is
  not `Secure`, so with it off the cookies are named `session` and `login_csrf` instead of
  `__Host-session` and `__Host-login_csrf`. Production refuses to start with it off.
- The CSRF check compares `Origin` (or `Referer`) with the address you typed, so browse to the
  same host name the server is listening on. Behind a TLS-terminating proxy set
  `TRUSTED_PROXY_HOPS` so the app sees `https`.
- As a manager, open **Gérer l'équipe** (`/staff`) to add a user: the temporary password is shown
  once, valid 30 minutes and usable once, and the new user must choose their own at first login.
  A manager can issue a new one for **staff** or an **accountant** from the user's page; never for
  another manager. Roles: manager (Responsable), accountant (Comptable: sees every sign like a
  manager, no staff management) and staff (Employé). Every role decision lives in
  `app/permissions.py`.
  A locked-out manager is reset by the owner: `python -m app reset-password --phone <phone>`
  (docs/runbook.md).
- Five failed sign-ins for one phone (or a hundred from one address) in 15 minutes show the
  "too many attempts" page. Only failures count: a successful sign-in clears the phone's count.
  The counters live in the database, so every server instance shares them.

## Try uploads locally

Any signed-in user (staff or manager) can send the daily Amadeus report. Use the committed
synthetic fixture, never a real report (those hold passenger names). Placeholders only; pick your
own values and never commit them.

```bash
uv run python -m app init-db
printf '%s\n' '<a password of at least 10 characters>' | uv run python -m app seed-demo --fixture tests/fixtures/1OCT.xlsx --password-stdin
COOKIE_SECURE=false uv run uvicorn --factory app.web.main:create_app     # http://127.0.0.1:8000
```

`seed-demo` creates two agencies for trying the app. It refuses when `ENV=production` (always),
when any of the slugs `demo-a`, `demo-b` or the four phones below is already taken (it names each
one that collides), and when the database is neither SQLite nor on this machine, unless you pass
`--i-know-this-is-not-production`. Everything is created in one transaction: it is all or nothing.
One password, read from standard input and never printed or accepted as an argument, is shared by
all four accounts:

| Phone | Agency | Role |
|---|---|---|
| `+22290000001` | `demo-a` | manager |
| `+22290000003` | `demo-a` | staff, linked to sign `0004SV` |
| `+22290000004` | `demo-a` | staff, linked to sign `0009ER` |
| `+22290000002` | `demo-b` | manager (the fixture is already loaded here) |

Sign in as the `demo-a` manager, open **Envoyer un rapport** (`/uploads/new`) and choose
`tests/fixtures/1OCT.xlsx`: the result page shows 48 tickets added. Send it again and it shows 0
added and 48 updated. **Questions de prix** (`/questions`) lists the day's price questions: the
manager sees both signs (34 tickets in 25 questions for `0004SV`, 14 in 8 for `0009ER`), a staff
member only the sign linked to them. Prices are not collected yet.

- Parsing runs in a throwaway child process (`app/parsing/sandbox.py`, `python -I` with a
  minimal environment: it cannot read `DATABASE_URL` or any other secret of the web process). One
  deadline, `PARSE_TIMEOUT_S` (default 30) seconds, covers it; on Linux it is also capped at
  `PARSE_MEMORY_MB` (default 768) of address space, no new processes and no file writes. Windows
  has none of those caps; the app logs a warning and keeps the timeout. At most `PARSE_CONCURRENCY`
  parses run at once (default 2; each child needs about 31-49 MB resident, so size it against the
  instance memory). A parse that finds every slot taken waits up to `PARSE_QUEUE_WAIT_S` seconds,
  then the user gets the "service occupe" page (503, `Retry-After`) and the attempt is not counted.
- Files over 10 MB get the 413 page; the limit is applied before the form is read, and the form is
  read only after login, Origin, CSRF (header) and the rate limits have passed.
  Ten uploads an hour per user and thirty per agency, then 429.
- Staff see only the uploads they made, without the warnings (those quote cell values); managers
  see every upload and its warnings.
- HTTP end-to-end journeys run against a live server (skipped when `E2E_BASE_URL` is unset):

```bash
E2E_BASE_URL=http://127.0.0.1:8000 E2E_PASSWORD='<the demo password>' uv run pytest tests/e2e --no-cov -rs
```

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

Each test on Postgres takes a session-level advisory lock first, so two test runs against the
same server take turns instead of wiping each other's schema.

Without `TEST_DATABASE_URL`, the Postgres-only tests are skipped.
`tests/test_postgres_concurrency.py` (two connections importing overlapping tickets at once)
only runs on Postgres. CI (`.github/workflows/ci.yml`) runs the full suite on SQLite (with the
coverage gate), and on `postgres:16` the Alembic upgrade/downgrade/upgrade cycle (with and
without `APP_DB_ROLE`) plus the full suite with both roles.
