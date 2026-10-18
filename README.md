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

- Database: set `DATABASE_URL` or pass `--db <url>` (default `sqlite:///bruge.db`).
- Unknown agencies are rejected (exit code 2); create them first with `add-agency`.
- Unreadable files are reported with exit code 1.
- `--date YYYY-MM-DD` overrides the report date derived from the file.

Development checks:

```bash
uv run pytest                 # tests + 80% coverage gate
uv run ruff check .
uv run mypy
```
