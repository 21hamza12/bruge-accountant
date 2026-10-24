# Phase B plan: the daily workflow, without AI

Status: **approved by the owner on 21 Oct 2026**, with the defaults in §4. One PR per step, in
order. Spec: [05-daily-comparison.md](05-daily-comparison.md); partners: [06](06-partners-and-invoices.md) §2.

## 1. What the code review found (shapes the plan)

1. **No accountant role.** `users.role` CHECK is `('manager','staff')`; `role == ROLE_MANAGER` is
   tested directly in `deps.require_manager`, `shell.build_shell`, `uploads._is_manager`.
2. **Migration trap (HIGH).** Migration 0003 calls the live `db_roles.apply_grants`, which grants on
   every table in `GRANTS`. Adding 0005+ tables to `GRANTS` makes a fresh `upgrade head` fail at
   0003. B1 freezes 0003 to its own table list; each later migration grants only its own tables.
3. **`tickets` cannot drive the comparison.** It upserts on `(agency_id, ticket_no)` and overwrites
   `last_upload_id`, so it is neither "the lines of the latest report for day D in SEQ order" nor an
   untouched record. B2 adds an append-only per-upload snapshot.
4. **TAX, COMM, FP are not parsed.** B2 reads them when present.
5. **Formula injection.** openpyxl turns strings starting with `=` into formulas; B10 neutralises.
6. **Existing guards cover new tables**: `test_every_table_with_tenant_data_has_forced_rls_and_a_policy`
   and `test_every_table_is_in_the_grants_map_or_explicitly_exempt` must keep passing.
7. **CANX branch** (`claude/zen-noyce-c95777`) touches `dashboard.py`, `questions.py`, `repository.py`,
   `parsing/amadeus.py`. Phase B leaves `questions.py`, `dashboard.py` and `_build_ticket` alone and
   rebases B2 after that branch merges.

## 2. Conventions for every step

- **Tables:** `agency_id BIGINT NOT NULL` FK to agencies; `UNIQUE(agency_id, id)` on referenced
  tables; child references are composite FKs `(agency_id, x_id)`, `ON DELETE RESTRICT`; Postgres
  `ENABLE` + `FORCE ROW LEVEL SECURITY`.
- **Policies:** mutable tables `tenant_isolation` (FOR ALL, USING/WITH CHECK
  `agency_id = app_current_agency_id()`); append-only tables `tenant_read` (SELECT) + `tenant_insert`
  (INSERT), no UPDATE/DELETE policy (as `audit_events`). SQLite: `BEFORE UPDATE/DELETE` triggers
  with `RAISE(ABORT)` (as 0004).
- **Grants:** every table in `db_roles.GRANTS` (append-only: SELECT, INSERT; nothing gets DELETE);
  each migration calls `apply_grants(bind, role, tables=...)` for its own tables.
- **Money:** `Numeric(14,2)` / `Decimal`; input via `app/money.py::parse_amount` (spaces/NBSP
  removed, `,` or `.` decimal, ≤2 decimals, ≥0, <10¹², floats rejected).
- **Text:** `parsing.amadeus.clean_text` with length caps.
- **Web:** plain `def` routes, `Depends(csrf_protect)`; agency only from `auth.user.agency_id`;
  other agency / other agent's data → 404; `Cache-Control: no-store` on pages with money or
  names; French UI; no inline style/script; plain forms first, htmx as enhancement.
- **Permissions:** only through `app/permissions.py`.
- **Audit events:** `entry_created`, `entry_edited`, `partner_created`, `partner_fee_set`,
  `flag_decided`, `flag_validated`, `day_closed`, `day_reopened`, `export_downloaded`.
- **Gates:** tests first; ruff; mypy strict; coverage ≥80%; Postgres RLS suite. Reviewers:
  code + python on every PR; fastapi with routes; database with a migration; **security** where
  marked.

## 3. Steps

| Step | Goal | Schema | Security review |
|---|---|---|---|
| **B1** Roles | `accountant` role; permission functions; fix the 0003 grants trap | 0005: users role CHECK incl. accountant | yes |
| **B2** Amadeus snapshot | append-only copy of every parsed line per upload; optional TAX/COMM/FP/CONFIRMED; synthetic 20 Oct fixture | 0006: `amadeus_lines` | yes (upload) |
| **B3** Partners | partners + fee history; `fee_on(partner, day)`; pages for manager/accountant | 0007: `partners`, `partner_fees` | – |
| **B4** Entries (domain) | report days; **sales** (one or more tickets, one P.B) and their tickets; append-only versions; P.B rules; edit rights | 0008: `report_days`, `day_events`, `sales`, `sale_tickets`, `sale_versions` | yes |
| **B5** Entry form | form → read-back → confirm (server recomputes everything); duplicate → edit; history | – | **yes** |
| **B6** Pasted text | `PastedTicketParser` interface + placeholder format + fixtures; fills the B5 form | – | **yes** |
| **B7** Day page | app TJQ AVANT per day, totals, status | – | – |
| **B8** Comparison | pure `compare()`; flags; run after upload; moved-to-later-day; re-run keeps decisions; closed day refuses uploads | 0009: `comparison_flags` | **yes** |
| **B9** Approvals | agent answers; validator for cost/ticket no/type (not their own answer); close; reopen (manager, reason) | 0013: `flag_decisions`, `day_acknowledgements` (numbers were shifted by the phases before it; see `app/approvals.py` for the rules, the close gate and what B10 calls) | **yes** |
| **B10** TJQ APRES | build at close, store versions, download (accountant/manager); highlighted corrections with comments; totals; neutralised cells | 0011: `day_exports` | **yes** |
| **B11** Acceptance | synthetic 20 Oct end-to-end (34 lines, 624 126, 9 cancelled); local real-file check skipped when `intake/` files absent | – | – |

### B1 detail (first step)

- `app/models.py`: `ROLE_ACCOUNTANT`; CHECK `IN ('manager','accountant','staff')`.
- `app/accounts.py`: add to `ROLES`.
- New `app/permissions.py` (pure, over `User`): `sees_all_signs` (manager, accountant),
  `can_manage_staff` (manager), `can_manage_partners` (manager, accountant), `can_validate_money`
  (manager, accountant), `can_close_day` (manager, accountant), `can_reopen_day` (manager),
  `can_edit_any_entry` (manager, accountant).
- `app/web/deps.py`: `require_permission(check)`; `require_manager` kept.
- `app/web/shell.py`: `sees_all` from `permissions.sees_all_signs`; keep `is_manager` alias for
  templates. `app/web/routes/uploads.py`: `_is_manager` → `sees_all_signs`.
- Staff pages: role option "Comptable"; the last manager cannot be turned into an accountant.
- `app/db_roles.py`: `apply_grants(..., tables=None)`; `MIGRATION_0003_TABLES`; migration 0003 passes
  the frozen list (identical behaviour for existing tables).
- Migration 0005: Postgres drop/recreate `ck_users_role`; SQLite rebuild `users` with
  `batch_alter_table(recreate="always")` and `PRAGMA foreign_keys=OFF` outside the transaction,
  then `PRAGMA foreign_key_check`. Downgrade refused if an accountant exists.
- Tests first: `test_permissions.py::test_matrix_per_role`; `test_accountant_sees_all_signs_in_shell`;
  `test_accountant_cannot_open_staff_pages_403_and_audited`; `test_manager_can_create_accountant`;
  `test_last_manager_cannot_become_accountant`; `test_migration_0005.py` (up/down on SQLite with
  rows, Postgres allows accountant, downgrade refused); `test_migrations_fresh_upgrade_head_with_app_role`;
  a guard that `role ==` comparisons appear only in `permissions.py` and `accounts.py`.

## 3b. Change after approval: multi-ticket sales (22 Oct)

The owner reported sales where **one RM price covers several ticket numbers** (layover on two
airlines; family bookings). B4 therefore stores **sales**, not single-ticket entries: a sale has one
or more tickets (`sale_tickets`, each ticket number in at most one sale per agency), one P.B, AG,
CLT, AP and payment method, and append-only `sale_versions`. Comparison (B8) stays per ticket.
Per-line P.B in the TJQ APRES (B10): full price on the first ticket, 0 on the others, lines
marked as one sale (decision 21). Partner fee is charged per ticket (decision 22).

## 3c. B5 as built (22 Oct)

Routes (all `Depends(csrf_protect)`, `Cache-Control: no-store`, `can_use_sales_screens`): `GET/POST
/sales/new` (form; POST redisplays the typed values), `POST /sales/preview` (read-back, writes
nothing; `add_row` adds an empty ticket row, no JavaScript), `POST /sales` (confirm), `GET /sales`
(day list, `?date=`, `?sign=` for manager/accountant), `GET /sales/{id}` (values and history, newest
first), `GET/POST /sales/{id}/edit`, `POST /sales/{id}/preview-edit`, `POST /sales/{id}` (edit),
`GET/POST /sales/{id}/void`. The read-back carries every posted value in hidden fields and trusts
none: the confirm step validates and prices again (agency, sale id, partner P.B ignored). An edit
carries `version_no`; a sale that moved on is "rechargez la page" (`expected_version` in
`edit_sale`/`void_sale`). Sale writes share one limit per user: `ratelimit.SALE_WRITE_USER`, 300 an
hour. Bottom bar: staff get "Saisir une vente" and "Mes ventes" in place of "Questions" (five tabs);
manager and accountant keep their five and reach "Ventes du jour" from the sidebar and the dashboard.

## 3d. Phase B as built (B1 to B11)

What each step delivered:

- **B1 roles**: the `accountant` role and every permission in `app/permissions.py`.
- **B2 Amadeus snapshot**: `amadeus_lines`, the report exactly as received, append-only, in file order.
- **B3 partners**: partners with a dated fee history (`fee_on`), their pages, and partner aliases
  (the other names an agency is known by in the Amadeus text).
- **B4 sales and days**: report days, multi-ticket sales with one P.B, versions kept for ever.
- **B5 sale screens**: form, read-back, confirm, list, history, edit, void.
- **B6 pasted text**: the booking screen pasted into the form (`/sales/paste`), one proposal per
  passenger, the agency question, alias proposals.
- **B7 day page**: the app's own TJQ AVANT per day, totals, status, history.
- **B8 comparison**: `compare()` after each upload, flags, moved tickets, cancelled tickets listed.
- **B9 approvals**: the agent answers, a validator approves, the close gate, reopen by the manager.
- **B10 TJQ APRES**: built at close, stored by version, downloaded by manager and accountant.
- **B11 acceptance**: `tests/test_acceptance_20oct.py` runs the made-up 20 October through the
  web client only (real CSRF, real logins): setup of the partners and aliases, 34 tickets entered
  by two agents (form and paste, a layover sale, partner sales, direct sales), the upload and the
  automatic comparison (0 blocking flags, 9 cancelled, day approved), then a day with troubles (a
  typo, a ticket nobody entered, a sign the agent keeps, a partner ticket issued elsewhere), the
  refused and the accepted close, the download (34 lines in SEQ order, 624 126, P.B rules, the
  highlighted correction, no formula), who sees what, and the reopen with version 2.
  `tests/test_real_20oct.py` does the same with the real TJQ AVANT and compares with the
  accountant's real TJQ APRES; it is skipped when `intake/amadeus-reports/TJQ AVANT.xlsx` or
  `intake/excel-sheets/TJQ APRES.xlsx` is missing and prints counts only, never names.

Decisions taken overnight (not in the first approval):

1. **Moves** of a ticket to another day are made only by the manager or the accountant, and only
   for a whole sale (never one ticket of a multi-ticket sale).
2. **Only a run by the manager or the accountant approves a day.** An upload or a re-run by anyone
   else can flag, but never sets the day to `approved`.
3. **Voids, sign changes and passenger-type changes need a validator.** An agent's answer to such a
   flag is `answered` and waits; the validator is never the person who answered. A change of cost
   or of ticket number needs one too; a name, booking reference or airline fix needs the agent only.
4. **The agent always confirms the partner.** A pasted booking proposes an agency (exact, through an
   alias, prefix or close name) but never pre-answers: the read-back is refused until the agent
   says yes, picks another partner, or sells direct.
5. **Partner aliases need approval.** An agency name the agent assigned to a partner is only
   *proposed*; a manager or accountant approves, rejects or reassigns it. A pending or rejected name
   never matches a later paste.
6. **The partner list**: partner tickets that are not in the report (issued in another office) do
   not block the close, but a manager or accountant who did not write them must have seen the list
   ("J'ai vu la liste"); a changed list or price must be seen again.

Known difference with the accountant's file (found by the real-file check), now closed (phase C1,
item 3): for a child or an infant, the Amadeus report pads the `(CHD)` / `(INF)` mark away from the
name. The rule, read from the real report and file: the name is written left-aligned in a column of
24 characters and the mark starts at character 25 (so the cell is 29 characters long whatever the
name), i.e. the mark always starts at the same position. The parser still keeps the name with single
spaces and `pax_type` apart, so the export rebuilds the cell (`export_apres._report_name`: the name
padded with spaces to 24 characters, then `(CHD)` or `(INF)`); no stored raw name and no migration
are needed. A name of 24 characters or more keeps one space before the mark (the report's rule for
such names was not seen in the real data; nothing is ever cut). The local real-file check
(`tests/test_real_20oct.py`) now compares every cell as written and shows 0 differing cells. Everything
else in the 8 Amadeus columns, the 34 lines and their order were already identical.

## 4. Approved defaults

1. Pasted text: placeholder labelled-lines format now; real parser in its own PR when samples arrive.
2. Agents enter only their own signs' tickets; manager/accountant correct any entry with a reason.
3. Any logged-in user uploads the Amadeus report; uploads for a closed day are refused.
4. A `NOT IN AMADEUS` ticket found in a later report moves to that day (any earlier day not closed).
5. Entry types: `TKTT, EMDA, EMDS, RFND, CANX`, extended on the first real sample.
6. A TJQ APRES exists only for days closed in the app; no backfill tool in B.
7. Unusual partner fees: per-ticket override with a mandatory reason.
8. AP optional for direct sales, with a "client a refusé" box.
9. Accountant sees all tickets and validates; staff management stays manager-only.
10. Payment methods: cash, Bankily, Masrvi, Sedad, virement, chèque, crédit.

## 5. Done when

An agent enters a ticket (paste or form) with a read-back; partner P.B = cost + fee of the day (or an
override with reason); an upload flags every case of doc 05 §3 and a re-upload keeps decisions on
unchanged lines; cost/ticket/type changes need a validator; a day closes only with no open flag and
reopens only by the manager with a reason; any closed day's TJQ APRES downloads in the accountant's
layout and passes the synthetic 20 Oct test; every new table has forced RLS, grants, composite FKs
and append-only enforcement; coverage ≥80%, ruff and mypy strict clean.
