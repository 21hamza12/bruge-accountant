# Phase F: partner accounts (plan)

Status: **plan for approval** (27 Oct 2026). Builds on [06-partners-and-invoices.md](06-partners-and-invoices.md)
§2 and the BADI findings in [04-findings-first-files.md](04-findings-first-files.md) §3. Replaces
the BADI-style Excel workbooks: one running account per partner, closed every half-month.

## 1. What already exists

- `partners` (name, phone, active) and the append-only fee history `partner_fees` (phase B).
- Every sale version stores `ag_kind`, `partner_id`, `fee_applied`, `pb`, `cost_total` and its
  tickets; `pb` of a partner sale = Σ cost + fee × tickets (decision 22).
- Days go `open → compared → approved → closed`; the closed day's TJQ APRES is the corrected report.
- Roles: `can_manage_partners` and `can_validate_money` = manager and accountant.

Nothing in phase F changes how a sale is entered or priced.

## 2. The account

**Sign:** a balance is **positive when the partner owes Bruge** (the BADI "Solde net" with the sign
reversed, as doc 06 says).

**Periods:** 1–15 and 16–end of month, per partner, built on demand (no row for empty periods
before the partner's first activity). States: `open` → `closed`. A closed period is frozen.

**Lines of a period** (all amounts in MRU, two decimals, `Decimal` only):

| Line | Source | Sign | Who |
|---|---|---|---|
| Ticket sale | the current version of every **active** partner sale filed on a day of the period: its `pb` (cost + fee) | + | computed, never typed |
| `ADM` | airline penalty passed on | + | accountant or manager, reason required |
| `RFND` | refund | − | accountant or manager, reason required |
| `CHANGEMENT` | change fee | + | accountant or manager, reason required |
| `AJUSTEMENT` | anything else | ± | entered by the accountant, **approved by the manager** before it counts |
| Payment | date, amount, method (the existing payment list), reference, received by | − | accountant or manager |

**Closing:** `new balance = previous balance + Σ sales + Σ manual lines − Σ payments`. Allowed only
when every day of the period is `closed` (so the ticket lines are the corrected ones). Closing
stores the totals and a snapshot of every line, so the statement never changes afterwards.

**After closing:** a sale corrected on a reopened day, or a late manual line, never edits the
closed period. The difference goes into the **next open period** as an automatic
`CORRECTION` line ("Vente #12 modifiée après clôture du 1–15 oct."), so the old statement stays
true to what was sent.

**Opening balance:** one per partner on the switch-over day, entered once by the manager (from the
partner's Excel), audited, never edited (a mistake is fixed by an `AJUSTEMENT`).

**Deadline and alerts** (decision 19): a closed period's balance is due **15 days after the period
ends**; "due soon" from 3 days before, "overdue" after. Shown to the manager and the accountant on
the home page and in the chat's answers.

## 3. Screens and documents

- **Comptes partenaires** (manager, accountant): every partner with its current balance, the open
  period's total, the oldest unpaid amount and its age; overdue first.
- **Compte d'un partenaire**: periods newest first; the open period with its lines; buttons
  « Ajouter une ligne », « Enregistrer un paiement », « Clôturer la période ».
- **Relevé** (statement) of a period, for the partner: agency header, period, tickets (date, ticket
  no, airline, passenger, PNR, cost, fee, P.T), manual lines, payments, previous and new balance.
  **Excel** (openpyxl, already a dependency) in F3; no PDF for now (answer 4).
- **Chat** (read-only, as today): "solde de BADI", "qui doit de l'argent ?", "relevé BADI 1–15 oct".

## 4. Build slices (each one: tests first, reviews incl. security for money, PR)

| Slice | Content |
|---|---|
| **F1** | Migration 0017: `partner_periods`, `partner_entries` (manual lines, corrections, opening balance), `partner_payments`; RLS FORCE and app-role grants like every tenant table (append-only where possible); the pure balance calculator with property tests (the sum always reconciles) |
| **F2** | Pages: partner accounts list, one partner's account, add a line, record a payment, manager approval of `AJUSTEMENT`, close a period; audit events; rate limits; CSRF |
| **F3** | Statement export (Excel); the agency details for the header |
| **F4** | Due-soon / overdue alerts on the home page; chat lookups for balances and statements; `RFND` lines proposed from the Amadeus report for the accountant to confirm |
| **F5** | Switch-over: enter the opening balances; a check page that compares the app's balance with the Excel's for the first period |

Estimate: F1–F2 are the bulk; the whole phase is roughly the size of phase B's sales part.

**F2 must** (left to the application code by the F1 review, 27 Oct 2026; the database already
refuses lines, payments and decisions dated in a closed period, decisions that do not fit their
line, and any change to a period but its close):

- **Roles and four eyes on decisions:** only the manager approves or rejects an `AJUSTEMENT`,
  never the person who entered it; the accountant or the manager confirms or rejects an Amadeus
  `RFND`.
- **Active users only:** whoever enters, decides, receives or records must be an active user of
  the agency.
- **Lock the partner before the totals:** the close first takes
  `pg_advisory_xact_lock(170017, mod(partner_id, 2147483647))` (EXCLUSIVE, the key of migration
  0017's insert guard, which takes the shared one), and only then reads the sales, lines and
  payments. Every insert in flight has then committed and is counted; every later one sees the
  closed period and is refused. Two partners sharing a key only wait for each other.
- **Close exactly one row:** the close takes the period row `FOR UPDATE`, updates it and checks
  that exactly one row changed; anything else is a refusal, not a silent success.
- **No close while lines wait:** a period with a pending `AJUSTEMENT` or Amadeus `RFND` cannot be
  closed until each one is decided: once it is closed, no decision can ever be inserted for
  them (the database refuses anything dated on or before a closed period's end).
- **Late lines go forward:** nothing can be dated on or before the end of the latest closed
  period, not even in a half-month that never had a row; a late line is written in the open
  period (a `CORRECTION`, or a dated-today line whose reason names the old date).
- **A `CORRECTION` matches its sale:** the sale it points at is a sale of the same partner.
- **Reasons are real text:** a reason made of spaces, or padded to reach 5 characters, is refused
  (`app.reasons`), not only by the length CHECK.
- **Double clicks:** each form post carries a `request_key`; a repeated key returns the line
  already written instead of a second one.

### F2 as built (27 Oct 2026)

Service `app/partner_book.py`, pages `app/web/routes/partner_accounts.py` (`/partner-accounts`),
the correction hook in `app/sales.py`. Each "F2 must" above is enforced there and tested in
`tests/test_partner_book.py` and `tests/test_partner_account_pages.py`. Choices made where the
plan left room, and why:

- **Built on demand = written on close.** A `partner_periods` row is only inserted by the close
  (then closed in the same transaction). Open periods are computed from the live data each time
  they are shown, so a page view never writes, and the database holds no open row that could
  drift. The periods shown run from the first one to the one holding today, in order.
- **First period and opening balance** (review, 27 Oct). The `OPENING` line's date is the
  switch-over day, and its amount is the spreadsheet's balance at the end of the day before.
  Nothing dated before the switch-over day is read, accepted or corrected (sales, lines,
  payments), even inside the first half-month: an opening on the 20th leaves out a sale of the
  17th and counts one of the 21st. The first period is the half-month of the opening; without
  one, the earliest closed period, else the earliest activity.
- **The close also needs the period to be over** (its last day before today). Then a
  `CORRECTION` dated today always lands after every closed period.
- **The close locks the report days of the period `FOR SHARE`** after the advisory lock, so no
  day is reopened while the totals are read.
- **Corrections also cover a sale created late** on a reopened day of a closed period
  ("Vente #N ajoutée après la clôture ..."), not only edits, voids and moves; without that the
  sale would be counted nowhere. A correction carries no request key: it is written in the
  sale's own transaction, under the sale's own locks.
- **Idempotency of decisions and closes.** The decisions table has no `request_key` column: a
  decision is idempotent by its own unique key (one per line): the same decision by the same
  person returns the stored one, anything else is "déjà traitée". The close stores its request
  key in the snapshot: the same form posted twice returns the closed period.
- **An `AJUSTEMENT` entered by a manager needs another manager** (four eyes); the accountant never
  decides one. Review, 27 Oct: an `AJUSTEMENT` is refused at entry when no other active user
  could approve it (« Aucun autre gérant ne peut approuver cette ligne : demandez à la comptable
  de la saisir, le gérant l'approuvera. »), so a manager alone never waits on their own line.
  A pending line says who may decide it.
- **"Plus ancien impayé"** is first in, first out, capped by what is owed now (review, 27 Oct):
  `owed = max(0, balance)`; nothing is shown when `owed` is 0; otherwise what a closed period
  still lacks is `min(closing − payments dated after its end, owed)`, so a refund, a credit or
  a negative adjustment after the close pays the oldest debt too. The oldest period still
  lacking something is shown with its due date (end + 15 days) and its age in days since its
  end.
- **A request key replayed with other values** (kind, amount, date; for a payment amount,
  method, date) is refused with a French message, and the form comes back with a fresh key
  (review, 27 Oct). The same form posted again returns the stored row, and the page says so
  (« Cette ligne était déjà enregistrée »).
- **Database refusals** (review, 27 Oct): only the closed-period trigger (its message, and on
  Postgres sqlstate `23514`) is « la période vient d'être clôturée »; any other refusal is a
  generic « Enregistrement refusé », logged without data. A refused close `UPDATE` is « la
  période a changé pendant la clôture »; a refused correction during a sale change is a French
  sale error, never a 500.
- **Cost of the pages** (review, 27 Oct): the snapshot column is deferred everywhere but the
  period page; the list reads every partner of the agency in a constant number of queries
  (about eight, whatever the number of partners; a test pins it), computing live only the open
  periods after each partner's last closed one. Reads have their own rate limit
  (`PARTNER_ACCOUNT_READ_USER`, 600 an hour per user).
- **Corrections take the advisory lock only when needed**: the latest closed end is read first
  without the lock; the lock is taken only when a closed period could cover the sale's day, and
  everything is read again under it.
- **Amounts are typed positive**: the kind gives the sign (`RFND` negative), and an
  `AJUSTEMENT` has a direction (« le partenaire doit plus / moins »).
- **Advisory lock** via `func.pg_advisory_xact_lock(_shared)` with bound, cast parameters (no
  SQL text), so `tests/test_code_guards.py` needed no allowlist entry.

Postgres-only (left to CI): the advisory locks and their interplay with the insert triggers, the
`FOR SHARE` / `FOR UPDATE` row locks, the closed-period triggers raising `check_violation`
(SQLite's triggers are exercised by a test on a migrated database), the `closing_reconciles`
CHECK, and the row-level security of the four tables under the app role.
`tests/test_partner_book_postgres.py` covers these on a server, in both deployment modes: the
whole flow as the application role (close, line, decision, payment, sale correction), the
trigger's refusal shown in French, and a two-session test where a close waits for a line in
flight (shared advisory lock held by the insert trigger) and counts it.

## 5. Owner's answers (27 Oct 2026)

1. **Payments:** recorded by the **accountant and the manager only**. Agents do not record partner
   payments.
2. **Which days count:** a partner sale belongs to the period of the day it is **filed under**
   (the ticket's issue day since PR #38).
3. **Refunds of a partner ticket:** when the Amadeus report shows an `RFND` for a partner's ticket,
   the app **proposes** the `RFND` line on that partner's account and the **accountant confirms**
   it (amount checked) before it counts. Until then it is shown as « à confirmer ».
4. **Statement format:** **Excel only** for now; PDF later if asked (no new library in F3).

Still open (not blocking F1–F2):

5. **Statement header:** the agency details (NIF, address, phones, bank account, Bankily/Masrvi/
   Sedad numbers) to print. Needed by F3.
6. **Switch-over day and opening balances:** which date, and who reads each partner's Excel balance
   that day. Needed by F5 only.
7. **Fee exceptions** (0, 100, 200, 600 in BADI): proposed default, the existing one-off override
   with a reason (no change).
