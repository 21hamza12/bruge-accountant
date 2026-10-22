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
| **B9** Approvals | agent answers; validator for cost/ticket no/type (not their own answer); close; reopen (manager, reason) | 0010: `flag_decisions` | **yes** |
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
