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

### F3 as built (27 Oct 2026)

Service `app/partner_statement.py` (the workbook), `app/agency_details.py` (the header), pages
`app/web/routes/partner_statements.py` (`GET /partner-accounts/{id}/periods/{start}/statement.xlsx`)
and `app/web/routes/agency_details.py` (`/agency-details`), migration 0018. Tests:
`tests/test_partner_statement.py`, `tests/test_agency_details.py`, `tests/test_migration_0018.py`,
and the `agency_details` cases of `tests/test_rls.py` (Postgres, CI). Choices made where the plan
left room:

- **Agency details in their own table** (`agency_details`, one row per agency, migration 0018):
  the app role only reads `agencies`. RLS ENABLE + FORCE with `tenant_read`, `tenant_insert` and
  `tenant_update`; grants SELECT, INSERT, UPDATE; never deleted (no policy, no grant; a SQLite
  trigger). Every field is optional with a length CHECK. The values are made-up placeholders
  until the owner supplies them (answer 5 is still open); the page works without any.
- **Who:** the manager edits, the accountant reads (« Seul le gérant modifie ces
  coordonnées. »), staff get a 403. The sidebar link « Coordonnées de l'agence » is for both.
  Phones are checked loosely (digits, `+ ( ) . -` and spaces, several numbers separated by
  `/ , ;`, 8 to 15 digits each); Bankily / Masrvi / Sedad 4 to 15 digits; the e-mail by its
  shape only. The audit event `agency_details_saved` lists the names of the fields that changed,
  never a value; a save that changes nothing writes nothing (« Aucune modification »). The row
  keeps the `request_key` of its last save: the same form posted twice saves once, the same key
  with other values is refused and the form comes back with a fresh key.
- **The header** is built from the details: French name (else `agencies.name`), Arabic name,
  address, « Tél. · E-mail », NIF, « Banque · RIB », « Bankily · Masrvi · Sedad »; a missing detail
  is left out. Each line starts with its label, so a typed value can only start a cell on the
  name, Arabic name and address lines, and those are neutralised like any typed text.
- **A closed statement reads only its frozen row:** the totals from the columns written by the
  close, the lines from the snapshot (`from_snapshot`); the snapshot must add up to the frozen
  totals (Σ P.T = sales, Σ counted lines = lines, Σ payments = payments, and the balance
  equation), otherwise the download fails rather than print a wrong statement. A test edits the
  sale after the close and checks every cell of the closed statement is unchanged, and another
  replaces the live builder by one that fails. The partner's **name** is the current one (it is
  not in the snapshot; it is not a figure).
- **An open period** is built from the live account, title « RELEVÉ DE COMPTE PARTENAIRE —
  PROVISOIRE », status « PROVISOIRE : période ouverte, les montants peuvent encore changer », and
  `-PROVISOIRE` in the file name. A pending `AJUSTEMENT` is listed as « en attente (non comptée) »
  and a rejected one as « rejetée (non comptée) ».
- **Layout:** one sheet « Relevé »: agency header, title, partner, period, status, issue date,
  currency; then the tickets (one row per ticket: date of the sale's day, ticket number, airline
  name from `app/airlines.py`, passenger, PNR, cost, fee per ticket, P.T = cost + fee) with their
  totals; the manual lines (date, kind, reason, state, signed amount); the payments (date,
  method, reference, amount; not who received it); the summary (previous balance, tickets,
  lines, payments, new balance), the meaning of the balance and the sign rule (« Solde positif :
  le partenaire doit à l'agence ; solde négatif : l'agence doit au partenaire. »). Nothing is
  frozen (see the review fixes below). Dates are
  real date cells shown `dd/mm/yyyy` (the TJQ APRES writes them as text; a date cell sorts and
  filters). Amounts are `Decimal` written as numbers, `#,##0.00`, like the TJQ APRES. Printed
  landscape, one page wide.
- **Formula injection:** every typed text goes through `export_apres.neutralise` and is forced to
  a string cell (no new sanitiser was needed). A test writes a partner named `=HYPERLINK(...)`, a
  reason `+1+1 frais` (a reason needs 5 characters, so not `+1+1` alone), a reference
  `=cmd|...`, and agency details `=1+1` / `@SUM(...)`, and finds no formula cell and no `<f>` in
  the sheet XML.
- **Download:** manager and accountant (`permissions.can_export_partner_statement`); a partner of
  another agency, or a period that does not exist, is a 404. Rate limit `PARTNER_STATEMENT_USER`
  (60 an hour per user); saves of the details `AGENCY_DETAILS_WRITE_USER` (20 an hour). Audit
  `partner_statement_downloaded` with `partner_id`, `period_start`, `period_id` (None when open)
  and `provisional`. File `releve-partenaire-<id>-<start>-<end>[-PROVISOIRE].xlsx`;
  `Content-Disposition: attachment`, `Cache-Control: no-store`, `nosniff`. The statement route
  is its own router, so the F2 router keeps its exact list of routes.
- **Buttons:** « Télécharger le relevé (Excel) » on every period page, and on the account page for
  the first open period (the one the page shows); a closed period's statement is downloaded from
  its own page.

Review fixes (27 Oct, after the two reviews of 95df1af):

- **A corrupt snapshot** (`StatementError`) is a deliberate **409** page with the French message,
  never a 500 and never a file; one log line `partner statement refused` carries only
  `partner_id` and `period_id`. Nothing is audited as downloaded.
- **Invisible characters:** every text cell is cleaned by `clean_text` (bidi overrides,
  zero-width characters, the byte-order mark) before `neutralise`, and `neutralise` (shared with
  the TJQ APRES) now also skips U+2000–U+200B, U+3000 and U+FEFF before looking for a formula
  start (`　=1+1` gets its apostrophe).
- **No frozen pane:** freezing under the ticket header pinned the whole agency header (16 rows)
  too. No print title rows either: the later sections (lines, payments) have other columns, so a
  repeated ticket header would mislead on paper.
- **One snapshot read:** `statement_for` reads the account with every snapshot deferred, then the
  wanted period's snapshot alone (`select(PartnerPeriod.snapshot)` by agency and id); a test
  counts one such statement with two closed periods.
- **A closed statement is never « PROVISOIRE »:** a closed row without `closed_at`, or a closed
  statement without its close date, is a `StatementError` (the 409 page).
- **Phones and wallet numbers** are normalised before they are checked and stored: NFKC
  (full-width digits, no-break spaces), Arabic-Indic and Extended Arabic-Indic digits to 0–9,
  every dash (U+2010–U+2015, U+2212) to `-`. `٢٢ ٣٣ ٤٤ ٥٥` is stored as `22 33 44 55`.
- **Concurrent first saves:** the save that loses the insert reads the row again; the same key
  and the same values are a success (one audit event), anything else is « Les coordonnées ont
  changé pendant l'enregistrement : rechargez la page. ».
- **Non-text values** given to `clean_values` (a number, a list, bytes, a boolean) are refused
  (« Texte invalide. »).
- **Read limit** on `GET /agency-details`: `AGENCY_DETAILS_READ_USER`, 300 an hour per user.
- **More exact-cell tests:** a sale of two tickets (fee × 2), an unknown airline (« Cie 999 »), a
  counted refund and a rejected adjustment, a payments-only period opening on the previous closed
  balance and ending negative, a zero balance (« Compte soldé. »), an empty period, and a partner
  name starting with `=`.

Postgres-only (left to CI): the row-level security, policies and grants of `agency_details` in
both deployment modes (`tests/test_rls.py`: isolation, insert for another agency refused, update
of the own row only, no DELETE, exact policies and grants).

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

## F4 as built (28 Oct 2026)

Services `app/partner_alerts.py` (home page and chat), `app/partner_refunds.py` (RFND proposals),
the three chat lookups in `app/chat/tools.py`, their reading in `app/chat/router.py` and their
model allowlist in `app/chat/ai_prompt.py`; the block `app/web/templates/_partner_alerts.html` on
the manager / accountant home page. Tests: `tests/test_partner_alerts.py`,
`tests/test_partner_alert_pages.py`, `tests/test_partner_refunds.py`. Choices made where the plan
left room:

- **One alert per partner: its oldest unpaid closed period** (F2's first in, first out
  `oldest_unpaid`), with what it still lacks, its due day and the days late. « Bientôt dû » from
  3 days before the due day to the due day included, « en retard » from the day after
  (`partner_ledger.alert_level`, pinned on the day boundaries). A partner whose oldest debt is not
  due yet is not listed. Overdue first (longest late first), then due soon (nearest due first).
- **The home page reads it through `account_rows`** (F2's batched reads): twelve statements for
  the whole page whatever the number of partners (a test compares 2 and 12 partners). The block
  is shown only when something is overdue, due soon or waiting, with three counts (« En retard »,
  « Bientôt dus », « Remboursements à confirmer »): these are the dashboard numbers; no other
  morning summary exists in the application. Staff never get it (the service returns nothing for
  them, and their home page is another template). The home page now reads the application clock
  for "today".
- **Chat, read-only, manager and accountant only** (`can_manage_partners`): `partner_balance`
  (« solde de … »: balance, who owes whom, the oldest unpaid with its due day and state),
  `partners_owing` (« qui doit de l'argent ? », « impayés », « partenaires en retard »: overdue
  then due soon, and the count of refunds to confirm) and `partner_statement` (« relevé … » with
  « 1-15 octobre », « 16 au 31 oct 2026 », a date, or nothing for the latest closed period: the
  totals, a link to the period page and a button to its Excel file,
  `/partner-accounts/{id}/periods/{start}/statement.xlsx`, the F3 route; the file is never sent
  through the chat). Staff get the chat's usual « Cette demande n'est pas disponible pour votre
  rôle. », and the model is never offered these tools for them.
- **Partner names** are a new argument kind `partner` (text, spaces collapsed, at most 80
  characters), matched in Python by `pnr_sales.find_partners` against the agency's active
  partners and approved aliases (the paste screen's rule): the text never reaches a query. No
  match: « Aucun partenaire actif ne correspond à ce nom. » (nothing typed is echoed); several:
  « Plusieurs partenaires correspondent à ce nom : lequel ? » with their names as links. Another
  agency's partner is never a candidate. The router passes the folded words after « solde » /
  « relevé » (fillers such as « de », « du » dropped; for a statement, up to the period).
- **What the model sees** is unchanged in kind: `sanitize` gained the three tools' fields (ids,
  counts, ISO dates, a level among `overdue` / `due_soon` / `none`, amounts as `-?digits.dd`
  strings); names stay in the lines. `grounded` is unchanged, so a model sentence with « MRU » or
  a reformatted amount is replaced by « Voici ce que j'ai trouvé : » and the lookup's own lines.
- **RFND proposals are made right after the comparison, in a transaction of their own**
  (`partner_refunds.propose_after_comparison`, review, 28 Oct). The upload and « Relancer la
  comparaison » commit the comparison first, then propose from the latest report of the day.
  The proposal takes the partner's shared advisory lock; inside the comparison (which holds the
  day locks) it could deadlock with a period close (advisory lock, then the days `FOR SHARE`).
  A failure there is rolled back and logged with ids only (agency, report day, error class); the
  comparison stays, and the next upload or run proposes again (idempotent). The reruns of the
  validation screens (`approvals`) do not propose: their report is the one already compared. A
  report imported without a comparison (the CLI) proposes at its first comparison.
  `created_by_user_id` is the person whose upload or run proposed the line (possibly an agent: the
  decision is still the accountant's or the manager's).
- **A deadlock or a serialization failure** (SQLSTATE `40P01` / `40001`, `errors.is_lock_conflict`)
  of the period close or of « Relancer la comparaison » is « Une autre opération était en cours au
  même moment : rien n'a été changé, réessayez. » (409), never a 500. The upload already logs any
  failure of its comparison and says it must be run again.
- **Idempotency, once per ticket for ever:** a ticket that already has an Amadeus `RFND` line in
  the agency (pending, confirmed or rejected) is skipped, and the row carries the deterministic
  `request_key` `amadeus-rfnd-<ticket>`, unique per agency in the database; a racing second
  insert is rolled back to its savepoint. So a later partial refund of the same ticket, or a
  corrected amount, is never proposed: the accountant enters it by hand (a manual `RFND`, or an
  `AJUSTEMENT` approved by the manager), and rejects a proposal whose amount is wrong.
- **No backdating:** under the partner's shared advisory lock, the line is dated on the report day
  when it is after the latest closed period's end and not after today, else today (a closed
  period always ends before today). A report day before the switch-over day proposes nothing. A
  database refusal is logged (ids only).
- **Amount:** the report line's TOTAL DOC, negative with two decimals at most; missing, zero,
  positive or odd: nothing proposed, a warning with ids only (report day, sale, partner). It is
  shown as « montant Amadeus (à vérifier) » with the ticket number wherever the line waits for a
  decision (the account page, the home alerts), and the reason says « … proposé depuis le rapport
  Amadeus du <jour> : montant Amadeus, à vérifier ».
- **A refund or a cancellation never moves a sale** (review, 28 Oct): a report line with TRNC
  `RFND` or `CANX` whose ticket is held by a sale of an earlier open day is ELSEWHERE (blocking,
  naming the sale's day), as for an earlier closed day, never a move: the sale keeps its filing
  day and its partner period (`comparison.NEVER_MOVES`, used by `compare` and by
  `flags._other_days`). Issue lines (`TKTT`, `EMDA`, `EMDS`, any other) move as before. Before
  this, a `CANX` line moved the sale and then flagged a TRNC difference.
- **Audit:** `partner_refund_proposed` is `audit.PARTNER_REFUND_PROPOSED`, with the other event
  names.

Postgres-only (left to CI): the proposal's shared advisory lock, now taken after the comparison's
commit; the real deadlock and serialization refusals (the tests raise a simulated 40P01 / 40001);
the unique request key under two truly concurrent comparisons; and the closed-period trigger
refusing a proposal.

### F5 as built (28 Oct 2026)

Service `app/partner_opening.py`, pages `app/web/routes/partner_openings.py` (`/partner-openings`),
the opening form on the account page (`app/web/routes/partner_accounts.py`,
`partner_accounts/account.html`), and `partner_book.accounts_of` (several accounts in a constant
number of queries). Tests: `tests/test_partner_opening.py`, `tests/test_partner_opening_pages.py`.
No migration: migration 0017 already holds one `OPENING` per partner, append-only. Choices made
where the plan left room:

- **Who:** the manager enters (`permissions.can_enter_partner_opening`), the manager and the
  accountant check (`can_check_partner_switchover`), staff get a 403. An inactive manager is
  refused. A refusal is a `permission_denied` event.
- **The form** is on the partner's account page while the partner has no `OPENING` and no closed
  period (and alone at `GET /partner-openings/{id}/new`, where a refused post comes back with its
  values). Fields: the switch-over date (default today, never in the future), the spreadsheet's
  balance at the end of the day before with « Le partenaire doit à l'agence » (positive) or
  « L'agence doit au partenaire » (negative), zero allowed and needing no sign, and a source note
  (`app.reasons`, e.g. « solde du fichier Excel au 31/10 »). Once written the account page shows
  it with « Saisi une seule fois, il ne se modifie pas : une erreur se corrige par un
  ajustement. ».
- **No rewriting of history:** refused when the partner already has an opening (« déjà un solde
  d'ouverture … une erreur se corrige par un ajustement ») and when **any** period of the
  partner is closed, whatever the date (« Une période de ce partenaire est déjà clôturée … »).
  That covers "a line, payment or sale dated on or after the chosen date already counted in a
  closed period": with no closed period nothing has been counted in one. The check runs under
  the EXCLUSIVE advisory lock the close takes (Postgres), so no period closes and no line lands
  between the check and the insert; the unique index and the closed-period trigger refuse the
  same on their own, and a lost race is reported with the French message, not « Enregistrement
  refusé ».
- **What it leaves out:** as F2 decided, nothing dated before the switch-over day is counted.
  Lines or payments already typed with an earlier date are not refused: they are in the
  spreadsheet's balance, so leaving them out is right (the form says so).
- **Double clicks:** the `request_key` is stored on the line; the same form posted again returns
  it (« Ce solde d'ouverture était déjà enregistré »); the same key with another amount, sign,
  date or partner, or a key used by another line, is refused and the form comes back with a
  fresh key.
- **Audit** `partner_opening_entered`: `partner_id`, `entry_id`, `amount`; never a name, the
  date or the note. **Rate limit** `PARTNER_OPENING_WRITE_USER` (60 an hour per user); the
  check page and its comparisons count against `PARTNER_ACCOUNT_READ_USER`. CSRF on the router.
- **« Vérification du passage »** (`/partner-openings/check`): for each partner with an opening,
  the opening (date and amount), the app's balance at the end of the half-month holding the
  switch-over day (« provisoire » until that period is closed), and a field for the
  spreadsheet's balance at that date with the same sign choice. The answer is « Identique » or
  « Écart de X MRU » with which side counts more. The typed value is neither stored nor audited.
- **Bulk entry** (`/partner-openings`, manager): every active partner without an opening, one
  form each, posted one at a time (back to the list with « Solde d'ouverture enregistré. »).
  Links from « Comptes partenaires ».

Postgres-only (left to CI): the EXCLUSIVE advisory lock against a close or a line in flight, and
the row-level security of the `OPENING` insert under the app role (the existing
`partner_entries` policies; no new table, grant or policy).

Review fixes (28 Oct, after the review of 8addfad):

- **H1, a floor on the date.** The switch-over day may not be more than 400 days back
  (`partner_opening.MAX_DAYS_BACK`; « … ne peut pas remonter à plus de 400 jours : au plus tôt
  le … Vérifiez l'année. »), and the date inputs carry `min=`. Defence in depth, migration 0019:
  no line (`partner_entries.entry_date`) or payment (`partner_payments.paid_on`) before
  1 January 2020 (`models.PARTNER_DATE_FLOOR`). Postgres gets real CHECKs
  (`ck_partner_entries_entry_date_floor`, `ck_partner_payments_paid_on_floor`), which validate
  the rows already stored. The models carry the same CHECKs, so `create_all` databases have them
  too. SQLite cannot add a CHECK without rebuilding the table (and with it 0017's triggers and
  partial indexes), so a migrated SQLite database gets a BEFORE INSERT trigger per table
  instead; rows are never updated anyway. No test data was dated before 2020; the 0017 "tables
  match the models" test leaves out the two floor CHECKs (0019 adds them) and
  `tests/test_migration_0019.py` tests both kinds.
- **H2, nothing stops counting silently.** Two steps when the switch-over day leaves something
  out. The first post counts, under the lock, every sale, line (pending ones included) and
  payment dated before that day, and shows them with « N lignes, M paiements et K ventes datés
  avant le JJ/MM ne seront plus comptés (effet net : X MRU) » (X is the change to the balance).
  Nothing is written. The second post must tick « J'ai vérifié que ces éléments sont dans le
  solde du fichier Excel » and carry the fingerprint of the list it saw (counts, sum and a
  digest of the ids). The list is counted again under the lock at insert time: if it changed,
  the post is refused (409) with the new list. With nothing to leave out, one post is enough.
  The audit event adds `dropped_lines`, `dropped_payments`, `dropped_sales` and `dropped_sum`
  (what those items counted for, before they stopped counting), numbers only.
- **M1, a twin post under the lock.** The request key is looked up again once the lock is held,
  before the "already opened" check. A twin that committed meanwhile is returned (the
  « déjà enregistré » redirect), or refused as a reused key when its values differ.
- **M2:** the date is required; a blank one is refused (« Saisissez la date du passage. »),
  never defaulted by the server. The form still pre-fills today.
- **L1:** a zero opening needs the « Solde nul » tick; the tick with a non-zero amount is refused.
- **L3:** `GET /partner-openings/{id}/new` shows the reason instead of the form for a partner
  that already has an opening, has a closed period or is inactive, and counts against
  `PARTNER_ACCOUNT_READ_USER`. A post for an inactive partner is refused (409), and the account
  page does not offer the form for one.
- **L4:** `partner_book.accounts_of` raises `ValueError` for a partner of another agency.

Postgres-only (left to CI, in addition to the above): the two CHECKs of 0019, including that
adding them validates existing rows (`tests/test_migration_0019.py` runs against the server
there, and `test_postgres_has_the_checks` checks their names).
