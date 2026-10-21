# Money model (DRAFT: to be checked against the real files and the interview)

Status: draft written before the data arrived. Every point marked **[verify]** depends on the
agency's real files or the accountant's answers and may change.

## 1. Two reports for the same day

The app builds **its own daily report** from what staff send during the day, then compares it
with the **Amadeus daily report** line by line. Differences are flagged; an approved difference
becomes a correction on the app's copy of the Amadeus report.

```
During the day
  Staff issues ticket in Amadeus
     └─► sends screenshot or pasted text to the app chat
            app reads every report field: ticket no, airline, cost (TOTAL DOC), passenger,
            pax type, staff sign, type (TKTT/RFND/CANX…), PNR
            + the accountant's four columns, which Amadeus does not have:
              AG  agency that booked: BRUGE or the partner's name
              P.B Bruge price: for a partner = cost + the partner's fee (computed);
                  for BRUGE = the price the agent chose (from the RM line)
              CLT client name, AP client phone (direct sales)
            + payment method
            staff confirms the read-back → one line of the APP REPORT
            the image is discarded right after reading (never stored)

End of day (D+1 for the accountant)
  Amadeus daily report uploaded → AMADEUS REPORT (original kept exactly as received)
  app compares the two, matching lines by ticket number:
     ├─ identical                       → OK
     ├─ same ticket, a field differs    → DIFFERENCE (e.g. cost 45 200 vs 45 020)
     ├─ in Amadeus only                 → MISSING IN APP REPORT (staff did not send it)
     └─ in app report only              → MISSING IN AMADEUS (wrong number, void, next day?)
```

### What happens to a difference

1. The app shows it to the staff member who issued the ticket, side by side:
   "Amadeus says X, you sent Y. Which is right?"
2. The staff member answers, and approves the change (proposed: the accountant or manager then
   validates any change to a cost or a ticket number; see decision 18).
3. Once approved, the correction is applied to the **corrected Amadeus report**. Both the
   original value and the approved value are kept, with who approved, when and why.

### The three layers

| Layer | What it is | Changed? |
|---|---|---|
| **Original Amadeus report** | The file exactly as uploaded | Never. It is the reference |
| **App report** | Built from staff screenshots and text during the day | Only by the staff member who sent it, before the comparison |
| **Corrected Amadeus report** | The Amadeus report plus every approved correction, plus the prices | Only through an approved correction; full history |

All daily sheets, totals, invoices and exports use the **corrected Amadeus report**. Every
corrected cell is marked, and the original value is one click away.

**Limit:** the app never writes back into Amadeus itself (that would need Amadeus/1A access and
is not offered to the agency). If the real ticket in Amadeus is wrong, staff still fix it in
Amadeus; the app records the correction on its side.

## 2. Screenshots are not kept: consequences

Decision 3 in [02-open-decisions.md](02-open-decisions.md): screenshots are read, then discarded.

- **The proof of each line is the staff member's confirmation**, not the picture. Each line stores
  the confirmed fields, who confirmed, when, and the exact text that was read (a few lines of
  text, not the image), so a later dispute can be checked against what was read.
- **The app reads the full ticket**, not just the price, because it builds a complete report of
  its own. Reading more fields means more chances to misread; the comparison with Amadeus is
  what catches those, and the golden set measures how often it happens.
- **Selling price, client/partner and payment method** exist only in the app report (Amadeus
  does not have them), so for those the read-back and confirmation are the only check.
- **No re-reading later.** If a better reading method comes along, old screenshots cannot be
  re-read. Corrections go through an edit (with history), not by re-processing.
- **Duplicates** are blocked by a fingerprint (SHA-256) of the image, plus the ticket number.
- **When the AI reads a screenshot**, the image is sent once to the AI provider for that
  reading only, under no-training API terms, and not stored on our side.

### Partner fees

Each partner has one current fee (e.g. 300 MRU for BADI since 16 July), kept with its history.
For a partner ticket the app fills `P.B` = cost + fee; staff may override it for one ticket with
a reason (the BADI file shows occasional 0, 100, 200 or 600).

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

## 7. Reconciliation

1. **App report ↔ Amadeus report** (daily, D+1): every line matches, or its difference is
   approved as a correction (section 1).
2. **Sales ↔ money recorded** (daily): every sale marked paid has a payment recorded in the app
   (method, amount, reference, who received it), and every credit sale stays open until paid.
   The cash count at closing is compared with the cash payments of the day.
3. **Not in the first version:** matching against mobile-money statements, bank statements and
   airline/BSP billing. The agency cannot provide those files, so the app cannot prove that a
   recorded payment really arrived; it relies on the payments staff and the accountant record.
   A statement import can be added later if the files become available.

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
