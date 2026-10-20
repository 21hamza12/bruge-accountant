# Money model (DRAFT: to be checked against the real files and the interview)

Status: draft written before the data arrived. Every point marked **[verify]** depends on the
agency's real files or the accountant's answers and may change.

## 1. The flow of one sale

```
Staff issues ticket in Amadeus
   │
   ├─► sends screenshot or pasted text to the app chat (same day)
   │      app reads: ticket no, PNR, passenger, RM price, client/partner, payment method
   │      staff confirms the read-back → SALE ENTRY (confirmed)
   │      the image is discarded right after reading (never stored)
   │
   └─► end of day: Amadeus daily report uploaded
          app matches report lines ↔ sale entries (D+1, accountant)
          ├─ matched             → OK
          ├─ in report, no sale  → "missing price" question to the staff member
          └─ sale, not in report → flagged for the accountant
```

## 2. Screenshots are not kept: consequences

Decision 3 in [02-open-decisions.md](02-open-decisions.md): screenshots are read, then discarded.

- **The proof of a price is the staff member's confirmation**, not the picture. Each sale stores
  the confirmed fields, who confirmed, when, and the exact RM text that was read (a short line
  of text, not the image), so a later dispute can be checked against what was read.
- **The Amadeus daily report is the safety net** for everything except the price: ticket number,
  cost, passenger, airline and staff sign are always re-checked against it at D+1. A reading
  mistake on those fields is caught automatically.
- **Only the selling price, the client/partner and the payment method rely on the read-back.**
  That is why the read-back must be clear and the staff member must confirm before saving.
- **No re-reading later.** If a better reading method comes along, old screenshots cannot be
  re-read. Corrections go through an edit (with history), not by re-processing.
- **Duplicates** are blocked by a fingerprint (SHA-256) of the image, plus the ticket number.
- **When the AI reads a screenshot**, the image is sent once to the AI provider for that
  reading only, under no-training API terms, and not stored on our side.

## 3. What is stored

All tables carry `agency_id` and the same database-level separation as today.

| Record | Holds | Changes? |
|---|---|---|
| **Ticket** (exists) | Amadeus facts: ticket no, airline, cost, passenger, sign, type, PNR, report day | Refreshed by later reports, never edited by hand |
| **Sale** | One per ticket (or per non-ticket service): selling price, client or partner, payment method, who confirmed, when, source (`screenshot`, `pasted`, `typed`), the RM text read, the image fingerprint | **Never overwritten.** A change creates a new version |
| **Sale version** | Every value a sale ever had, with who, when and the reason | Append-only: this is the edit history |
| **Party** | Clients and partners (name, phone, NIF, payment terms) | Editable, history kept |
| **Invoice** | Number, party, date, due date (+15 days **[verify]**), lines (sales), status | Lines frozen once issued; corrections by credit note |
| **Payment** | Amount, method, reference, date, received by; allocated to one or more invoices or sales | Append-only; reversal by a negative entry |
| **Day** | Date, status (`open` → `checked` → `closed`), totals at closing, who closed | Reopening a closed day needs the manager **[verify]** |

**Money rule:** amounts are stored as exact decimals in MRU, never floats (same as today).

## 4. Edits and history

- Nothing is deleted. Editing a price adds a new **sale version**; the sheet shows the latest one,
  and the history shows every earlier value, who changed it, when and why.
- Rights (proposed in decisions #4–5): staff edit their own sales on the same day; manager and
  accountant edit anything with a reason; edits to a closed day need manager approval.
- An issued invoice is never changed. A wrong invoice is cancelled by a credit note and reissued.

## 5. Special lines in the daily report

| Report type | Treatment | [verify] |
|---|---|---|
| `TKTT` ticket | Matched to its sale | – |
| `RFND` refund | Linked to the original ticket; revenue reduced by the amount refunded to the client; airline penalty recorded as cost | Who keeps the penalty; is the client refund the same as the airline refund? |
| `CANX` / void | Sale cancelled (new version, status `void`); no revenue, no cost | Same-day only? Any fee charged? |
| EMD / extras | Their own sale lines (bags, seats, change fees) with their own price | How they appear in the report |
| Reissue/exchange | Linked old → new ticket; only the difference is charged | Format in the report |

## 6. Daily sheet (any day, on demand, Excel export)

One row per ticket or service for the chosen day:
ticket no · PNR · passenger · staff · airline · **cost** · **selling price** · **profit** ·
client/partner · payment method · paid / unpaid · invoice no · matching status.

Then totals per staff, per airline and per payment method, and a **mismatch section**
(missing price, sale not in report, amount differs). The columns will be aligned with the
accountant's own sheet once we have it **[verify]**. Exported cells are protected against
formula injection, as already planned.

## 7. Reconciliation (three levels)

1. **Sales ↔ Amadeus report** (daily, D+1): every ticket has exactly one confirmed sale.
2. **Sales ↔ money received** (daily): cash count, mobile-money and bank lines matched to sales
   paid that day. Unmatched money and unpaid "paid" sales are flagged.
3. **Amadeus cost ↔ airline/BSP billing** (each billing period): what the airlines charge
   equals the costs in the reports.

Each level ends in a list of differences for a person to clear. The app never "fixes" a
difference on its own.

## 8. Invoices and reminders

- States: `draft` → `issued` → `partly paid` → `paid`, or `cancelled` (credit note).
- Every morning, in the daily report: invoices **overdue**, and invoices **due within 3 days**
  marked critical.
- Reminder messages are prepared by the app and sent by a person (decision #8).
- Invoice layout and legal mentions follow Mauritanian requirements **[verify with the real invoices
  and the accountant]**.

## 9. Revenue, cost, profit (proposed; see decision #13)

- **Revenue** = selling price of confirmed sales, minus refunds given back to clients.
- **Direct cost** = Amadeus cost (+ penalties, + partner commission where one applies).
- **Gross profit** = revenue − direct cost. **Margin** = gross profit ÷ revenue.
- **Cash collected** is reported separately from revenue: a credit sale is revenue today and
  cash later.

## 10. The chatbot

The chat is the way in, not a separate system. Every chat action goes through the same records
above:

- staff: send a ticket, confirm a read-back, answer a missing-price question, record a payment;
- manager and accountant: "sheet for 19 Sept", "who owes us more than 30 days", "profit this week".

The AI chooses among fixed, read-only report functions, or proposes an entry that a person
confirms. It never writes to the database on its own, never writes free-form SQL and never sets a
price.

## 11. Not covered yet

Hotels, visas and Hajj/Umrah as services (Phase 7 in PLAN.md), staff commissions, multi-currency.
