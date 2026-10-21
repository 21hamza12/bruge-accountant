# What we need from Bruge Voyages before designing

Put every file in the matching folder under `intake/` (on this laptop only; that folder is
git-ignored and never leaves the machine). Real names and phone numbers are fine there.

The more **unusual** days you include, the better: a day with a refund, a cancellation, a
partner who paid late, a mistake that was corrected.

| # | Folder | What | How many | Why we need it |
|---|---|---|---|---|
| 1 | `ticket-screens/` | Screenshots of Amadeus ticket screens exactly as staff take them today (phone photo or PC capture), with the RM line showing the selling price | 10–20, from **both** agents, including at least one CHD/INF, one multi-passenger booking, one partner sale | Decide how the app reads them (plain code vs AI), and start the ~200-ticket golden set |
| 2 | `ticket-screens/` | The same tickets as **pasted text** if staff can copy from Amadeus (a `.txt` per ticket) | 5–10 | If pasting works, reading costs nothing and is 100% exact |
| 3 | `amadeus-reports/` | End-of-day Amadeus daily reports (`.xlsx`) | 5–10 days, including the days of the screenshots above, and **at least one day with RFND, CANX/VOID and an EMD/extra** | Matching rules between screenshots and the report |
| 4 | `excel-sheets/` | The accountant's current Excel workbook(s), as they really are (all tabs) | the current month + one closed month | This is the sheet the app must reproduce; we copy its columns and totals |
| 5 | `invoices/` | Invoices sent to partners and to clients (PDF, Word or photo) | 5–10, including one paid late and one partly paid | Invoice layout, numbering, legal mentions, due dates |
| 6 | ~~`bank-and-mobile-money/`~~ | ~~Bank statements and mobile-money exports~~ | – | **Not available.** Payments are recorded in the app instead of matched to statements |
| 7 | ~~`airline-bsp/`~~ | ~~BSP / airline billing statements~~ | – | **Not available.** No automatic check of airline billing in the first version |
| 8 | `notes/` | Answers to the interview guide ([01-accountant-interview.md](01-accountant-interview.md)), typed or as voice notes | 1 | The routine, the exceptions, the words people use |
| 9 | `notes/` | List of partners (BARAKE, BOUCHRA…) and regular credit clients, with their usual payment terms | 1 | Who gets invoices and reminders |
| 10 | `notes/` | Staff list: Amadeus sign (0004SV…), real name, phone, role | 1 | Accounts and who may edit what |
| 11 | `notes/` | Which airline is **code 495** | – | Shown as "Cie 495" today |

## Minimum to start

If collecting everything takes time, **#1, #3 and #4 for the same 3 days** are enough to start
the matching design. The rest can follow.

## Privacy

- Files stay in `intake/` on this laptop. They are never committed, never uploaded to GitHub.
- The screenshots in `intake/` are only for the design-phase reading test. In the live app,
  screenshots are read and discarded, never stored (decision 3 in
  [02-open-decisions.md](02-open-decisions.md)).
- Anything used in automated tests is replaced by made-up look-alikes first (as with `1OCT`).
- When the AI-reading test runs on real screenshots, only the ticket image is sent, with
  no-training API terms; you will be told before that happens.
