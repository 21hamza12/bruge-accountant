# The parallel-run comparison

Status: built in phase C1 (item 4 of `14-go-live.md` §2). It serves C7, the parallel run of 2 to 4
weeks in which Bruge works in the app while the accountant keeps doing her Excel APRES.

## 1. What it does

Every closed day with a stored TJQ APRES has a page, **"Comparer avec la feuille de la comptable"**
(`/days/{yyyy-mm-dd}/compare`, a button on the day page). A manager or the accountant
(`permissions.can_compare_apres`, the same roles as the export) chooses the accountant's own APRES
file for that day. The app reads it and shows:

* **Lignes communes**: tickets present in both files, and how many of them are identical;
* **Cellules différentes**: every differing cell, with the ticket, the column, her value and the
  app's value;
* **Seulement dans sa feuille** and **Seulement dans l'application**: tickets present on one side
  only.

When nothing differs the page says "Aucune différence."

The app's side is the **latest stored version** of the day's export (the one a download gives), not
a rebuilt one: it is exactly what the accountant would have been given.

Nothing is changed by it. The file is read in memory, compared and dropped: it is not written to
the database, to disk or to the logs. The only trace is one audit event (`apres_compared`) with
counts (day, version, matched, identical, differing cells, rows only on each side), never a name, a
file name or a cell value. Reloading the result page asks the browser to send the file again; the
result is not kept anywhere.

## 2. What counts as a difference

Rows are **aligned by ticket**: the DOC NUMBER normalised to 10 digits as everywhere else (a 13-digit
number whose first three digits are the airline loses them). A number that cannot be normalised
aligns only with the very same text. If a number appears twice in a file, the first line counts and
the page says how many were dropped.

For the rows on both sides, the 12 columns of the export are compared (SEQ NO, A/L, TOTAL DOC,
PAX NAME, AS, TRNC, RLOC, P.B, AG, CLT, AP; DOC NUMBER is the key):

* **Money** (TOTAL DOC, P.B) as a decimal number **compared to the cent** (a negative zero such as
  -0.004 is `0.00`). A non-empty money cell that is not an amount ("abc", "12 EUR") is shown as
  her own text, never as empty, and differs from the app's value. `1500`, `1500.00` and `1 500,00`
  typed as text are equal in P.B (TOTAL DOC is read as in an Amadeus upload); one cent is a
  difference. **An empty cell is not a zero**: where the app
  writes `0.00` (the second ticket of a multi-ticket sale carries no P.B, decision 21) and she
  leaves the cell empty, that is shown, so the two can agree on a rule.
* **Text** after the same cleaning as the export: runs of white space count as one space, upper and
  lower case are equal, spaces around the `/` of a name do not count, and the `'` the export puts
  before a text that could be read as a formula is ignored.
* **PAX NAME** includes the `(CHD)` / `(INF)` mark: a child against an adult is a difference, but
  the padding of the mark (item 3 reproduces it exactly) does not matter.
* Her cells with a formula are read as the value Excel stored with them, never evaluated; a formula
  with no stored value reads as empty, which shows as a difference.
* Her header block must look like the app's: a `Report date range` and a `Current date` above the
  headers, as in every Amadeus export and APRES. If her sheet is for another date than the day
  being compared, the page warns but still compares.

* An AG, CLT or AP cell longer than 200 characters is cut when read; it is always shown as a
  difference and marked "… (tronqué)", since two cut texts cannot be proven equal.

Not compared: the totals row, the colours and comments, the Amadeus columns the export does not
write (TAX, COMM, FP, CONFIRMED). A cancelled (CANX) line left in her sheet shows as "only in hers",
because the app's APRES never has any.

## 3. How the file is handled

Same protection as the Amadeus upload: login and permission first (staff get a 403 and an audit
event, nothing is read); Origin check; CSRF token (header or form field, checked before the file is
looked at); the day must be closed, in the user's agency and have a stored export (otherwise 404);
a rate limit of 20 comparisons an hour for each user (a busy sandbox or a server fault gives the hit
back; a wrong form token does not, because the body is already read). Only then the body is read, in memory. The bytes go through the same size, empty and zip
checks as an upload (10 MB), then through the **sandboxed parser** (a child process with no
secrets, time and memory limits, zip-bomb checks, strict validation of what it sends back). A busy
sandbox is a 503; an unreadable file is a French message. Everything shown is escaped by the
templates.

## 4. The exit rule (from `14-go-live.md` §4)

Each day she uploads her APRES. Each difference is explained (her mistake, an app bug, or a rule to
agree) and fixed the same week. **Exit: 10 working days in a row with no unexplained difference.**
Then the daily Excel stops. The report back to the owner each day is the four numbers of the page
(common, identical, differing cells, rows on one side only) and the explanation of each difference.

## 5. Where it lives

`app/apres_compare.py` (the pure comparison), `app/web/routes/apres_compare.py` (the routes),
`app/web/templates/days/compare.html`, `app/parsing/amadeus.py` (`parse_apres`: the same parser
with the four hand-typed columns read), `tests/test_apres_compare.py`,
`tests/test_apres_compare_pages.py`, `tests/test_apres_parse.py`.
