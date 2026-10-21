# What the first real files show (21 Oct 2026)

Files studied (kept in `intake/`, never committed). Client and passenger names are left out on
purpose.

| File | What it is |
|---|---|
| `TJQ AVANT.xlsx` | Amadeus daily report for 20 Oct, as exported |
| `TJQ APRES.xlsx` | The same report after the accountant's edit |
| `BADI 2026.xlsx` | The account of one partner (BADI), Jan–Oct 2026, one tab per month |
| `facture_impayees_<client>.pdf` | Unpaid invoices for one client |
| `Situation_de_client.pdf` | Full statement for the same client: purchases, payments, balance |

## 1. The Amadeus report has a newer, wider layout

The 20 Oct export has **16 columns**, not the 8 we built for:
`SEQ NO, CONFIRMED, A/L, DOC NUMBER, TOTAL DOC, TAX, FEE, COMM, AGENT, FP, PAX NAME, AS, RLOC, TRNC, REPORT IND, NDC`.

- Our parser already reads it correctly (it finds columns by name): 43 lines, 34 `TKTT`, 9 `CANX`.
- **New useful fields:** `TAX` (airport taxes inside the total), `COMM` (commission the airline
  pays the agency, e.g. 60.27 or 290.99 MRU; this is agency income), `FP` (form of payment;
  all `CA` = cash on this day), `NDC`.

## 2. What the accountant changes (AVANT → APRES)

Compared ticket by ticket on 20 Oct:

| Change | Detail |
|---|---|
| **Removes every cancelled line** | All 9 `CANX` lines are deleted (43 → 34 lines). Nothing else is removed |
| **Keeps the old 8 columns** | Drops CONFIRMED, TAX, FEE, COMM, AGENT, FP, REPORT IND, NDC |
| **Adds 4 empty columns to fill in** | `P.B`, `AG`, `CLT`, `AP` (still empty in this file) |
| **Changes no value** | Cost, passenger, sign, PNR: identical on all 34 kept lines |

Patterns worth noting:
- **Cancel and reissue:** one passenger has a `CANX` line and a `TKTT` line with the same PNR and
  the same amount. The cancelled one disappears, the reissued one stays.
- **Zero-cost tickets:** two `TKTT` tickets on airline 495 show a cost of 0 (same PNR). To ask why.

**Consequence for the app:** the "edited report" step is mostly mechanical: drop `CANX`, then
collect the four hand-filled values per ticket. The app can do the first part automatically.

**Bug found in the current app:** cancelled lines are imported with only a flag. On 20 Oct the
dashboard would show **801 498 MRU** of cost instead of **624 126** (+177 372 from 9 cancelled
tickets), and staff would be asked for prices on cancelled tickets. To fix before go-live.

## 3. A partner account: BADI 2026

One workbook per partner, one tab per month. Since March, each month is split in two halves
(1–15 and 16–end), each with its own table, totals and ledger.

**Ticket lines** (copied from the Amadeus report) plus two columns:
- `Frais BRG`: **Bruge's fee per ticket**, charged on top of the Amadeus cost.
  - 500 MRU until mid-July, **300 MRU from 16 July**; some tickets 0, 100, 200 or 600.
- `P.T` (price to the partner) = `TOTAL DOC + Frais BRG`. True on every line checked.

**Hand-typed lines with no ticket number:**
- January: an **ADM** (airline penalty passed on to the partner), +11 306.
- February: a **refund** (RFND), −12 311.
These are the only lines that make the totals differ from cost + fees.

**Ledger beside each period:**
- previous balance (carried from the previous period by a formula);
- payments received, by date: round amounts (20 000, 60 000, 170 000…), **not tied to tickets**;
- `Solde net` = payments − previous balance − period total; negative = BADI owes.
- Latest (16–19 Oct): BADI owes **126 247 MRU**.

**What this means:**
- **Partners work on a running account, not on invoices.** They pay lump sums against a balance.
  The "15-day invoice deadline" fits clients; for partners the natural rule is the age of the
  balance per period (decisions 10 and 19).
- **Partner profit = the fee** (plus any airline commission), not a free selling price. Staff do
  not need to type a price for partner tickets: the fee rule gives it, with exceptions.
- **The sheet layout is not reliable:** column labels are shifted in several months (the `RLOC`
  column holds `TKTT`, the `TRNC` column holds the PNR; in April the `AS` column holds the PNR).
  The app must rebuild these statements from its own data, never read the partner sheet as a source.
- One hand-typed opening balance in January comes from a 2025 workbook (`[1]DEC`), which we do
  not have.

## 4. Client invoices already come from a billing system

Both PDFs are produced by existing software (footer "Powered by TCPDF"; numbering such as
invoice `IN2610-7972`, client code `CU2609-…`, payment `PAY2610-…`, which looks like
**Dolibarr**; to confirm).

- **One invoice per ticket**, with products `1 - Billet` and `2 - Changement` (change fee).
- **Payments are recorded** with mode and account (e.g. "Virement bancaire, BANKILY").
- **Two documents:** "Factures impayées" (unpaid invoices) and "Situation de client" (all
  purchases, all payments, balance).
- **Agency details printed:** NIF, bank account, Bankily/Masrvi/Sedad number, address, Arabic and
  French name. Amounts show `HT` and `TTC` with the same value (no VAT applied).
- **Airline code mismatch:** the invoices print **474** before ticket numbers that the Amadeus
  report lists under airline **147**. To ask: typo, setting, or a different code?

**Consequence:** the software is custom and cannot be read or linked (section 5), so the app
cannot take invoices or payments from it. See decision 20.

## 5. Answers from the owner (21 Oct)

| Question | Answer |
|---|---|
| The billing software | **Custom software bought from a developer.** No export, no API: the app cannot read from it or link to it |
| `P.B` | **Bruge price**: the selling price |
| `AG` | **The agency that booked**: `BRUGE` for a direct sale, or the partner agency's name |
| `CLT` | **The client** the ticket was booked for |
| `AP` | **The client's phone number** |
| `Frais BRG` | **Fixed fee for partners.** For direct clients there is no fixed rule: **the agent decides** how much to add |

What this means for each ticket:
- **Partner sale** (`AG` = a partner): price = Amadeus cost + the partner's fixed fee. Staff only
  need to say which partner; the app computes `P.B`.
- **Direct sale** (`AG` = `BRUGE`): staff give the price they chose, the client's name and phone.
- The BADI file still shows fees of 0, 100, 200 and 600 on some tickets, and a change from 500
  to 300 in July. So "fixed" means **one current fee per partner, changeable over time, with
  occasional exceptions**. The app keeps a fee history per partner and lets staff override it
  for one ticket, with a reason.

## 6. Questions still open

1. When is a partner fee 0, 100, 200 or 600 instead of the usual one?
2. Airline 474 on the invoices vs 147 in Amadeus: which is right?
3. Why do some 495 tickets cost 0?
4. Are ADM and refund lines always typed by hand into the partner sheet? Where do they come from?
5. Code 495: which airline?
6. Since the billing software cannot be linked: should the app take over invoices and client
   statements (decision 20)?
