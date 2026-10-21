# Open decisions, with a proposed default for each

Each line has a **proposed default** so design can continue. Tick it, or write the change.
Nothing here is built until approved.

| # | Question | Proposed default | Why | Needed by |
|---|---|---|---|---|
| 1 | Where does the chat live? | **In the app** (installed on the phone like an app), not WhatsApp | WhatsApp Business API costs per message, needs Meta approval, and messages leave our control; the app already has logins and agency separation | Chat design |
| 2 | Pasted text as well as screenshots? | **Both**; pasted text preferred | Pasted text is read by plain code: free and exact. Screenshots stay as the fallback | Extraction design |
| 3 | Are screenshots kept? | **Decided: no.** The image is read in memory, the extracted fields are shown back to the staff member to confirm, then the image is discarded (never written to disk or the database). We keep only the confirmed fields, who confirmed them and when, and a fingerprint (SHA-256) of the image so the same screenshot is not entered twice | Owner's decision. Less personal data held. The proof of a price becomes the staff member's confirmation, not the picture | Storage |
| 4 | Who may edit a confirmed entry? | Staff: **their own entries, same day only**. Manager and accountant: **any entry**, with a mandatory reason. Every edit kept in the history | Mistakes get fixed fast; nothing disappears | Ledger |
| 5 | Do edits on a closed day need approval? | **Yes, the manager approves** edits to a day the accountant has closed | Totals already reported must not move silently | Ledger |
| 6 | Invoice due date counted from? | **Invoice date + 15 days** | Standard practice; the interview confirms it | Invoices |
| 7 | Who gets the "due within 3 days" and "overdue" alerts? | **Manager and accountant**, in the app; overdue also in the daily report | Keeps reminders internal until we know how clients react | Invoices |
| 8 | Remind clients and partners directly? | **Not at first.** The app prepares the message; a person sends it | Avoid an automated message going wrong with a good partner | Invoices |
| 9 | Partial payments? | **Allowed**; the invoice stays open until fully paid | Usual with partners | Invoices |
| 10 | Partner billing | **Per ticket at sale, grouped on one invoice per partner per period** (period chosen per partner: weekly or monthly) | Fits both habits | Invoices |
| 11 | Payment methods | **Cash, Bankily, Masrvi, Sedad, bank transfer, cheque, on credit** (editable list per agency) | From the interview so far | Ledger |
| 12 | Voice messages | **Later** (Phase 4), not in the first chat version | Text and screenshots first; voice adds cost and errors | Chat |
| 13 | Revenue definition | **Revenue = P.B (Bruge price) of every sale. Gross profit = P.B − Amadeus cost (+ airline commission COMM).** For a partner sale this equals the partner fee; for a direct sale it is the amount the agent added. Margin = profit / revenue | P.B, AG, CLT, AP explained by the owner ([04-findings-first-files.md](04-findings-first-files.md) §5) | Reports |
| 14 | Does the AI ever set or suggest prices? | **Never sets a price.** Advice is limited to costs, late payers, trends; every suggestion is labelled as advice | Your rule: prices only come from staff | AI advice |
| 15 | AI model | **Haiku for routine reading, Sonnet for free questions and weekly advice**, decided after the golden-set test | Cost about $5–15/month per agency | Cost model |
| 16 | Airline code 495 | Ask the agency | – | Reports |
| 17 | Real staff names in the app | Use real names from the staff list (`intake/notes/`) | – | Accounts |
| 18 | Who approves a difference between the app report and the Amadeus report? | **The issuing staff member confirms** which value is right; **the accountant or manager validates** before a cost, ticket number or ticket type is corrected. A passenger-name spelling fix needs the staff member only | Cost and ticket changes move money and must match what the airline bills | Comparison |
| 19 | Partners: invoices with a 15-day deadline, or a running account? | **Running account per partner**, statement per half-month (as the BADI sheet does today); alert when a period's balance is unpaid 15 days after the period ends, critical 3 days before. Clients keep per-invoice due dates | The BADI file shows partners pay lump sums against a balance, not invoice by invoice ([04-findings-first-files.md](04-findings-first-files.md) §3) | Invoices |
| 20 | The existing billing software is custom and cannot be linked: what happens to invoices? | **The app takes over** client invoices, unpaid-invoice lists and client statements (same documents and agency details as today), and partner statements. While both run, staff stop typing into the old software for each module the app takes over, so nothing is typed twice | The old software cannot export or connect; running both in parallel for long means double entry and two different balances | Invoices |
