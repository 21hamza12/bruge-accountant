# Design phase: the money-control system

Design documents written and approved **before** any code for the post-funding scope (ticket
intake by chat, D+1 reconciliation, daily sheets, edits with history, invoices and reminders,
AI advice). Real agency files go in `intake/` (git-ignored, local only).

| File | What | Status |
|---|---|---|
| [00-data-request.md](00-data-request.md) | Files needed from the agency | Ready to send |
| [01-accountant-interview.md](01-accountant-interview.md) | Questions for the accountant | Ready to use |
| [02-open-decisions.md](02-open-decisions.md) | Open decisions with proposed defaults | Waiting for the owner |
| [03-money-model-draft.md](03-money-model-draft.md) | Draft money flow, records, reconciliation | Draft, to check against the data |
| [04-findings-first-files.md](04-findings-first-files.md) | What the first real files show, and questions for the agency | Done |
| [05-daily-comparison.md](05-daily-comparison.md) | Daily workflow: app report vs Amadeus, flags, approvals, TJQ APRES | Approved |
| [06-partners-and-invoices.md](06-partners-and-invoices.md) | Partner accounts, client invoices, payments, reminders | Approved |
| [07-chat.md](07-chat.md) | What agents, accountant and manager do in the chat; AI advice | Approved |
| [08-build-plan.md](08-build-plan.md) | Phases A–I, in order, with what each delivers | Approved |
| [10-ticket-screens.md](10-ticket-screens.md) | What the ticket screenshots show: pasteable PNR text, RM per passenger, two ticketing offices | New |
| [11-screenshots.md](11-screenshots.md) | Reading Amadeus screenshots with Claude: measured results, models, fallback rule, privacy, budget | Built (phase D) |
| [09-phase-b-plan.md](09-phase-b-plan.md) | Phase B step by step (B1–B11), conventions, approved defaults | Approved |
| [12-chat-part1.md](12-chat-part1.md) | Phase E part 1: the in-app chat without AI (router, tool registry, storage, security) | Built |

Still to come once the ticket samples arrive: how screenshots are read (plain code or AI), the
reading test on the ~200 checked tickets, and the measured AI cost.
