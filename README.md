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
- Unreadable files are reported with exit code 1.
- `--date YYYY-MM-DD` overrides the report date derived from the file.

Development checks:

```bash
uv run pytest                 # tests + 80% coverage gate
uv run ruff check .
uv run mypy                   # strict, over app, tests and migrations
```

Tests run on a temporary SQLite file by default. To run the storage and CLI tests against
Postgres, point `TEST_DATABASE_URL` at an empty database whose name contains `test` (each
test drops and recreates its `public` schema):

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pw@localhost/bruge_test uv run pytest --no-cov tests/test_storage.py tests/test_storage_hardening.py tests/test_cli.py tests/test_postgres_concurrency.py
```

`tests/test_postgres_concurrency.py` (two connections importing overlapping tickets at once)
only runs on Postgres. CI (`.github/workflows/ci.yml`) runs both: the full suite on SQLite,
and the Alembic upgrade/downgrade/upgrade cycle plus those tests on `postgres:16`.
