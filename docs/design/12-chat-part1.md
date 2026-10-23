# Phase E, part 1: the in-app chat without AI

Status: built. Implements the plain-code half of [07-chat.md](07-chat.md): "plain code first, AI only
when needed" (PLAN.md, "AI usage"). The chat is a front door onto the screens that exist; it adds no
rule and writes nothing but the conversation itself.

## 1. What a person sees

- A **Discussion** page (`/chat`), in the sidebar for every role that may use the sales screens
  (staff, manager, accountant: `permissions.can_use_chat`). The bottom bar of a phone already holds
  its five tabs, so the page is also one tap from the home page ("Discussion").
- The last **50 messages of today** (the application clock's day) of **their own** conversation, newest at the bottom (CSS only, no
  script), a text box and a *Envoyer* button. The form is posted with htmx and the whole panel
  (thread and an empty form) is swapped in place; without htmx it is a plain POST that redirects
  back to the page (an Amadeus paste renders the page instead, because its hand-off form exists in
  that one response only). A refused message comes back in the text box.
- Links in a reply open the existing screens (a sale, the flags of a day, the void screen, the
  TJQ APRES download). **Nothing is saved, voided or approved from the chat.**

## 2. What it understands (the router)

`app/chat/router.py::parse_message` reads the French text with accents, case and punctuation
ignored (`app/chat/text.py::fold`), tolerates extra words, and picks the first matching rule:

| Message (examples) | Tool | Who |
|---|---|---|
| a pasted Amadeus screen (an `RP/` line, or two element lines) | `paste_sale` | sales screens |
| "annule 3940299439", "annuler le billet ...", "supprime ..." (an imperative or infinitive verb and a ticket) | `void_link` | sales screens |
| "billet 3940299439", a bare number, `495-3940299439`, and any other sentence that holds a ticket number ("pourquoi le billet ... est dans les écarts") | `ticket_status` | sales screens |
| "aide", "menu", "?", "que sais-tu faire" (a message of one to three words, or one of those phrases) | `help` | every chat user |
| "feuille du 19 septembre", "feuille d'hier", "feuille 19/09", "tjq apres ...", "export ..." (not a lone "après") | `day_sheet` | manager, accountant (`can_export_day`) |
| "billets non envoyés", "manquants" | `not_sent_tickets` | sales screens |
| "qu'est-ce qui bloque (aujourd'hui) ?", "flags", "écarts" | `open_flags` | sales screens |
| "mes billets (d'aujourd'hui)", "billets du 19/09", "billets de 0004SV" | `list_sales` | sales screens |
| anything else | `unknown` (see section 6) | |

More specific rules come first. **A ticket number decides on its own**, before the words around
it: with an imperative or infinitive cancel verb it is a void, otherwise a status (so "je cherche
le billet ... annulé ?" is a status: the participle "annulé" is a question, read from the accent
as typed; "annulation ..." alone is a status too). "Billets non envoyés" is not a list of sales.
A run of **six digits or more** that is not a valid ticket number is answered "Je n'ai pas compris
le numéro du billet (10 chiffres)", never a list of today's sales, unless the sentence is about an
amount ("payé", "montant", "cash", "mru", ...).

Dates: today, "hier", "avant-hier", `19/09`, `19.09`, `19.09.26`, `2026-09-19`, `2026/09/19`,
`19 septembre`, `19 sept`, `1er octobre`; without a year, the latest such day that is not after
today (`29/02` is the latest 29 February that is not after today). A dotted date is `dd.mm`
(`dd.mm.yy`) with a valid day and month: `4.5k` and `12.50` are amounts, not dates. A date that
does not exist ("31/02") is answered with "Je n'ai pas compris la date" and an example, never
guessed; so is a date the reader cannot use (a weekday such as "lundi", "demain", a slashed number
that is not a date such as `19/09/2026/5`): it is never replaced by today. Ticket numbers: ten
digits, or airline code plus ten digits, normalised with `sales_rules.ticket_number` like the sale
form. A staff sign is read as `0004SV`, or the word after "signe", "agent" or "code" when it has
the shape of a sign (not "hier", not "12eme"). Part 1 reads no amount (the reader that came with
the first draft had no caller and was deleted; part 2 adds one with the payments tool).

## 3. The registry (`app/chat/tools.py`)

A `Tool` is `name`, French `description` and `examples`, a permission predicate from
`app/permissions.py`, typed `params` and a `handler(ctx, args) -> Reply`.

| Tool | Does |
|---|---|
| `list_sales(day?, sign?, mine?)` | the sales of a day the user may see, with what is missing on each: the client's phone of a direct sale, and open flags on its tickets. A manager or accountant saying "mes billets" gets the sales they entered; otherwise every sign. Staff always get their own signs. 20 rows, then a link to the sales screen. |
| `open_flags` | open blocking flags: a count per sign for manager and accountant, a list for everyone, links to `/flags?date=` |
| `not_sent_tickets` | open NOT SENT flags, each linking to the existing entry form (`/flags/{id}/enter`) |
| `ticket_status(doc)` | the sale of a ticket (day, sign, AG, what is missing) and its open flags, if the user may see it |
| `day_sheet(day?)` | a link to the existing TJQ APRES: the stored export of a closed day, else the draft; never rebuilt in the chat |
| `paste_sale(text)` | reads the paste with `sale_paste.read_paste`, says how many sales it found, and (that response only) offers a form that posts the text to the existing `POST /sales/paste` |
| `void_link(doc)` | when the user may void that sale, a button to `/sales/{id}/void`; otherwise why not |
| `help` | the tools the role may use, with an example each |

Rules every tool follows:

- **Permission twice.** `Tool.run` checks the predicate, and the `tool(...)` decorator wraps every
  handler with the same check, so a handler called directly cannot skip it.
- **Agency and user from the session** (`ChatContext`), never from the message. The services reused
  (`sales.sales_for_day_for`, `sales.get_sale_for`, `flags.flags_for_me`, ...) already filter on the
  agency and, for staff, on their signs. Another agent's ticket, a ticket of another agency and a
  ticket that does not exist get the same words ("Aucun billet ... trouvé parmi vos ventes").
- **Read-only, no raw SQL.** The text of a message only ever becomes a tool name, a date, a
  ten-digit number or a staff sign. No handler writes.
- **Typed arguments.** `Tool.check_args` turns raw values into a `date`, a ten-digit string, a
  normalised sign, a boolean or text, refuses unknown names (an `agency_id` is not an argument) and
  gives a French message for a missing or unreadable one. `Tool.input_schema()` is the JSON schema.
- **A `Reply`** (`app/chat/reply.py`): `outcome` (`ok`, `unknown`, `denied`, `invalid`, `error`), `tool`,
  structured `data` (ids, counts, ISO dates), and `lines` (text, optional link, optional button).
  Replies are built from fixed phrases and typed values; nothing the person typed is echoed.

## 4. Storage

`chat_messages` (migration **0015**): `agency_id`, `user_id` (composite foreign key to `users`,
`ON DELETE RESTRICT`), `role` (`user` or `app`), `text` (1 to 4000 characters, CHECK), `created_at`;
index `(agency_id, user_id, created_at)`. **Append-only**: Postgres has `tenant_read` and
`tenant_insert` policies only (FORCE ROW LEVEL SECURITY) and the app role gets `SELECT, INSERT`;
SQLite has abort triggers on UPDATE and DELETE.

- **Each person reads only their own conversation.** Row-level security separates agencies; the
  separation of people is the query (`store.recent` filters on the session's agency and user), and
  the manager has no way to ask for another user's rows.
- **Only today is shown.** The table is append-only and keeps every message for audit, but the
  page shows the current day's conversation only (`store.recent`: messages since midnight of the
  application clock, at most 50). An old reply made with rights the person no longer has (a manager
  since demoted, a sign since reassigned) therefore never comes back on screen. Retention of old
  rows is open: a purge would have to be done by the owner role (the app role has no DELETE);
  how long to keep them is to be decided.
- `ChatMessage` is queried in `app/chat/store.py` only (a code guard in `tests/test_code_guards.py`).
- A reply is stored as lines (`text`, or `text<TAB>/path` for a link, `text<TAB>>/path` for a
  button). On reading, every link is checked again (`reply.safe_href`: a site path of plain
  characters); anything else is shown as plain text.
- **No ticket screenshot and no pasted Amadeus text is stored**: the person's message is saved as
  `[Texte Amadeus collé]` and the reply keeps only the number of sales read. So is a **fragment**
  typed in or pasted in part (one element line such as `1.DUPONT/JEAN MR`, a fare line `FA` / `FP`,
  or a `NAME/FIRST` with its title): `router.looks_like_fragment`, `store.stored_text`.
- `down_revision` is `"0013"` for now. After phase D (0014) is merged, change it to `"0014"` and the
  head in `tests/test_migration_0015.py` stays `"0015"`.

## 5. Security

- CSRF on the router (synchronizer token and Origin check, like every POST). A token of another
  user is refused.
- **60 messages an hour for each user** (`ratelimit.CHAT_USER`), counted first and committed, so a
  refused message counts; the first refusal of a window is one `rate_limited` audit row.
- **Size first, then content.** The request body is capped at about 10 MB, so nothing may read a
  posted text before its size is known: the characters and the UTF-8 bytes are compared with the
  paste screen's 16 KB first, and `looks_like_amadeus` looks at the first 20 000 characters only
  (its `RP/` pattern is `^[ \t]*RP/\S`: with `\s*` a text of blank lines was quadratic).
- **Length**: 500 characters after cleaning (control and invisible characters removed, one line);
  an Amadeus paste is allowed up to the paste screen's 16 KB. A refused message is not stored and
  comes back in the text box (the box takes up to 16 KB, so a paste is possible; a French hint
  under it states both limits).
- **A lookup that fails** is answered "Une erreur est survenue, réessayez." (outcome `error`); the
  log gets the exception's class name, never its message or the person's text.
  htmx does not swap a 4xx answer, so for an htmx request the refusal (empty, too long, rate
  limited) is a 200 with a notice in the panel; any other request gets the real status
  (400, 429 with `Retry-After`).
- `Cache-Control: no-store`; no inline script or style (the CSP is unchanged); every value is
  autoescaped.
- **Nothing sensitive is audited or logged**: no message text in `audit_events` and none in the log.
  The log line of a message has the outcome name, the tool name, whether it was a paste and a
  character count.

## 6. What part 2 adds

One seam: `route_message(ctx, text, unknown=polite_unknown)`. A message no rule understands calls
`unknown(ctx, text)`, which must return a `Reply`. Part 2 passes a function that:

1. checks that AI is on for the agency and the monthly budget is not spent (phase D, `ai_usage`),
   otherwise falls back to `polite_unknown`;
2. gives the model `TOOLS` filtered by `tools_for(ctx.user)` (name, `description`,
   `input_schema()`), so the model sees only what the role may do;
3. runs the tool the model names with `run_tool(ctx, name, args)`, the same permission and
   argument checks as a typed message, and gives it `Reply.data` to phrase an answer;
4. logs the call per agency (model, tokens, cost; no ticket content).

Still plain code in the same registry, ready for the AI: payments ("BADI a payé 60 000 cash"; the
amount reader is to be written), partner statements, overdue balances, reports and advice (doc 07 sections
3 and 4). Each is a new `@tool(...)`; the router needs no change for the AI to use it. A write
(a payment, a cancellation) stays a link or a read-back with *Confirm*, never a silent action.

## 7. Tests

`tests/test_chat_text.py` (reading), `test_chat_router.py` (parsing and the seam),
`test_chat_tools.py` (every tool for every role, isolation between agents and agencies, typed
arguments, no write), `test_chat_pages.py` (page, CSRF, limits, rate limit, privacy, escaping,
paste hand-off, history, query budget), `test_migration_0015.py`, and the Postgres cases added to
`test_rls.py` (policies, grants, append-only, cross-agency insert; they run only with
`TEST_DATABASE_URL`). `/chat` is in the query budget of `test_query_counts.py`; `test_chat_pages.py` also counts the
statements of `POST /chat` ("mes billets" reads the staff signs once; a ticket lookup filters the
flags by ticket in the query).
