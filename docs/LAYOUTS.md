# Statement layouts

Personal Finance Analyzer identifies a statement's **layout** — its column and section structure — and keeps
that separate from **who issued it**. No bank name is hard-coded anywhere in the code, fixtures,
or tests; issuers are your own local data (`pfa issuers`), matched by substring against
each issuer's `match_terms` on the statement's first page.

Every layout, and the generic fallback, shares a few rules (owned by `ingest/layout.py`):

- **Money** is parsed only through one shared function, which uses `Decimal` (never `float`) and
  understands `1,234.56`, `-12.34`, `(12.34)`, `12.34 CR`, `12.34-`, and a leading `$`.
- **Amounts are signed integer cents; positive = money leaving you.** Every parser has a test
  asserting the sign of a purchase, a refund, and a payment explicitly — not just a total, which
  can hide a matched pair of sign flips.
- **Reconciliation.** Every parser reads the statement's own totals (per-section totals, a running
  balance, or the opening/closing balance equation — whichever the layout prints) and the import
  banners a warning rather than silently accepting numbers that don't add up. This is also how
  **layout drift** is caught: a `detect()` score that quietly decays, or a shape assertion (an
  expected column or section) that stops matching, both flag the statement for a spot-check
  instead of trusting a possibly-broken parse.
- **Account masks** are the last 4 digits only, and are local-only data — never sent to an LLM.

> **Layouts the templates don't handle.** The recommended path for a statement none of these
> layouts (or a saved layout spec) fits is the **LLM extractor**: choose it for that statement in
> Spending › Import, or `pfa import x.pdf --parser-id llm --pages 1-3`. It is opt-in per
> statement, sends only the pages you select (redacted: card/account numbers masked, emails and PII
> terms removed) to your configured endpoint — a local model keeps everything on your machine — and
> is never auto-confirmed. See the [README](../README.md#using-an-llm-local-or-remote). The
> hand-built layout specs below remain the no-LLM, fully deterministic option.

## The four built-in layouts

### Layout A — `layout_a_credit` (sectioned, dual-date)

Sections `Payments and Other Credits` / `Purchases and Adjustments` / `Fees` / `Interest Charged`,
each closed by a `TOTAL <SECTION> FOR THIS PERIOD` line carrying that section's own total — the
strongest reconciliation signal of the four layouts, because it localizes a dropped row to one
section. Both a transaction date and a posting date are printed.

### Layout B — `layout_b_credit` (dual-table, capitalized)

Two **separate** tables: `ACCOUNT ACTIVITY` (the actual transactions) and `INTEREST CHARGED` (APR
and balance-subject-to-interest rows — not transactions at all; a parser that treats it as one
would store an APR as a dollar amount). Only `ACCOUNT ACTIVITY` is parsed for transactions; the
label `PURCHASES` appears in both tables, so which table you're in — never the label alone —
decides how a row is read. One date column only; the year is inferred from the statement period.

### Layout C — `layout_c_credit` (repeating headers as delimiters)

No section headings — a repeated column-header row is the section delimiter, and its second
column names the section (`PAYMENTS AND CREDITS` or `PURCHASES`). Watch for `TRANS. DATE` vs.
`TRANS DATE` (punctuation varies) and re-derive column positions per section, since the two
sections don't share a column count. The `MERCHANT CATEGORY` column, where present, is captured
into `issuer_category` — a free, deterministic, local-only signal the classification cascade
consults before ever asking an LLM.

### Layout D — `layout_d_bank` (bank statement, debit/credit columns)

The reference **bank statement** shape, and also the shape of most **debit-card** activity — there
is no separate debit-card layout (see the README's [supported layouts](../README.md#supported-statement-layouts)
section). Separate withdrawal/debit and deposit/credit columns decide the sign, not a printed `-`
or `CR`; a row with values in both columns is an error, not a guess. A running balance printed on
(most) rows is the strongest reconciliation signal available: `balance[i] = balance[i-1] -
amount[i]` pinpoints the exact row that was dropped or mis-signed. A payment from this account to
a tracked credit card is classified as a transfer, not spend — the purchase was already counted on
the card statement.

## Anything else: the generic parser and layout specs

A statement that doesn't confidently match one of the four above runs through the **generic
fallback parser** (column-position heuristics, no layout-specific knowledge). If that also isn't
confident, nothing is silently guessed:

1. Its best-effort result shows as a **correctable preview table** in the web UI before you commit
   the import.
2. You map columns by clicking (date / description / amount / category), pick a date format, and
   mark section boundaries. That produces a **layout spec** — plain YAML, no code — saved locally
   and tried automatically on the account's next statement.
3. A spec obtained any other way (shared by another user, written by hand) can be pasted in and
   goes through the same validation as a hand-mapped one: checked against a JSON Schema, with
   every regex length-capped and screened for catastrophic backtracking before it is ever run,
   because a layout spec is untrusted input even once approved.
4. "Contribute this layout" exports the spec plus a **synthetic fixture generated from it** — never
   the real statement — for upstreaming into the built-in set.

## Adding a new built-in layout

See [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-parser) for the full walkthrough. In short: one
parser file under `src/personal_finance_analyzer/ingest/parsers/`, one `LayoutBuilder` describing its column
bands, one golden fixture set, and one row added to the cross-`detect()` matrix that checks every
parser scores every other layout's fixtures below its own — so a new layout can't accidentally
start claiming another layout's statements.
