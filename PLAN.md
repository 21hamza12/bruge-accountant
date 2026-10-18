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
   **In progress — detailed plan: [docs/phase2-plan.md](docs/phase2-plan.md)** (Render, manager-created
   accounts, any staff can upload).
   Before exposing uploads: `agency_id` only from the authenticated session; parse in a worker with
   memory/time limits; separate migration vs app DB roles and a per-transaction `app.agency_id`
   setting for Postgres RLS; sanitize upload filenames
   Before Phase 3: a design pass on every page (high-end look, approved from a clickable mockup).
3. **Price chat.** After each upload the app asks each staff member about the bookings they issued.
   They answer by text; the app reads the answer back and saves only after they confirm.
   Each price is stored with who gave it and when, in an append-only price history.
   Per booking it collects:
   - the **price**
   - **who it was sold to**: a walk-in client or a partner (BARAKE, BOUCHRA…). This resolves the
     open question of whether P.B is the full price or the partner fee.
   - **how it was paid**: cash, Bankily, Masrvi, Sedad, bank transfer, or on credit
   - optionally, the **client's name and phone**
   Replies are read by plain code first; AI is used only when needed (see "AI usage").
4. **Voice notes.** Speech-to-text runs only when a voice note is sent; the transcript then goes
   through the same read-back and confirm flow.
5. **Reports and the manager's daily summary.**
   - Daily, weekly and monthly tickets, revenue, profit and margin.
   - **Refunds, voids and EMDs** are matched to their original ticket and netted out.
   - **Staff performance and commissions**: per agent, plus the commission rule if one is set.
   - **Daily summary card** at the top of the manager's home page: yesterday's numbers, missing
     prices per staff member, top unpaid balances, odd prices. It is generated on the dashboard,
     not sent by WhatsApp. An optional free phone notification says when it is ready.
   - Excel/CSV export. Any export must neutralise cells starting with `=` `+` `-` `@` tab or CR
     (formula injection).
6. **Balances and payments.**
   - What each partner and credit client owes, payments recorded against it, how late each
     balance is, and a statement per partner.
   - **Daily cash check**: the expected cash from the day's cash sales compared with the counted
     cash, with any difference flagged.
7. **Other services.** Visas, hotels, Hajj/Umrah packages and insurance, entered by staff with
   their price, cost and payment method, and included in the reports and balances.
8. **Client list.** Repeat customers, their purchase history, and reminders before the Hajj and
   Umrah seasons. Built on the client details captured in Phase 3.
9. **Smart questions.**
   - Filters and buttons first, at no running cost: period, airline, staff member, partner,
     service.
   - Plain-language questions ("Bénéfice de la semaine sur Turkish ?") come as an add-on, and the
     AI is used only for that.
   - Read-only and scoped to the agency. The AI picks from fixed report functions; it never
     writes free-form SQL.
10. **Helpers and alerts.** The partner's "usual" fee as a suggestion (never applied
    automatically), odd-price flags (below cost, equal to cost, unusually high), escalation of
    unanswered questions to the manager, and push notifications.
11. **Selling to other agencies.** Arabic (RTL) and French interface, self-service onboarding
    for new agencies, and billing.

### AI usage: only when needed

- **Plain code first.** It handles the common cases at no running cost, e.g. "45000", "45 000",
  "45k", "cash", "usual".
- **AI only when that fails or is ambiguous.** Mixed languages, several numbers, a Hassaniya
  sentence, a voice note. Every AI result still goes through the staff member's read-back and
  confirm; nothing is saved on the AI's word alone.
- **The smallest model that does the job.** Inputs are short, and no passenger data goes to the
  AI beyond what the question needs.
- **AI use is logged per agency** so its cost can be measured. A cost estimate is done when
  Phase 3 is planned.

### Decisions still open (each one is due before its phase)

- **Phase 3**
  - the list of payment methods
  - whether the client name and phone are optional or required
  - partner fee vs full price (answered by the "who it was sold to" field)
- **Phase 5:** the commission rule (per ticket, % of margin, or none)
- **Phase 6:** do partners pay per ticket or at the end of the month?
- **Phase 7:** which services the agency sells, and whether each one has a cost
- **Before go-live:** hosting (free tier, laptop, or paid Render)

## Workflow

ECC: planner → tdd-guide (tests first) → code-reviewer + python-reviewer; database-reviewer for schema;
security-reviewer before logins/uploads. Fix all CRITICAL/HIGH before moving on. Conventional commits.
