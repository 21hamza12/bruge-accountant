# Bruge Accountant — Implementation Plan

AI accountant for travel agencies. Pilot customer: Bruge Voyages (Nouakchott, MRU).
Multi-agency from day one: every record carries `agency_id`.

## Key constraint

Selling prices are not in Amadeus and not fixed — staff decide them at the counter.
No pricing rules, markup grids or price predictions. Prices come only from staff,
confirmed by them, stored with who gave them and when.

## Stack

Python 3.12+ · FastAPI · SQLAlchemy 2 + Alembic · PostgreSQL (SQLite for Phase 1 dev/tests) · pytest · openpyxl.
Later UI: server-rendered Jinja + htmx, PWA (manifest + service worker + Web Push), RTL via `dir="rtl"`, Babel i18n.
Deploy: one Docker container + managed Postgres.

## Phase 1 — Amadeus import → per-staff price questions (current)

1. **Parser** `app/parsing/amadeus.py` — `parse_report(file) -> ParsedReport(header, tickets, warnings)`
   - locate header row by column names; read only Amadeus columns A–H (SEQ NO, A/L, DOC NUMBER, TOTAL DOC, PAX NAME, AS, TRNC, RLOC)
   - ignore hand-typed columns I–N entirely
   - blank rows skipped; formula costs read from cached values (missing cache → flagged, never guessed)
   - text kept as text (leading zeros); `(INF)` / `(CHD)` suffix → `pax_type`
   - non-TKTT types imported with `unknown_trnc` flag, never dropped
   - report year derived from "Current date" (handles 31DEC → 1 JAN rollover)
2. **Storage** `app/models.py`, `app/repository.py`, Alembic migration
   - `agencies`, `uploads`, `tickets`; UNIQUE(agency_id, ticket_no); index (agency_id, report_date, staff_sign)
   - re-upload upserts Amadeus fields only, never touches price fields
   - `price_status = awaiting_price`
3. **Question generator** `app/questions.py` — per staff sign, grouped by booking ref (RLOC);
   one question per group with passengers, ticket count, airlines (a group can mix airlines), cost
4. **CLI** `python -m app import <file.xlsx> --agency <slug>` → JSON per staff.
   (No HTTP endpoint until Phase 2 adds auth — an unauthenticated upload endpoint would be an IDOR by design.)

### Acceptance (tests/fixtures/1OCT.xlsx)

- 48 tickets; total cost 1,118,857 MRU
- report range "01OCT", generated 02-OCT-2026
- 0004SV: 34 tickets, 25 groups, cost 746,423
- 0009ER: 14 tickets, 8 groups, cost 372,434
- 33 groups total
- INF: PAX02/GIVEN02, PAX37/GIVEN37; CHD: PAX03/GIVEN03
- columns I–N have no effect on output
- importing the same file twice → still 48 tickets

## Decisions

- **report_date is fixed at first import.** A ticket belongs to the day it first appeared; a later
  report containing the same ticket refreshes its Amadeus fields but never moves it to another day.
- **Duplicate DOC NUMBER inside one file:** the first row is kept, the ticket gets the
  `duplicate_in_file` flag (so it shows up in the question) and a warning is recorded.
- **Blank RLOC:** the ticket is flagged `missing_booking_ref` and becomes its own question; blank
  RLOCs are never merged into one group. A blank staff sign is flagged `missing_staff_sign`.
- **Money in JSON is always a string** (`"746423"`, `"1234.50"`; whole amounts have no decimals);
  `null` stays `null`.
- Costs are stored with 2 decimals (half-up; `cost_rounded` flag when digits were lost); a cost of
  10^12 or more is treated as missing.

## Later phases (not built yet)

2. Auth, agencies, users, staff-sign ↔ person mapping, Postgres, upload API, first deploy.
   Before exposing uploads: `agency_id` only from the authenticated session; parse in a worker with
   memory/time limits; separate migration vs app DB roles and a per-transaction `app.agency_id`
   setting for Postgres RLS; sanitize upload filenames
3. Text price chat (read-back + confirm; who/when stored in append-only price history).
   Open question: partner sales (P.B = fee only) vs direct sales (P.B = full price)
4. Voice notes (speech-to-text → same confirmation flow)
5. Daily / weekly / monthly reports: tickets, revenue, profit, margin.
   Any Excel/CSV export must neutralise cells starting with `=` `+` `-` `@` tab or CR (formula injection)
6. "Usual" partner fee suggestion (never auto-applied), odd-price flags, manager escalation, push notifications
7. Arabic / French UI, onboarding other agencies, billing

## Workflow

ECC: planner → tdd-guide (tests first) → code-reviewer + python-reviewer; database-reviewer for schema;
security-reviewer before logins/uploads. Fix all CRITICAL/HIGH before moving on. Conventional commits.
