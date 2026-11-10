# Personal Finance Analyzer

Personal Finance Analyzer is a local-first app for understanding your own money. It turns your
credit-card, debit-card and bank-statement PDFs into a searchable, categorized spending history;
reads your W-2, 1099 and 1040 forms into an income and tax picture with a per-person federal tax
estimate; tracks your net worth from the balances you enter and forecasts it forward; and can back
everything up into one file to move to another machine. It runs entirely on your own computer
(macOS, Windows or Linux): there is no account, no hosted service, no telemetry, and no server to
trust with your documents. An LLM is optional, and can be a model running locally (Ollama, LM
Studio) so that nothing leaves your computer at all.

```console
$ pipx install personal-finance-analyzer
$ pfa migrate
$ pfa import ~/Downloads/january-statement.pdf
$ pfa classify
$ pfa report --from 2026-01-01 --to 2026-01-31
$ pfa serve       # opens the local web UI
```

> **Upgrading from Spend Analyzer?** Your data, settings and API key carry over automatically; see
> [Upgrading from Spend Analyzer](#upgrading-from-spend-analyzer).

## What's in it

The web UI is organized in four sections (plus a user bar, below); everything in it has a CLI
counterpart except the layout mapper and the forecast.

### Spending

- **Import native digital PDF statements** (not scanned/OCR) from credit cards, debit cards and bank
  accounts: one row per transaction with a signed amount in cents, a posted date and a normalized
  merchant description. Pick which pages to keep and parse; for layouts the built-in templates
  don't handle, map the columns by hand (a reusable layout spec) or opt in to the LLM extractor
  for that statement. Re-importing a PDF or an overlapping period never creates duplicates.
- **Classify every transaction** into a fixed two-level category taxonomy via a rules-first
  cascade: kind detection, your own rules, ~120 built-in rules, the issuer's own category column,
  a cache of past classifications, then an LLM only for what nothing local could resolve; the rest
  lands in a **Review** queue.
- **Keep two kinds of correction apart.** *Editing a transaction* (the edit popover, a bulk update,
  or **Accept** on Review) changes only that transaction and always wins over every rule, the cache,
  the LLM and any re-classify. *"Always categorize this merchant this way"* saves a rule for the
  merchant's future transactions; it reaches existing ones only when you press "Apply to existing"
  on the Rules screen, and never changes one you edited by hand. Precedence: one-time edits > your
  rules > built-in rules > issuer category and merchant cache > LLM.
- **Reconcile every import** against the statement's own printed totals and warn, rather than
  silently accept numbers that don't add up; **never double-count a card payment** (a payment from
  a tracked checking account is a transfer on both sides, not spend).
- **Chart and report** spending by category over time, in the web UI or with `pfa report`.

### Income & tax

- **Import W-2, 1099-INT/DIV/B/NEC/MISC, consolidated 1099 and Form 1040 PDFs** (`pfa tax import`,
  or **Income & tax › Import**) to see income by category and payer, withholding, refund or amount
  owed, effective and all-in tax rates, and a year-over-year comparison. W-2 and 1099 forms are the
  source of income and the 1040 the source of tax owed, so nothing is counted twice.
- **Estimate** computes a per-person federal tax estimate ("Refund $X" / "Owe $X") from the forms
  plus a few inputs you enter (filing status, deduction, credits, payments), with your filed 1040 as a
  cross-check. It is a labelled estimate, not tax advice. See [docs/TAX.md](docs/TAX.md).

### Net worth

- **Track net worth** by entering monthly balances for checking, savings, investment and
  credit-card accounts (several per category): a breakdown table, a pie of where your assets are
  held, and trend lines per category and per account. A month with no entry carries the last
  balance forward.
- **Forecast** net worth to a year you choose from your balances, tax forms and spending, with
  adjustable income growth, tax rate, spending growth, investment return, inflation, retirement and
  one-off events; built-in and saved presets; nominal or starting-year dollars. A deterministic
  illustration, not advice. See [docs/FORECAST.md](docs/FORECAST.md).

### Settings

- **LLM** (off by default; local or remote), **Privacy** (names and numbers to strip before any LLM
  call), **Tax parameters** (the opt-in yearly check), and **Backup & restore**, which seals your
  whole data directory into one passphrase-encrypted `.pfabackup` file for moving machines (see
  [Moving to another machine](#moving-to-another-machine)).

### Everyone gets their own view

Every page except Settings has a user bar ("All users" plus one chip per user, a dropdown beyond
six). The choice is kept in the `?user=` URL parameter, so a view can be bookmarked, and remembered
in your browser between screens.

## Quick start

```console
$ pipx install personal-finance-analyzer   # or: pip install personal-finance-analyzer
$ pfa migrate               # create/upgrade the local database, sync the taxonomy
$ pfa import statement.pdf  # extract + propose an issuer/parser; confirms automatically
                            # when confident, otherwise prints the confirm command
$ pfa import odd.pdf --pages 1-3,5 --parser-id llm   # optional: parse chosen pages
                            # with your configured LLM (see "Using an LLM")
$ pfa classify              # run the rules-first cascade over new transactions
$ pfa report --from 2026-01-01 --to 2026-01-31
$ pfa tax import w2-2025.pdf  # optional: income & tax from tax forms
$ pfa serve                 # local web UI at http://127.0.0.1:<port>/?token=<token>
```

`pfa` is the command; `personal-finance-analyzer` is the same program under its long name. See
[docs/INSTALL.md](docs/INSTALL.md) for requirements, upgrading, uninstalling and troubleshooting, and
[docs/CLI.md](docs/CLI.md) for the full command reference.

Running from a source checkout? After `git pull`, rebuild the web UI with
`cd frontend && npm ci && npm run build` (an editable install never does; `serve` warns when the
build is missing or stale).

## Upgrading from Spend Analyzer

This project was called *Spend Analyzer* (package and command `spend-analyzer`). Nothing needs to be
exported or moved by hand. Replace the installed package, since the old and new ones both ship a
`spend-analyzer` command:

```console
$ pipx uninstall spend-analyzer             # or: pip uninstall spend-analyzer
$ pipx install personal-finance-analyzer    # or: pip install personal-finance-analyzer
$ pfa migrate
```

What carries over automatically the first time you run a command that opens the database
(`pfa migrate`, `pfa serve`, `pfa import`, ...). Read-only commands such as `pfa llm show` or
`pfa backup inspect` never move anything; until the first of those commands runs they simply keep
reading the old directory:

| What | Before | Now |
|---|---|---|
| Data directory | `~/Library/Application Support/SpendAnalyzer` (macOS), `%LOCALAPPDATA%\SpendAnalyzer` (Windows), `~/.local/share/spend-analyzer` (Linux) | `.../PersonalFinanceAnalyzer`, `%LOCALAPPDATA%\PersonalFinanceAnalyzer`, `~/.local/share/personal-finance-analyzer`: the old directory is renamed in place (never copied). If that fails, or an old `spend-analyzer serve` still has the database open (stop it and restart), the old one keeps being used; if both exist the new one wins with a warning |
| Database file | `spend.db` | `finance.db`, renamed inside whichever data directory you use |
| Kept PDF copies | recorded by absolute path | the recorded paths are rewritten to the new directory, and copies are looked up under the current directory if a recorded path is stale |
| LLM API key (OS credential store) | service `spend-analyzer` | service `personal-finance-analyzer`, copied and the old entry deleted the first time the key is really used (an LLM call or a save), never by `pfa doctor`, `pfa llm show` or the Settings screen, which only read it |
| Environment variables | `SPEND_ANALYZER_HOME`, `SPEND_ANALYZER_LLM_API_KEY`, `SPEND_ANALYZER_<SECTION>__<FIELD>` | `PFA_HOME`, `PFA_LLM_API_KEY`, `PFA_<SECTION>__<FIELD>`; the old names still work, the new ones win |
| Command | `spend-analyzer ...` | `pfa ...`; `spend-analyzer` remains as a deprecated alias that prints a one-line stderr notice |
| Backups | `*.spendbackup` | `*.pfabackup`; old files can still be inspected and restored |
| Web UI | remembered user selection | carried over in the browser |

A directory you point at with `SPEND_ANALYZER_HOME` or `PFA_HOME` is never moved. Details and
troubleshooting: [docs/INSTALL.md](docs/INSTALL.md#upgrading-from-spend-analyzer).

## Privacy model

This is the reason to run this instead of a hosted budgeting app.

- **Everything runs locally.** PDF extraction, parsing, deduplication, the rules engine, the
  database, and the web UI all run as one program on your machine. There is no telemetry and no
  analytics. By default there is no update check either: the one exception is the opt-in tax
  parameter check below.
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
  Statement ingestion (`ingest/`) can never make a network call. The single other module that may
  import an HTTP client is the opt-in tax parameter check described next, and the same test pins
  that too.
- **Opt-in, once-a-year tax parameter check (off by default).** The federal tax estimate needs the
  IRS's yearly figures (brackets, standard deduction, limits), which ship inside the app for 2022 -
  2026. If you set `[tax] auto_update_params = true` (**Settings › Tax parameters**, or the file), the first tax
  estimate or summary you open in a calendar year makes **one plain HTTPS `GET`** of a single
  public file, `federal_params.yaml`, from this project's GitHub repository
  (`raw.githubusercontent.com/haoxinm/personal-finance-analyzer/main/src/personal_finance_analyzer/core/tax_data/federal_params.yaml`)
  and keeps a validated copy in your data directory, but only to ADD a tax year the app does not
  ship yet (each checked against the previous year); the figures of years the app ships are never
  replaced. The date is stored so it is not asked again that year, whatever the outcome. The request
  has no query string, cookies, credentials or proxy
  settings and carries none of your data (not your tax figures, not the installed version); GitHub
  sees your IP address like for any download. It never blocks the page and a network error is
  ignored. `pfa tax params update` (or the **Check now** button in Settings › Tax parameters) does the same on
  request, and you can instead supply your own file with `tax params import`. With the setting off,
  nothing is ever requested. See [docs/TAX.md](docs/TAX.md#where-the-parameters-come-from-and-the-opt-in-update).
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
- **Secrets** (an LLM API key) live in your OS credential store (macOS Keychain, Windows
  Credential Manager, Linux Secret Service), keyed per host — never in the database, the config
  file, or a log line. The key is looked up in this order: the `PFA_LLM_API_KEY` environment
  variable, then the OS keychain, then (only if you opt in) a plaintext key file. On a machine with
  no keychain at all (headless Linux, WSL, an SSH session, a container) saving a key fails with
  setup instructions rather than falling back silently; either export `PFA_LLM_API_KEY`, set up a
  Secret Service (see [docs/INSTALL.md](docs/INSTALL.md#linux-keychain-setup)), or switch on
  `[secrets] allow_file_store = true` (Settings -> LLM provider shows the toggle) to keep keys in
  `<data dir>/secrets/api_keys.toml`. That file is **plaintext at rest**, protected only by its
  `0600` permissions, used only while no keychain is available, and never part of a backup.
  Settings and `pfa doctor` both say which backend is in use. `pfa doctor` only *reads* the
  keychain (and creates neither the data directory nor the database on a fresh machine);
  `pfa doctor --write-test` is the explicit write round trip. A setting controlled by a `PFA_*`
  environment variable is never written to `config.toml` by a save, and the Settings toggle for the
  plaintext key file is disabled (with the variable's name) while one controls it.
- **Backups are encrypted by default and never carry secrets.** `backup create` (and Settings ->
  Backup & restore) seals the archive with a passphrase (scrypt + AES-256-GCM); skipping that is an
  explicit `--no-encrypt`. The OS-keychain API key is never read into one. The archive is built
  locally and goes nowhere on its own.
- **The local web server** binds `127.0.0.1` only, checks every request's `Host` header, and
  requires a random token minted fresh on every launch — so nothing else on your machine's network,
  let alone the internet, can reach it, and a malicious web page open in your browser can't either.

See [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) for the full, code-verified security
checklist, and [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

## Moving to another machine

Switching computers should not mean re-importing every statement and tax form. Back up on the old
one, copy the single file, restore on the new one:

```console
$ pfa backup create ~/Desktop/          # old machine; asks for a passphrase
$ pipx install personal-finance-analyzer                      # new machine
$ pfa backup restore pfa-2026-10-30.pfabackup
$ pfa llm set-key                       # only if you use a remote LLM that needs a key
$ pfa serve
```

The same two actions are in the web UI under **Settings -> Backup & restore**. A backup holds your
database, the kept statement PDFs, layout specs and settings, and is **encrypted with a passphrase
by default** because it contains everything the app knows about your finances (there is no way to
recover a lost passphrase). It never contains your LLM API key: that stays in the OS credential
store of the machine that made it, so enter it again on the new one. Restore checks the whole file
before touching anything, migrates a backup made by an older version, and refuses one from a newer
version (including `.spendbackup` files made before the rename). If the new machine already has data, `restore` needs `--replace`, which moves the existing
data directory aside to `<name>.<timestamp>.before-restore` instead of deleting it; the plaintext key file and the `[secrets]` opt-in of the machine being
restored into are kept (they are not part of a backup). See [docs/CLI.md](docs/CLI.md#backup).

## Using an LLM (local or remote)

The LLM is optional. It is used to classify leftover transactions and, if you choose, to parse
statements whose layout the built-in templates don't handle. You can configure it **before or
after `pfa serve` has started — no restart is needed.** `config.toml` is re-read on
every request, so a running server picks up changes made from the Settings screen, from
`pfa llm ...`, or by editing the file by hand.

Pick whichever of these suits you; they all write the same settings.

**1. The CLI** (`--preset` fills in the URL and API format; add `--model`; the model is optional for
single-model local servers such as llama.cpp or vLLM, but OpenAI and Anthropic require one):

```console
$ pfa llm set --preset ollama --model llama3.1 --enable            # local, no key
$ pfa llm set --preset lm-studio --model qwen2.5-7b-instruct --enable
$ pfa llm set --preset openai --model gpt-4.1-mini --enable        # remote
$ pfa llm set-key                                                  # prompts, hidden
$ pfa llm set --preset anthropic --model claude-sonnet-4-5 --enable
$ pfa llm set-key
$ pfa llm test        # one real round trip; `llm show` prints the current settings
```

Presets: `ollama`, `lm-studio`, `openai`, `openai-responses`, `anthropic`, `openrouter`. Use
`pfa llm disable` to turn it off again.

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
`PFA_LLM__ENABLED=true`, `PFA_LLM__BASE_URL=...`,
`PFA_LLM__MODEL=...` (pattern `PFA_LLM__<FIELD>`), and the API key via
`PFA_LLM_API_KEY`.

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

## Spending in detail

The overview above covers what the Spending section does; the details that matter when something
looks off:

- **What is parsed is your choice.** The Import screen shows page thumbnails (the CLI has
  `--pages 1-3,5`); an LLM parse previews the exact redacted text that would be sent, is opt-in per
  statement, and is never auto-confirmed.
- **The cascade ends in review.** Kind detection → your rules → ~120 built-in high-precision rules →
  the issuer's own category column, where the statement prints one → a cache of past
  classifications → an LLM, only for what nothing local could resolve → an `others/uncategorized`
  fallback that still needs your review. The Review screen can re-run the cascade over everything
  uncategorized.
- **A one-time edit teaches the app nothing** about the merchant or future transactions. A rule
  reaches transactions you already have only when you press "Apply to existing" on the Rules screen
  (which first shows how many rows it will change, and whether that spans several users or changes
  kinds, then offers an Undo), and it never changes one you edited by hand; undoing a Review accept
  or a rule application never reverts something you edited afterwards. Older versions silently
  pinned a merchant on every single-transaction edit; on upgrade those pins became ordinary rules
  you can see, edit and delete on the Rules screen (ranked after your other rules), and the
  transactions those pins had classified are no longer treated as hand edits, so "Apply to
  existing" reaches them (an edit you made by hand stays).
- **Reconciliation** uses whichever the layout provides: section subtotals, running balance, or
  the opening/closing balance equation, and banners a warning when they don't add up.

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

Tax forms are a separate import path (`pfa tax import`) with their own parsers. Native
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
pfa migrate              create/upgrade the DB, sync the taxonomy and builtin rules
pfa serve                start the local web server (binds 127.0.0.1, per-launch token)
pfa import <pdf>...      import statement PDFs (--pages, --parser-id llm)
pfa classify             run the classification cascade over unclassified transactions
pfa report               print monthly category totals for a date range
pfa export               export transactions to CSV/JSON (never includes account masks)
pfa backup               move all data to another machine (create / inspect / restore)
pfa llm                  configure the LLM (show / set / enable / disable / set-key / clear-key / test)
pfa doctor               diagnose the local install: Python, data dir, DB, secret backend (read-only; --write-test), LLM
pfa eval                 run the classifier evaluation harness against a labelled CSV
pfa users                manage local users (list / add / set-default)
pfa issuers              manage issuers (list / add), so later imports auto-confirm
pfa tax                  income & tax analysis from tax forms (import / list / report / delete)
```

Run `pfa --help` or `pfa <command> --help` for full options; see
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
