# The chat

Status: proposal for approval. The chat is the main way to use the app (decision 1: inside the
app, installed on the phone; not WhatsApp). Every chat action goes through the same records and
rules as the screens; the chat is a front door, not a separate system.

## 1. Principles

- **Nothing is saved without a confirmation.** The app reads back what it understood; the person
  taps *Confirm* or corrects it.
- **The AI never sets a price** and never writes to the database by itself. It proposes an entry
  or picks a fixed, read-only report. No free-form SQL.
- **Each person sees only what their role allows** (same rules as the screens; agency data
  separated at the database level).
- **Plain code first, AI only when needed** (PLAN.md "AI usage"); every AI call logged per agency
  for cost.
- Languages: French and Arabic (Hassaniya understood); numbers like `45000`, `45 000`, `45k`.

## 2. Agents

| They say / send | The app does |
|---|---|
| a ticket screenshot or pasted Amadeus text | reads it, asks what is missing (AG, P.B, CLT, AP, payment), reads back, saves on *Confirm* (doc 05 §2) |
| "annule 3940299439" or a CANX screenshot | records the cancellation (doc 05 §3) |
| answers a flag ("Amadeus a raison" / "c'est moi, 45 200 car…") | records the decision (doc 05 §4) |
| "BADI a payé 60 000 cash" | prepares a payment on BADI's account, reads back, saves on *Confirm* |
| "mes billets d'aujourd'hui" | lists their tickets of the day and what is still missing |

The app also **writes first**: "3 tickets in today's Amadeus report are not in the app yet:
…, please send them" (flag `NOT SENT`), and reminders for open flags.

## 3. Accountant and manager

| They ask | The app answers with |
|---|---|
| "feuille du 19 septembre" | the TJQ APRES of that day + Excel download |
| "qu'est-ce qui bloque aujourd'hui ?" | open flags per agent, missing tickets |
| "situation BADI" | the partner statement, current balance |
| "qui nous doit plus de 30 jours ?" | overdue list by age |
| "bénéfice de la semaine", "bénéfice Turkish octobre" | figures from the fixed report functions |
| "facture impayées de <client>" | the unpaid-invoice document |
| "conseils" | the weekly advice (§4) |
| approves a correction | records the validation (decision 18) |

## 4. AI advice (weekly, and on request)

Read-only suggestions, each labelled *Conseil*, never applied by the app:
- partners or clients paying later and later; the largest overdue balances;
- costs that rose (penalties/ADM, change fees) and where they come from;
- agents with many corrections or missing tickets;
- direct-sale margins by airline or route compared with past weeks (facts only: **no price is
  ever suggested**, decision 14).

## 5. What it costs

Estimated about $5–15 a month per agency with Haiku for routine reading and Sonnet for free
questions and advice; pasted text is read by plain code at no cost. Confirmed after the
ticket-sample test ([00-data-request.md](00-data-request.md) #1–2).
