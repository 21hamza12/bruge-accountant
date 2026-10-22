# Build plan (after the design is approved)

Each phase follows the same workflow: detailed plan → tests first → code → reviews (code, Python,
FastAPI, database; security review for anything that touches logins, uploads, AI or money) →
CRITICAL/HIGH findings fixed → merged. Each phase ends with something the agency can use.

The order puts the **daily workflow first** (it replaces the accountant's daily Excel work) and
the AI parts after it, so the app is useful even before screenshot reading is ready.

| # | Phase | What the agency can do after it | Needs from you |
|---|---|---|---|
| **A** | **Fixes on today's app** — cancelled tickets out of totals and questions (in progress) | correct totals on the current dashboard | – |
| **B** | **Daily workflow, without AI** — partners with fees; agents enter each ticket by **pasted Amadeus text** or a short form (P.B, AG, CLT, AP, payment); app report; upload of the Amadeus report; comparison and flags; approvals; TJQ APRES stored and exported for any day | the full daily routine of doc 05, except reading screenshots | the partner list with current fees |
| **D** | **Reading screenshots** — test on the ~200-ticket set your accountant checks, choose the model, then plain-code + AI reading with read-back | agents send a screenshot instead of pasting | 10–20 samples now, then the ~200 checked tickets |
| **E** | **The chat** — agents send tickets, answer flags and record payments in a conversation; accountant and manager ask for sheets, flags, balances (doc 07) | everything by chat, in French or Arabic | – |
| **C** | **Go live + parallel run** — paid hosting (about $15/month), backups, monitoring; for **2–4 weeks the accountant keeps her Excel too**, and each day's TJQ APRES is compared with hers | real daily use from any phone; proof that the app's sheet equals hers | the accountant's daily APRES during the parallel run |
| **F** | **Partner accounts** — half-month statements, manual lines (ADM, refund, change fee), payments, balances, partner statement PDF/Excel | the BADI-style Excel files are no longer needed | opening balances on switch-over day |
| **G** | **Client invoices** — invoices, credit notes, payments, unpaid-invoice list, client statement, 15-day and 3-day alerts, prepared reminder messages | the old invoicing software is no longer needed | opening balances, numbering choice, one of each document |
| **H** | **Reports and advice** — revenue, profit, margin by day/week/month, by agent, airline, partner/direct; overdue by age; weekly AI advice | the manager's dashboard and morning summary | – |
| **I** | **Later** — visas/hotels/Hajj as products, voice messages, other agencies | – | – |

**Order changed on 22 Oct (owner, decision 23):** after B come **D (screenshots) and E (chat)**,
then **C (go live)**, so the agency starts with the chat and screenshots from day one. Agents can
also paste the PNR text (doc 10); screenshots and the chat bot for agents, accountant and manager
stay core features.

## Before go-live (end of phase C)

- The release checklist (nine items: tenancy isolation, password-reset expiry, input
  sanitisation, API origin restriction, rate limiting, error screens, indexes, logging and
  alerting, rollback) passed with evidence.
- A restore of the backup tested once.
- Staff accounts with real names and signs; the 495 airline name.

## Switch-over days

- **Partner accounts (F):** each partner's balance from its Excel file on the day; from then on
  partner tickets and payments go only into the app.
- **Client invoices (G):** each client's open invoices from the old software on the day; the old
  software is kept read-only for past invoices.

## Done when

The accountant can close a day, produce any day's TJQ APRES, see who owes what and send
reminders **without opening Excel or the old software**, and her parallel-run sheets matched the
app's for every day of the run.
