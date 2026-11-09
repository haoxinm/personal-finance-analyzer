# Spend Analyzer

Spend Analyzer turns your credit-card, debit-card, and bank-statement PDFs into a searchable,
categorized spending history — on your own machine (macOS, Windows, or Linux). There is no account,
no hosted service, and no server to trust with your statements. An LLM is optional, and can be a
model running locally (Ollama, LM Studio) so nothing leaves your computer at all.

```console
$ pipx install spend-analyzer
$ spend-analyzer migrate
$ spend-analyzer import ~/Downloads/january-statement.pdf
$ spend-analyzer classify
$ spend-analyzer report --from 2026-01-01 --to 2026-01-31
$ spend-analyzer serve       # opens the local web UI
```

## Privacy model

This is the reason to run this instead of a hosted budgeting app, so it comes first.

- **Everything runs locally.** PDF extraction, parsing, deduplication, the rules engine, the
  database, and the web UI all run as one program on your machine. There is no telemetry, no
  analytics, and no update check.
- **The LLM is off by default** (`[llm] enabled = false`). The local rules cascade classifies most
  transactions on its own; anything it can't classify lands in a Review queue for you, not an API
  call. With the LLM off, nothing is ever sent anywhere.
- **If you turn an LLM on, exactly two things can be sent to the endpoint you configure** — each
  through its own audited code seam:
  1. **Classification** sends only the normalized, redacted merchant description of a transaction
     the local rules couldn't classify (e.g. `AMZN MKTP US*1A2B3` → `amazon`), as plain CSV text.
     Never dates, amounts, account numbers, issuer/bank names, or your issuer's category codes
     (`classify/llm/egress.py`).
  2. **Statement parsing with the LLM is opt-in, per statement.** Only when you choose the LLM
     extractor for a statement (Spending › Import, or `import --parser-id llm`) is the text of the
     pages *you selected* sent. Before it leaves, your personal-info entries are removed,
     email addresses are replaced, and any remaining card/account numbers (8+ digits) are masked
     to the last 4. Amounts and dates are kept, since they are the content being parsed
     (`llm/statement_parser.py`). LLM imports are never auto-confirmed.
- **Personal info you list is removed first, on both paths.** In Settings → Privacy, add your name,
  family members' names, phone numbers, address and account/SSN-like numbers (stored as
  `[privacy] pii_terms`, plus per-user aliases). Names match ignoring case, spacing and
  "Last, First" order (with an optional middle initial) and never inside a longer word. Phone and
  account numbers match in any format -- `(312) 555-0100` = `312.555.0100` = `3125550100` =
  `+1 312-555-0100` -- but never inside a longer run of digits. Entries are applied before the
  generic number masking, so nothing (not even the last 4 digits) of a listed number is sent. The
  Settings "Try it" box shows what a pasted sample looks like after redaction (local only), and
  the Import preview reports how many matches were removed (`core/pii.py`).
- **Local endpoint = nothing leaves your machine.** Local vs. remote is derived from the address:
  `localhost`, `127.0.0.1` and `::1` are local. The redaction and previews apply either way.
- **Preview before you send.** Settings shows the exact CSV a classification call would send
  (`GET /api/classify/preview`), and Spending › Import shows the exact redacted page text a
  statement parse would send (`GET /api/statements/{id}/llm-preview`) — the same bytes the real
  call uses, built by the same code path.
- **One HTTP client, two callers.** Only `llm/client.py` imports an HTTP client, and only the two
  seams above may import it — checked by `tests/test_architecture_guards.py` on every change.
  Statement ingestion (`ingest/`) can never make a network call.
- **Tax forms stay local unless you opt in.** W-2, 1099 and 1040 PDFs are read by deterministic
  local parsers, and the tax code imports neither an LLM nor an HTTP client
  (`tests/test_architecture_guards.py`). The only exception is a separate, off-by-default switch,
  `[ingest] tax_llm_fallback = true` (Settings -> LLM, or `llm set --tax-llm-fallback`), which
  works only while an LLM is configured. With it on, just the pages the parsers could not read are
  sent, redacted: SSNs, EINs and account numbers are masked to their last 4 digits, recipient /
  employee name and address rows are removed (best effort), and your personal-info entries from
  **Settings -> Privacy** (the same list and matching rules used for statements) are removed.
  Values read that way are tagged and marked "review", and `tax import --no-llm` turns it off for
  a run. Only the **last four digits** of a payer or account identifier are stored; full SSNs, ITINs
  and EINs are never written to the database, the logs or the extract cache, and your own and your
  spouse's numbers are never read. The PDF is deleted after import unless you set
  `[privacy] store_tax_pdf_copies = true`. See [docs/TAX.md](docs/TAX.md).
- **Account balances stay local.** The balances you enter for net worth (checking, savings,
  investment, credit card) live only in your local database. They are never exported, never part
  of any LLM payload, and the net-worth code (including the forecast, its manual past-year net worth
  and saved presets) imports neither an LLM nor an HTTP client.
- **Secrets** (an LLM API key) live only in your OS credential store (macOS Keychain, Windows
  Credential Manager, Linux Secret Service), keyed per host — never in the database, the config
  file, or a log line. On a headless machine you can supply it as `SPEND_ANALYZER_LLM_API_KEY`.
- **The local web server** binds `127.0.0.1` only, checks every request's `Host` header, and
  requires a random token minted fresh on every launch — so nothing else on your machine's network,
  let alone the internet, can reach it, and a malicious web page open in your browser can't either.

See [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) for the full, code-verified security
checklist, and [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

## Using an LLM (local or remote)

The LLM is optional. It is used to classify leftover transactions and, if you choose, to parse
statements whose layout the built-in templates don't handle. You can configure it **before or
after `spend-analyzer serve` has started — no restart is needed.** `config.toml` is re-read on
every request, so a running server picks up changes made from the Settings screen, from
`spend-analyzer llm ...`, or by editing the file by hand.

Pick whichever of these suits you; they all write the same settings.

**1. The CLI** (`--preset` fills in the URL and API format; add `--model`; the model is optional for
single-model local servers such as llama.cpp or vLLM, but OpenAI and Anthropic require one):

```console
$ spend-analyzer llm set --preset ollama --model llama3.1 --enable            # local, no key
$ spend-analyzer llm set --preset lm-studio --model qwen2.5-7b-instruct --enable
$ spend-analyzer llm set --preset openai --model gpt-4.1-mini --enable        # remote
$ spend-analyzer llm set-key                                                  # prompts, hidden
$ spend-analyzer llm set --preset anthropic --model claude-sonnet-4-5 --enable
$ spend-analyzer llm set-key
$ spend-analyzer llm test        # one real round trip; `llm show` prints the current settings
```

Presets: `ollama`, `lm-studio`, `openai`, `openai-responses`, `anthropic`, `openrouter`. Use
`spend-analyzer llm disable` to turn it off again.

**2. `config.toml`** (see [INSTALL.md](docs/INSTALL.md#data-location) for where it lives):

```toml
[llm]
enabled = true
base_url = "http://localhost:11434"   # or https://api.openai.com, https://api.anthropic.com, ...
api_format = "openai_chat"            # openai_chat | openai_responses | anthropic_messages
path = ""                             # empty = the format's default path
model = "llama3.1"                     # optional for single-model local servers; OpenAI/Anthropic need one
# temperature = 0.0                   # omit to not send one (some newer models reject it)
# max_tokens = 4096
# reasoning_effort = "low"            # minimal | low | medium | high | xhigh | max
# extra_body = { top_p = 0.9 }        # merged into every request body
# extra_headers = { "HTTP-Referer" = "https://example.com" }

[ingest]
default_extractor = "auto"            # "llm" makes the LLM the default for new imports
```

**3. Environment variables** override the file for one run (handy in CI or containers):
`SPEND_ANALYZER_LLM__ENABLED=true`, `SPEND_ANALYZER_LLM__BASE_URL=...`,
`SPEND_ANALYZER_LLM__MODEL=...` (pattern `SPEND_ANALYZER_LLM__<FIELD>`), and the API key via
`SPEND_ANALYZER_LLM_API_KEY`.

**4. The Settings screen** in the web UI has the same fields. "Test connection" tests the values
currently in the form, saved or not.

**API format, path, and extra arguments.** The app talks plain HTTP to any endpoint that speaks one
of three formats: `openai_chat` (`/v1/chat/completions` — OpenAI, Ollama, LM Studio, vLLM,
llama.cpp, OpenRouter, ...), `openai_responses` (`/v1/responses`), or `anthropic_messages`
(`/v1/messages`). Set `path` if your server mounts the API elsewhere (a trailing `/v1` on the base
URL is handled). `temperature`, `max_tokens` and `reasoning_effort` are sent only if set; anything
else the endpoint supports goes in `extra_body` (e.g. `--extra-body '{"top_p": 0.9}'`), and custom
headers in `extra_headers` (`--extra-header KEY=VALUE`). `structured_output` asks for
schema-constrained JSON and falls back automatically if the server rejects it. Older configs using
`mode`/`provider`/`api_base` are migrated automatically. Full option list:
[docs/CLI.md](docs/CLI.md#llm).

## Quick start

```console
$ pipx install spend-analyzer          # or: pip install spend-analyzer (Windows, macOS, Linux)
$ spend-analyzer migrate               # create/upgrade the local database, sync the taxonomy
$ spend-analyzer import statement.pdf  # extract + propose an issuer/parser; confirms automatically
                                        # when confident, otherwise prints the confirm command
$ spend-analyzer import odd.pdf --pages 1-3,5 --parser-id llm   # optional: parse chosen pages
                                        # with your configured LLM (see "Using an LLM")
$ spend-analyzer classify               # run the rules-first cascade over new transactions
$ spend-analyzer report --from 2026-01-01 --to 2026-01-31
$ spend-analyzer serve                 # local web UI at http://127.0.0.1:<port>/?token=<token>
```

See [docs/INSTALL.md](docs/INSTALL.md) for requirements, upgrading, uninstalling, and
troubleshooting, and [docs/CLI.md](docs/CLI.md) for the full command reference.

Running from a source checkout? After `git pull`, rebuild the web UI with
`cd frontend && npm ci && npm run build` (an editable install never does; `serve` warns when the
build is missing or stale).

## What it does

- **Imports native digital PDF statements** (not scanned/OCR) from credit cards, debit cards, and
  bank accounts, extracting one row per transaction with a signed amount in cents, a posted date,
  and a normalized merchant description.
- **Lets you choose what is parsed.** Pick which pages of a statement to keep and parse (the Import
  screen shows page thumbnails; the CLI has `--pages 1-3,5`), and for layouts the templates don't
  handle, parse them with an LLM — local or remote — after previewing the exact redacted text that
  would be sent. Opt-in per statement; never auto-confirmed.
- **Classifies every transaction** into a fixed two-level category/subcategory taxonomy via a
  rules-first cascade: kind detection → your own rules → ~120 built-in high-precision rules → the
  issuer's own category column, where the statement prints one → a cache of past classifications →
  an LLM, only for what nothing local could resolve → an `others/uncategorized` fallback that
  still needs your review.
- **Keeps two kinds of correction apart.** *Editing a transaction* (the edit popover, a bulk
  update, or **Accept** on the Review screen) is a one-time edit: it changes only that transaction,
  it always wins over every rule, the cache, the LLM and any re-classify, and it teaches the app
  nothing about the merchant or about future transactions. *"Always categorize “merchant” this
  way"* (Review's second button, or "Also create a rule" in the edit popover) is the explicit
  merchant-level action: it saves a rule for that merchant's future transactions. A rule reaches
  transactions you already have only when you press "Apply to existing" on the Rules screen
  (which first shows how many rows it will change, and whether that spans several users or changes
  kinds, then offers an Undo), and it never changes one you edited by hand. Undoing a Review accept
  or a rule application never reverts something you edited afterwards. Precedence: one-time edits > your rules > built-in
  rules > issuer category and merchant cache > LLM. The Review screen can re-run the cascade over
  everything uncategorized.
- **Reconciles every import** against the statement's own printed totals (section subtotals,
  running balance, or the opening/closing balance equation, whichever the layout provides), and
  banners a warning rather than silently accepting numbers that don't add up.
- **Never double-counts a card payment.** A payment to a credit card from a tracked checking
  account is classified as a transfer on both sides, not as spend — the purchase was already
  counted on the card statement.
- **Serves a local web UI and a CLI** for reviewing transactions, correcting categories, mapping an
  unrecognized layout by hand, and charting spending by category over time — plus a `report`
  command for the same numbers in a terminal.
- **Analyzes income and taxes from your tax forms.** Import W-2, 1099-INT/DIV/B/NEC/MISC,
  consolidated 1099 and Form 1040 PDFs (`spend-analyzer tax import`, or **Income & Tax › Import** in the web UI)
  to see income by category and payer, withholding, refund or amount owed, effective and all-in tax
  rates, and a year-over-year comparison. W-2 and 1099 forms are the source of income, the 1040 the
  source of tax owed, so nothing is double counted; the ordinary-versus-preferential split of
  federal tax is a labelled estimate. See [docs/TAX.md](docs/TAX.md).
- **Tracks net worth.** On the **Net worth** page, enter monthly balances for checking, savings, investment
  and credit-card accounts (several accounts per category). Net worth is assets minus credit-card
  balances, shown with a breakdown table, a pie of where your assets are held, and trend lines per
  category and per account. A month with no entry carries the account's last balance forward.
- **Forecasts net worth.** **Net worth › Forecast** projects net worth to a year you choose from your
  balance entries, tax forms and spending, with adjustable income growth, tax rate, spending growth,
  investment return, inflation, retirement and one-off events. Start from a built-in preset
  (Conservative, Moderate, Aggressive) or save your own, enter net worth by hand for past years your
  entries don't cover, and see the result nominal or in starting-year dollars. It is a deterministic
  illustration, not advice. See [docs/FORECAST.md](docs/FORECAST.md).
- **Gives each person their own view.** Every page except Settings has a user bar with user
  chips ("All users" plus one per user, a dropdown beyond six users). Your choice is kept in the
  `?user=` URL parameter, so a view can be bookmarked, and remembered in your browser between
  screens.
- **Deduplicates on import.** Re-importing the same PDF is a no-op; importing an overlapping
  statement period never creates duplicate rows.

## Supported statement layouts

Four built-in layouts, identified by structure (never by bank name — which bank issued a statement
is separate, user-editable data):

| Layout | Shape | Typical source |
|---|---|---|
| **A** — `layout_a_credit` | Sectioned, dual-date (transaction date + posting date), per-section totals | Credit card |
| **B** — `layout_b_credit` | Two tables — an activity table plus a separate, non-transactional interest-charged table | Credit card |
| **C** — `layout_c_credit` | Repeating column-header rows double as section delimiters, no explicit section headings | Credit card |
| **D** — `layout_d_bank` | Separate debit/credit columns, a running balance on every row | Bank statement; also most **debit-card** activity |

**Debit cards have no dedicated layout.** Debit-card activity is either a bank statement (Layout D,
or the generic parser's debit/credit-column mode) or a card-style statement with signed amounts
(Layouts A–C, or the generic parser's signed-amount mode) — see D11 in the design record.

**Anything else** goes through the **generic fallback parser**, and if that isn't confident either,
an **unknown-layout funnel**: a correctable preview table, then a point-and-click column mapper
that produces a reusable, versioned **layout spec** (plain YAML, no code, no LLM) saved locally and
tried automatically on the account's next statement. Alternatively, choose the **LLM extractor** for
that statement (see [Using an LLM](#using-an-llm-local-or-remote)). See [docs/LAYOUTS.md](docs/LAYOUTS.md) for
the full per-layout specification and how to write one by hand.

## Supported tax forms

Tax forms are a separate import path (`spend-analyzer tax import`) with their own parsers. Native
digital, software-generated PDFs only, USD only.

| Form | Role | What is read |
|---|---|---|
| **W-2** | source | Wages (box 1); federal, Social Security, Medicare, state and local tax withheld; several employers or copies in one file |
| **1099-INT** | source | Interest (boxes 1, 3), tax-exempt interest as a memo item, federal and state withholding |
| **1099-DIV** | source | Ordinary dividends, qualified dividends (memo), capital-gain distributions, exempt-interest dividends (memo), withholding |
| **1099-B** | source | Net short-term and long-term gain or loss, and withholding (proceeds and cost basis are not stored) |
| **1099-NEC / 1099-MISC** | source | Nonemployee compensation; rents, royalties and other income; withholding |
| **Consolidated 1099** | source | A broker's composite statement: its 1099-INT, -DIV, -B and -MISC sections, per account |
| **Form 1040** (2022-2025) | return | Income lines, Schedules D, 1, 2 and C, total tax, payments, refund or amount owed, and an estimated ordinary/preferential tax split |

Not supported: 1040-SR, 1040-NR and 1040-X, state returns, scans and photos. The exact boxes and
how they map to categories are in [docs/TAX.md](docs/TAX.md).

## CLI reference

```
spend-analyzer migrate              create/upgrade the DB, sync the taxonomy and builtin rules
spend-analyzer serve                start the local web server (binds 127.0.0.1, per-launch token)
spend-analyzer import <pdf>...      import statement PDFs (--pages, --parser-id llm)
spend-analyzer classify             run the classification cascade over unclassified transactions
spend-analyzer report               print monthly category totals for a date range
spend-analyzer export               export transactions to CSV/JSON (never includes account masks)
spend-analyzer llm                  configure the LLM (show / set / enable / disable / set-key / clear-key / test)
spend-analyzer doctor               diagnose the local install: Python, data dir, DB, keychain, LLM
spend-analyzer eval                 run the classifier evaluation harness against a labelled CSV
spend-analyzer users                manage local users (list / add / set-default)
spend-analyzer issuers              manage issuers (list / add), so later imports auto-confirm
spend-analyzer tax                  income & tax analysis from tax forms (import / list / report / delete)
```

Run `spend-analyzer --help` or `spend-analyzer <command> --help` for full options; see
[docs/CLI.md](docs/CLI.md) for a worked example of each command.

## Documentation

- [docs/INSTALL.md](docs/INSTALL.md) — installing (Windows/macOS/Linux), upgrading, uninstalling, troubleshooting
- [docs/CLI.md](docs/CLI.md) — full CLI command reference
- [docs/LAYOUTS.md](docs/LAYOUTS.md) — the four built-in layout specifications and how to add one
- [docs/TAX.md](docs/TAX.md) — income and tax analysis: supported forms, boxes, rates, the estimate and its limits
- [docs/FORECAST.md](docs/FORECAST.md) — net worth forecast: sources, parameters, formula, presets and limits
- [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) — the security checklist, re-verified against
  the shipped code
- [SECURITY.md](SECURITY.md) — threat model and vulnerability reporting
- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup, checks, and how to add a statement or tax-form parser

## License

MIT — see [LICENSE](LICENSE).
