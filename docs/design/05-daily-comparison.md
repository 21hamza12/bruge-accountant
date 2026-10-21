# The daily workflow: app report, Amadeus report, TJQ APRES

Status: proposal for approval. Built on the owner's description (21 Oct) and the 20 Oct files
([04-findings-first-files.md](04-findings-first-files.md)).

## 1. The day in one picture

```
 During day D                         End of day D / morning D+1                 After approval
 ─────────────                        ───────────────────────────                ──────────────
 agents send each ticket   ──►  APP TJQ AVANT (built by the app)  ─┐
 (screenshot or pasted text)                                      ├─► COMPARISON ─► approvals ─► TJQ APRES
                                AMADEUS TJQ AVANT (uploaded)   ───┘    (flags)                    (stored,
                                                                                                    Excel export)
```

A day has a status: `open` (tickets arriving) → `compared` (Amadeus uploaded, flags open) →
`approved` (no open flag) → `closed` (TJQ APRES stored). A closed day can be reopened only by the
manager, with a reason (decision 5).

## 2. What an agent sends for one ticket

| Field | From | Required |
|---|---|---|
| Ticket number (DOC NUMBER), airline (A/L) | screenshot/text | yes |
| Passenger name, pax type (ADT/CHD/INF) | screenshot/text | yes |
| PNR (RLOC), type (TKTT/CANX/RFND/EMD…) | screenshot/text | yes |
| Cost (TOTAL DOC), and TAX/COMM when visible | screenshot/text | yes (cost) |
| Agent sign (AS) | the logged-in agent's linked sign | automatic |
| **AG**: BRUGE or the partner's name | RM line, or asked | yes |
| **P.B**: Bruge price | RM line for BRUGE sales; **computed** for partners (cost + fee) | yes |
| **CLT**: client name | RM line, or asked | direct sales |
| **AP**: client phone | RM line, or asked | direct sales (optional if the client refuses) |
| Payment method | RM line, or asked | yes |

The app reads the message, shows a **read-back** ("Ticket 394…438, Turkish, 21 368 MRU,
BRUGE, P.B 24 000, client …, cash. Correct?"), and saves only after the agent confirms. Anything
missing is asked in the same chat. The image is discarded after reading (decision 3).

A ticket number already sent is refused as a duplicate; the agent is offered "edit the existing
one" instead.

## 3. The comparison

Lines are matched on **DOC NUMBER**. For each pair:

| Case | Flag | Who acts | Default |
|---|---|---|---|
| In both, all compared fields equal | none | – | OK |
| In both, a field differs | `DIFFERENT` (field, app value, Amadeus value) | issuing agent, then accountant/manager for money fields (decision 18) | Amadeus value suggested |
| In Amadeus only, type `TKTT`/`EMD`/`RFND` | `NOT SENT` | issuing agent (by AS sign): send the ticket's P.B/AG/CLT/AP now | none: it blocks closing |
| In Amadeus only, type `CANX` | none | – | **accepted automatically**, listed as "cancelled today" |
| In app only | `NOT IN AMADEUS` | issuing agent: wrong number? issued another day? voided before the report? | none: it blocks closing |

**Fields compared:** A/L, TOTAL DOC (exact, to the unit), PAX NAME (ignoring extra spaces and
case), pax type, AS, RLOC, TRNC. P.B, AG, CLT, AP are not compared (Amadeus does not have them).

**Ticket from another day:** if a `NOT IN AMADEUS` ticket appears in the next day's Amadeus
report, it is moved to that day automatically and the flag closes.

**Re-uploading** the Amadeus report for the same day re-runs the comparison; approved decisions
stay unless the line they were about changed.

## 4. Approving a flag

- The agent sees each flag side by side: "Amadeus: 45 020 · You sent: 45 200 · Which is right?"
  with buttons *Amadeus is right* / *Mine is right (reason)*.
- **Cost, ticket number or ticket type** changes then wait for the accountant or manager.
  Name spelling, PNR typos: the agent's answer is enough.
- Every decision is stored with who, when, the reason, the old and new value. Nothing is deleted.
- The original Amadeus file is never modified; corrections live in the **corrected report**
  ([03-money-model-draft.md](03-money-model-draft.md) §1).

## 5. The TJQ APRES (what the app produces)

Same layout as the accountant's file today, so it can replace it directly:

| Row | Content |
|---|---|
| 1–3 | `Agy no` · `Report date range` = the day (e.g. `20OCT`) · `Current date` = Amadeus generation date |
| 5 | `SEQ NO, A/L, DOC NUMBER, TOTAL DOC, PAX NAME, AS, TRNC, RLOC, P.B, AG, CLT, AP` |
| 6… | one line per ticket of the corrected report, **CANX lines removed**, in SEQ NO order |

- Values are the **approved** values. A corrected cell is highlighted, with a comment giving the
  original Amadeus value and who approved the change.
- A totals line under the table: number of tickets, Σ TOTAL DOC, Σ P.B, Σ (P.B − TOTAL DOC).
- Cells starting with `= + - @`, tab or CR are neutralised (formula injection).
- Stored when the day closes; downloadable any time for any date ("give me 19 Sept").

## 6. Acceptance test (from the real 20 Oct files)

Given the 20 Oct Amadeus report and app lines identical to it, the app's TJQ APRES must equal the
accountant's `TJQ APRES` on: the 34 lines, their order and the 8 Amadeus columns. Cost total
624 126 MRU. The 9 CANX lines are listed as cancelled, not flagged. (Run on local files only;
the committed test uses a synthetic copy with made-up names.)

## 7. Not decided here

How the screenshot is read (plain code or AI, which model): waits for the ticket samples
([00-data-request.md](00-data-request.md) #1–2).
