# Partner accounts, client invoices and reminders

Status: proposal for approval. The app replaces the BADI-style Excel files and the old invoicing
software (decision 20). Based on [04-findings-first-files.md](04-findings-first-files.md) §3–5.

## 1. Two kinds of buyer

| | Partner agency (AG = partner name) | Direct client (AG = BRUGE) |
|---|---|---|
| Price | cost + the partner's fee (automatic) | chosen by the agent |
| Owes money through | a **running account**, closed every half-month | an **invoice per ticket** |
| Pays | lump sums against the balance | per invoice, fully or in part |
| Deadline | 15 days after the period closes (decision 19) | 15 days after the invoice date (decision 6) |
| Document | partner statement (replaces the Excel tab) | invoice, unpaid-invoice list, client statement |

## 2. Partners

**Partner record:** name, phone, current fee, fee history (fee, valid from, set by).
Example: BADI 500 MRU until 15 July, 300 MRU from 16 July.

**Lines on a partner account**
- every ticket of the corrected report with AG = that partner: `P.T` = cost + fee (or the
  one-off override, with its reason);
- **manual lines** with no ticket number, entered by the accountant with a reason:
  `ADM` (airline penalty passed on, +), `RFND` (refund, −), `CHANGEMENT` (change fee, +),
  `AJUSTEMENT` (other, ±; manager approval);
- **payments**: date, amount, method, reference, received by. Not tied to tickets.

**Periods:** 1–15 and 16–end of month, as the sheet does today. When a period closes:
- `previous balance + period total − payments = new balance`, carried to the next period
  (the BADI "Solde net", with the sign reversed so that **positive = the partner owes**);
- the statement is frozen; a later correction goes into the next open period as a manual line.

**Partner statement** (PDF and Excel): period, tickets (date, ticket no, airline, passenger,
PNR, cost, fee, P.T), manual lines, payments, previous balance, new balance, agency details.

**Opening balances:** on switch-over day each partner starts with the balance from its Excel file
(to be supplied then; see [00-data-request.md](00-data-request.md)).

## 3. Direct clients

**Client record:** name, phone, client code (`CU…`, continuing the old style), purchase history.
A client is found by phone first, then by name; the agent confirms "same client as before?".

**Invoice:** one per ticket at the end of the day, or grouped per booking when one client buys
several tickets (to confirm with the accountant).
- Number: `IN<yymm>-<sequence>`, one unbroken sequence per agency, never reused.
- Lines: product (`Billet`, `Changement`, later `Visa`, `Hôtel`…), passenger, ticket no, amount.
- Same header and agency details as today: Arabic and French name, phones, email, address, NIF,
  bank account, Bankily/Masrvi/Sedad number. No VAT (HT = TTC), as today; to confirm with the
  accountant.
- An issued invoice never changes. A mistake is fixed by a **credit note** (`AV…`) and a new
  invoice.

**Payments:** `PAY<yymm>-<sequence>`, date, amount, mode (cash, Bankily, Masrvi, Sedad,
transfer, cheque), account, received by. One payment can cover several invoices, oldest first
unless the accountant chooses.

**Documents** (same as the old software's):
- **Factures impayées:** open invoices with paid and remaining amounts.
- **Situation de client:** all purchases, all payments, totals per product, balance.

## 4. Reminders and alerts

Every morning, on the dashboard and in the daily summary:

| Level | Clients | Partners |
|---|---|---|
| **Due soon** (critical) | invoice due within 3 days | period balance due within 3 days |
| **Overdue** | past due date, with days late and amount | same, per partner |

- Shown to the manager and the accountant (decision 7).
- For each overdue item the app **prepares** a reminder message (French or Arabic) with the
  amount and the statement attached; a person sends it (decision 8).
- Partial payments keep the item open with the remaining amount (decision 9).

## 5. What is reported

- **Revenue** = Σ P.B; **profit** = Σ (P.B − cost) + airline commission (decision 13).
- Split by AG: profit from partners (= their fees) vs profit from direct clients.
- **Owed to the agency:** total, by partner and by client, by age (0–15, 16–30, 31–60, 60+ days).
- **Cash in:** payments by mode and by day.

## 6. Not covered yet

Visas, hotels, Hajj/Umrah as products (PLAN.md phase 7); staff commissions; any VAT.
