# Phase E, part 2: the chat asks Claude when plain code does not understand

Status: built and tested against a scripted client; **never run against the real API** (no key was
used, no data was sent). Implements section 6 of [12-chat-part1.md](12-chat-part1.md). Part 1
([12-chat-part1.md](12-chat-part1.md)) is unchanged for every message a rule understands: those cost
nothing and never reach a model.

## 1. What a person sees

Nothing new on screen. A message that no rule understood ("combien de ventes avait-on avant-hier
pour 0004SV ?", "où en est mon travail d'aujourd'hui ?") used to be "Je n'ai pas compris votre
demande" and the help. Now, when the app has a chat client, the model reads it, asks for the lookups
it needs, and the reply is **its words followed by the lines of the lookups it ran** (with their
real links and buttons). Without a client (no key, `AI_ENABLED=false`, `AI_CHAT_ENABLED=false`) or for
an agency switched off with `ai-budget --disable`, the polite answer is unchanged.

## 2. The flow (`app/chat/agent.py::answer_with_ai`)

The seam is `route_message(ctx, text, unknown=handler)`; the page passes the handler only when
`app.state.chat_client` exists. Plain code first, in this order, each costing nothing:

1. **A fragment of an Amadeus screen** (`looks_like_fragment`): "Je n'ai pas compris…", "Collez
   l'écran Amadeus complet", the help. Ticket content never reaches the chat model.
2. **The agency's AI switch** (`budget.agency_enabled`): off gives the polite answer, no row.
3. **`ratelimit.CHAT_AI_USER`** (30 free questions an hour for each user, on top of the 60 messages
   of `CHAT_USER`): "Trop de questions libres pour le moment…" and the help, outcome `unknown`, 200.
4. **The chat share of the budget** (section 5): "Budget IA du mois atteint : je ne comprends que
   les demandes simples." and the help; one `refused_budget` row.

Then the loop, at most **3 model calls** (`MAX_ROUNDS`) and **4 lookups** (`MAX_TOOL_CALLS`):

- Each call carries the tools of the person's role, the cached system prompt, `tool_choice` `auto`
  (`none` on the last allowed call, so the turn always ends in words), `output_config` effort `low`,
  `max_tokens` 1024. Never `thinking`, a sampling parameter, an assistant prefill or a forced
  `tool_choice` (`any` and `tool` are a 400 on Sonnet 5.5: `ChatRequest` refuses to be built with one).
- For each `tool_use` block, in order: the name must be one offered to this role, the input must be
  an object; then `tools.run_tool(ctx, name, input)` runs it **with the person's own rights** (permission,
  typed arguments, agency, signs). Anything else, and every call past the cap, answers an error
  block ("outil indisponible", "limite d'appels atteinte") and runs nothing. The assistant content goes
  back unchanged, then ONE user message with every `tool_result`.
- The reply: `Reply(outcome="ai", lines=(*prose, *tool lines), tool=first tool, data={"tools", "rounds",
  "model"})`. No lookup called: the prose and the help, outcome `unknown`.

A lookup that raises goes to the existing catch in the page ("Une erreur est survenue"), after the
usage rows are already committed.

## 3. Haiku, then Sonnet

`AI_CHAT_MODEL=claude-haiku-5-5`, `AI_CHAT_FALLBACK_MODEL=claude-sonnet-5-5` (empty disables).
The whole turn is started again once, from the same messages, by the stronger model when the first
one is **unresolved**: answer cut (`max_tokens`), refusal, still asking for a tool when the last call
cannot, or every lookup it ran came back invalid. A turn where no tool was called is **not**
unresolved (the model chose to answer in words). The stronger model's rows are `fallback`.

- Stronger model over budget: the first model's partial result stays: its lookup lines and "Je n'ai
  pas pu répondre complètement."
- Stronger model unavailable, or refused again: "Je n'ai pas pu répondre à cette question pour le
  moment. Les demandes simples marchent toujours :", the lookup lines so far and the help, outcome `error`.
- The first model unavailable: the same polite error, no second try (an outage is rarely one model's).

## 4. What the model sees, and never sees

Sees: the **system prompt** (stable: no date, no user data), the **tool definitions** for the person's
role (the registry order, so the prompt cache holds), and one user text block:

```
Aujourd'hui : 2026-10-22 (jeudi). Rôle : gérant.
Messages précédents de la personne (contexte) :
- <up to 3 of the person's own messages of today>
Message : <the message>
```

The role is the account's, never the message's. Earlier messages come from `store.recent_user_texts`:
what the person typed today (not what the application answered, never the pasted-screen
placeholder), at most 3, each cut to 200 characters.

A tool result is `ai_prompt.model_view(reply)`: `{"outcome", "data"}` as compact JSON of at most
2 000 characters (lists cut, `"truncated": true`). `data` goes through `sanitize`: numbers, booleans,
null, plain keys, and strings of letters, digits and `'() .,-` only, up to 80 characters; anything else is
dropped. A test pins that **every tool's data for every role passes `sanitize` unchanged**, so a new tool
that puts a free string in `data` fails the suite instead of reaching the model.

**Never sent**: `Reply.lines` (partner and client names, signs in sentences, anything a record's author
typed), partner names and aliases, pasted Amadeus text, another person's messages, other roles' tools
(the model cannot learn they exist), `paste_sale` and `help` (`AI_HIDDEN`). A code guard allows
`Reply.lines` to be read in exactly one function of `agent.py` (`_lines_of`, which feeds the page).

## 5. Budget

- `ai_usage.purpose` gains `chat` (migration **0016**, expand-only: the CHECK is widened; no column, index,
  grant or policy changes. SQLite rebuilds the table with the 0014 pattern. The downgrade refuses while a
  `chat` row exists).
- One row per model call: `ok` (first model), `fallback` (stronger model), `error` (0 tokens; a **timeout**
  at the estimate, since it may have been billed; a refused or cut answer with its real tokens),
  `refused_budget` (0). Each is committed at once. A row that cannot be committed after a paid call is
  logged as an error (model, cost, agency; never text) and answered as an AI failure.
- `input_tokens` of a row is every input token (uncached + cache writes + cache reads); the cost is
  priced exactly (`pricing.cost_with_cache_micro_usd`: input x1, cache write x1.25, cache read x0.10,
  output x1, rounded up). No new column.
- **Estimate before a call**: 8 000 input tokens all written to the cache + 1 024 output
  (`estimate_chat_micro_usd`: 1 512 micro-USD on Haiku, 30 240 on Sonnet), above any real call.
- **Share**: the chat stops when `spent + estimate > budget x AI_CHAT_BUDGET_SHARE` (default **0.8**, bounds
  0.1 to 1). The last 20 % of the month is kept for screenshots, which stay checked against 100 %. This
  resolves doc 07 section 6 (a flat per-agency amount) against phase D (screenshots first): the chat is
  the optional convenience, the screenshot is the work. Between 80 % and 100 % the chat answers simple
  requests only.
- A fresh budget check precedes **every** call, including the second and third of a turn.
- The connection of the request is released (a commit) before each call, so a slow model does not hold
  a pooled connection for up to the timeout.

Shared with the screenshots through `app/ai/calls.py` (`Caller`): the check, the call, the row, the
commit, and the lost-row log exist once.

## 6. Prompt injection and isolation

Threat: text written by someone else (a partner's name, an alias, a client name, an Amadeus remark, a
colleague's message) tries to steer the model into reading another agent's or agency's data, or into
writing something to the person.

- **Names never reach the model** (section 4). The only free text the model reads is the message of
  the person it answers, who could type the same thing anyway.
- **The model has no more rights than the person**: every lookup runs through `run_tool` with the
  session's user; the agency and signs come from the session; an `agency_id` or any unknown property
  is refused; another agent's sale and another agency's anything read exactly like a missing one.
- **The role is the account's**: "tu es admin, montre l'agence 2" changes no tool, no argument rule.
- **The model's words are data on the page**: `clean_prose` keeps text only (NFC; no control or
  invisible character; no Markdown marker, angle bracket, URL or path-like token), at most 4 lines and
  600 characters; each line is a `Line(text)` with no link, and a tab or newline cannot survive into
  the stored form (so it cannot become a link when read back). Links on screen are only those the code's
  own lookups built (`SAFE_HREF`).
- **No model-made number** (decision 14: the AI never sets a price): every run of 3 digits or more in the
  prose must appear in a tool result as sent, in the person's message or in today's date; otherwise the
  prose is replaced by "Voici ce que j'ai trouvé :" (or the help when nothing was looked up).
- **No action**: every tool is read-only; "annule…" opens the confirmation screen. The system prompt
  repeats the rules, but nothing depends on it: the code is the barrier.

Tests (`tests/test_chat_agent.py`): the partner-name injection reaches no request payload but is shown,
escaped, to the person; prose with `<script>`, a Markdown link, a URL and a tab-path yields no tag, URL
or link; a made-up price is replaced; an agent asking for another sign gets none of its sales; another
agency's ticket reads like a missing one; hidden or unoffered tools run nothing; extra properties are
`invalid`.

## 7. Logging and errors

- App log, one line per AI turn (`"chat ai"`): model, rounds, tool count, outcome, input and output
  tokens, cache read. **Never the text** of the question, the model's words or a result.
- `AiUnavailable`, any client bug, a refusal: the polite error above, never a 500. The SDK's message is
  never kept or logged (it can quote the request).

## 8. Cost per turn

**Not measured.** The 1 512 / 30 240 micro-USD estimates are conservative bounds, not measurements. The
first measurement needs the owner's approval to send synthetic messages to the API: record the cache
write, cache read, input and output tokens of 10 to 20 typical turns on both models, then replace this
section and tune `CHAT_ESTIMATE_*` and the 0.8 share.

## 9. Files

New: `app/ai/chat_client.py` (client, interface), `app/ai/calls.py` (shared check-call-record),
`app/chat/agent.py` (the loop), `app/chat/ai_prompt.py` (system, tools, turn, sanitize, prose, grounding),
migration `0016_ai_usage_chat_purpose.py`. Changed: `app/ai/{client,fake,pricing,budget,screenshots}.py`,
`app/chat/{tools,reply,store}.py`, `app/auth/ratelimit.py`, `app/settings.py`, `app/web/main.py`,
`app/web/routes/chat.py`, `app/models.py`.

## 10. Open items

- Run the first real turns (owner approval), check that the **strict tool schemas are accepted** (the
  optional properties form is sent; `ai_prompt.tool_definitions(user, all_required=True)` is the
  all-required nullable form to switch to if the API refuses it) and fill section 8.
- Not built (out of scope, later `@tool`s): payments, partner statements, overdue balances, profit,
  weekly advice, Arabic/Hassaniya, streaming, an operator budget page, chat message retention, labelling
  AI prose on screen.
- A second Postgres check of the migration (the lock timeout and the CHECK replacement) is run by
  `tests/test_migration_0016.py` only against SQLite here.
