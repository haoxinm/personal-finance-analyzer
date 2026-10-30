# CLI reference

Every command reads/writes the local database in the data directory (see
[INSTALL.md](INSTALL.md#data-location); override with `SPEND_ANALYZER_HOME`). None of them make a
network call unless you've enabled an LLM and run `classify`, `import` with the LLM extractor,
`doctor`, or `llm test`. Commands work the same on Windows, macOS and Linux.

Run `spend-analyzer --help` or `spend-analyzer <command> --help` at any time for the exact,
current option list — this page shows the common case for each command.

## `migrate`

```console
$ spend-analyzer migrate
Database migrated and taxonomy synced.
```

Creates or upgrades the local SQLite database to the latest schema, seeds/updates the taxonomy
from `taxonomy.yaml`, syncs the built-in rules corpus, and ensures a default user exists. Safe to
run repeatedly, and required after every upgrade.

## `serve`

```console
$ spend-analyzer serve
Spend Analyzer is running at http://127.0.0.1:8756/?token=<per-launch token>
```

Starts the local web server. Binds `127.0.0.1` only, mints a fresh random token for this one run,
and opens your browser to it automatically (`--no-open-browser` to skip that). See the
[privacy model](../README.md#privacy-model) for what the token and `Host` check protect against.

## `import`

```console
$ spend-analyzer import statement.pdf --user-id 1
```

Imports one or more statement PDFs (globs are expanded by your shell, not by this command).
Extracts text, proposes an issuer and parser, and — when the proposal is confident — parses and
persists the transactions in the same step. When it isn't confident, the statement is left
`awaiting_extractor` and the exact confirming command is printed:

```console
$ spend-analyzer import statement.pdf --user-id 1
statement.pdf: statement #7 awaiting_extractor (detect_score=0.4); confirm with:
  spend-analyzer import statement.pdf --user-id 1 --issuer-id 3 --parser-id layout_a_credit
```

Options: `--user-id`, `--issuer-id` / `--parser-id` / `--layout-spec-id` (pin the extractor
instead of accepting the proposal), `--remember` (make this the issuer's default extractor for
future imports), `--pages` (see below). Re-importing a byte-identical file is a no-op; an
overlapping statement period never creates duplicate transaction rows.

### Choosing pages and the LLM extractor

`--pages 1-3,5` keeps and parses only those pages (1-based; default: all). Template parsers see
only the selected pages too, so you can drop cover sheets, ads, or legal pages.

`--parser-id llm` parses the statement with the LLM configured via [`llm`](#llm) instead of a
template. It is opt-in per statement, needs an enabled LLM, and **sends the selected pages' text
to that endpoint** after redaction (card/account numbers masked to the last 4, emails replaced,
`[privacy] pii_terms` and user aliases removed; amounts and dates are kept). LLM imports are never
auto-confirmed — you choose the pages and confirm explicitly:

```console
$ spend-analyzer import odd-statement.pdf --user-id 1
odd-statement.pdf: statement #8 awaiting_extractor ...; confirm with:
  spend-analyzer import odd-statement.pdf --user-id 1 --parser-id llm --pages 1-4
$ spend-analyzer import odd-statement.pdf --user-id 1 --parser-id llm --pages 2-3
```

To see the exact text that would be sent first, use the Spending › Import screen's preview
(`GET /api/statements/{id}/llm-preview`). Set `spend-analyzer llm set --default-extractor llm` to
make the LLM the default for new imports.

## `classify`

```console
$ spend-analyzer classify
  40/40 classified ($0.0000)
Classified 38 transaction(s); 2 need review; 1 LLM request(s); $0.0012 spent.
```

Runs the rules-first classification cascade over transactions that don't yet have a category.
Options: `--user-id` / `--all-users`, `--statement-id` (limit to one import). With
the LLM disabled (`[llm] enabled = false`, the default) this never calls an LLM — everything it can't resolve locally
lands in the Review queue with `needs_review=true` instead.

## `llm`

Configures the one LLM endpoint used for classification and (opt-in, per statement) statement
parsing. Every subcommand edits `config.toml`, and **changes take effect immediately, even for a
running `spend-analyzer serve`** — the file is re-read on every request. The same settings are on
the web UI's Settings screen, and as `[llm]` in `config.toml` (README: [Using an LLM](../README.md#using-an-llm-local-or-remote)).

```console
$ spend-analyzer llm show
config file: /home/you/.local/share/spend-analyzer/config.toml
enabled: true
base_url: "http://localhost:11434"
api_format: "openai_chat"
...
mode: local
resolved_url: http://localhost:11434/v1/chat/completions
api key on file: no
[ingest] default_extractor: auto
```

`mode` is derived, not set: `none` (disabled or no base URL; the model is optional), `local` (`localhost`,
`127.0.0.1`, `::1`), or `remote` (anything else). `show` never prints the API key.

### `llm set`

Changes only the options you pass.

| Option | Meaning |
|---|---|
| `--preset NAME` | `ollama`, `lm-studio`, `openai`, `openai-responses`, `anthropic`, `openrouter`; fills in base URL and format (explicit options override) |
| `--base-url URL` | endpoint root, e.g. `http://localhost:11434` |
| `--format FMT` | `openai_chat` (`/v1/chat/completions`), `openai_responses` (`/v1/responses`), `anthropic_messages` (`/v1/messages`) |
| `--path PATH` | request path; `''` = the format's default |
| `--model NAME` | model id as the server knows it; optional for single-model local servers (llama.cpp, vLLM) — left blank, no `model` is sent — but required by OpenAI and Anthropic |
| `--temperature X` | float, or `none` to not send one |
| `--max-tokens N` | int, or `none` for the provider default |
| `--reasoning-effort E` | `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; `''` clears |
| `--extra-body JSON` | JSON object merged into every request body |
| `--extra-header KEY=VALUE` | repeatable; `--clear-extra-headers` removes all |
| `--structured-output` / `--no-structured-output` | ask for schema-constrained JSON (retried without it if the server rejects it) |
| `--batch-size N` | descriptions per classification request |
| `--timeout S` | request timeout in seconds |
| `--confidence-threshold X` | below this, a classification goes to Review |
| `--pages-per-request N` | statement parsing: pages per request (`0` = all selected pages at once) |
| `--enable` / `--disable` | turn the LLM on/off |
| `--default-extractor auto\|llm` | `[ingest] default_extractor` for new imports |
| `--tax-llm-fallback` / `--no-tax-llm-fallback` | `[ingest] tax_llm_fallback`: send redacted, unreadable tax-form pages to the LLM (off by default) |

```console
# Local (nothing leaves this machine)
$ spend-analyzer llm set --preset ollama --model llama3.1 --enable
$ spend-analyzer llm set --preset lm-studio --model qwen2.5-7b-instruct --enable

# Remote
$ spend-analyzer llm set --preset openai --model gpt-4.1-mini --enable
$ spend-analyzer llm set-key
$ spend-analyzer llm set --preset anthropic --model claude-sonnet-4-5 --max-tokens 4096 --enable
$ spend-analyzer llm set-key

# Any OpenAI-compatible server (vLLM, llama.cpp, a gateway) with a custom path and extras
$ spend-analyzer llm set --base-url http://gpu-box:8000 --format openai_chat \
      --path /openai/v1/chat/completions --model my-model --temperature 0 \
      --reasoning-effort low --extra-body '{"top_p": 0.9}' --extra-header X-Team=finance --enable
```

### `llm enable` / `llm disable`

`enable` turns the LLM on (it needs a base URL — set it with `llm set`; a model is optional for single-model local servers); `disable`
turns it off. While it is off, nothing is ever sent anywhere.

### `llm set-key` / `llm clear-key`

```console
$ spend-analyzer llm set-key                       # hidden prompt, for the configured base URL
$ printf '%s' "$KEY" | spend-analyzer llm set-key  # or from stdin
$ spend-analyzer llm set-key --base-url https://api.openai.com
$ spend-analyzer llm clear-key
```

Stores/removes the API key in the OS credential store (macOS Keychain, Windows Credential Manager,
Linux Secret Service), **one key per host**, so a key saved for one endpoint is never sent to
another. Local servers usually need none. On a headless machine without a credential store, set
the `SPEND_ANALYZER_LLM_API_KEY` environment variable instead.

### `llm test`

```console
$ spend-analyzer llm test
OK  ...
```

Makes one real round trip to the configured endpoint; exit code 1 on failure (the message says why:
unreachable, 401, unknown model, wrong path/format, ...).

Environment overrides (take precedence over `config.toml`): `SPEND_ANALYZER_LLM__<FIELD>` (for
example `SPEND_ANALYZER_LLM__MODEL`), and `SPEND_ANALYZER_LLM_API_KEY`.

## `report`

```console
$ spend-analyzer report --from 2026-01-01 --to 2026-01-31
Period   Category       Currency  Total   Count  Avg
-------  -------------  --------  ------  -----  -----
2026-01  groceries      USD       412.33  14     29.45
2026-01  dining         USD       201.10  9      22.34
```

Prints monthly category totals for a date range. `--csv` prints machine-readable CSV instead of a
table; `--user-id` / `--all-users` and `--currency` (D5: aggregations never convert currency,
they filter to one) narrow the result.

## `tax`

Income and tax analysis from W-2, 1099 and Form 1040 PDFs. Everything runs locally: tax forms are
parsed by deterministic code, are never sent to an LLM, and only the last four digits of any
payer or account identifier are kept. What is read from each form, and how the totals are
computed, is in [TAX.md](TAX.md).

### `tax import`

```console
$ spend-analyzer tax import w2-2024.pdf composite-1099-2024.pdf return-2024.pdf
w2-2024.pdf: tax document #1 -> parsed (2024: w2) 1 income / 5 tax item(s)
composite-1099-2024.pdf: tax document #2 -> parsed (2024: 1099-consolidated) 8 income / 1 tax item(s)
return-2024.pdf: tax document #3 -> parsed (2024: 1040) 6 income / 7 tax item(s)
```

Imports one or more tax-form PDFs in a single step (no confirm phase). Options: `--user-id` (whose
forms these are; defaults to the default user) and `--tax-year` (used only when a form doesn't
print its year) and `--no-llm` (skip the optional LLM fallback for this run; see
[TAX.md](TAX.md#llm-fallback-optional)). Re-importing an identical file is a no-op, and a form already imported from a
different file is skipped:

```console
$ spend-analyzer tax import w2-2024.pdf
w2-2024.pdf: tax document #1 -> duplicate
$ spend-analyzer tax import w2-copy.pdf
w2-copy.pdf: tax document #4 -> parsed (2024: w2) 0 income / 0 tax item(s), skipped 1 duplicate form(s)
  warning: 1 form(s) in this file were already imported from another file and were skipped.
```

A file that isn't a supported form, has no text layer, or can't be read is reported on stderr and
the command exits 1 (other files in the same call are still imported). A corrected form imports
with a warning:

```console
$ spend-analyzer tax import w2-corrected.pdf scanned.pdf
w2-corrected.pdf: tax document #5 -> parsed (2024: w2) 1 income / 5 tax item(s)
  warning: Form is marked as corrected; it may replace a form imported earlier.
scanned.pdf: tax document #6 -> unsupported: This PDF doesn't look like a supported tax form (W-2, 1099, 1040).
$ spend-analyzer tax import --tax-year 2024 no-year-printed.pdf
```

The uploaded PDF is not kept unless `[privacy] store_tax_pdf_copies = true` (see
[INSTALL.md](INSTALL.md#privacy-settings)). Running the statement `import` on a tax form prints a
pointer to this command instead of storing it.

### `tax list`

```console
$ spend-analyzer tax list
id  year  forms              status  warnings
--  ----  -----------------  ------  --------
3   2024  1040               parsed  0
2   2024  1099-consolidated  parsed  0
1   2024  w2                 parsed  0
```

Lists imported tax documents, newest first. Options: `--year`, `--user-id`, `--all-users`. A file
that couldn't be recognised still gets a row (status `unsupported` or `error`, year `-`); remove it
with `tax delete`.

### `tax report`

```console
$ spend-analyzer tax report --year 2024
Tax year 2024  (income basis: return; 3 document(s), 3 form(s))
  Income
    Wages and salary               90000.00
    Interest                        1850.00
    Ordinary dividends              4200.00
    Short-term capital gain         1500.00
    Long-term capital gain         12000.00
    Self-employment income             0.00
    Other income                       0.00
    Total income                  109550.00
  Memo (not in total income)
    Qualified dividends (memo)         3100.00
  Withheld
    Federal income tax withheld        11901.40
    State income tax withheld           3512.88
    Local income tax withheld            214.58
  FICA
    Social Security tax         5495.19
    Medicare tax                1285.17
  Tax liability (from return)
    Total federal tax                                14891.00
    Federal tax on ordinary income (est.)            12626.00
    Federal tax on dividends and gains (est.)         2265.00
    Net investment income tax                            0.00
  Payments and result
    Total payments        15191.00
    Refund                  300.00
  Rates
    Federal effective rate    13.59%
    All-in rate               23.18%
    Federal withholding       10.86%
  warning: W-2 wages is $4,567.90 less than the return reports - some forms may not be imported. Income shown is from the return.
  warning: 1099 interest is $405.44 less than the return reports - some forms may not be imported. Income shown is from the return.
  warning: 1099 dividends is $1,749.25 less than the return reports - some forms may not be imported. Income shown is from the return.
  warning: 1099-B net capital gain is $8,439.60 less than the return reports - some forms may not be imported. Income shown is from the return.
  warning: Federal tax withheld on source forms ($11,901.40) differs from the return's line 25d ($15,191.00). Withholding shown is from the source forms.
  warning: The ordinary vs preferential federal tax split is an estimate, not a printed figure.
```

(Example figures come from the synthetic fixtures under `tests/fixtures/generated/tax_*/`, which
is why the W-2, the 1099 and the 1040 don't agree, and why the warnings fire.) With a 1040 on
file, income comes from the return and withholding from the source forms. Prints one block per tax year, newest first, from the
numbers on the forms: no transaction data is involved. Sections with nothing to show are left
out; rates print `n/a` when they can't be computed. Options: `--year` (default: all years),
`--user-id` / `--all-users`, and `--csv`.

`--csv` prints one row per figure, in minor units (cents) with no thousands separators, ready for
a spreadsheet:

```console
$ spend-analyzer tax report --year 2024 --csv
tax_year,section,category,amount_minor
2024,income,salary_wages,8543210
2024,income,interest,144456
2024,income,ordinary_dividends,245075
2024,income,short_term_capital_gain,45000
2024,income,long_term_capital_gain,461040
2024,income,self_employment,0
2024,income,other,0
2024,income,total_income,9438781
2024,memo,tax_exempt,8000
2024,memo,qualified_dividends,190025
2024,withheld,federal_income_tax_withheld,1190140
2024,withheld,state_income_tax_withheld,351288
2024,withheld,local_income_tax_withheld,21458
2024,fica,social_security_tax,549519
2024,fica,medicare_tax,128517
2024,liability,total_federal_tax,1489100
2024,liability,federal_tax_ordinary_est,1262600
2024,liability,federal_tax_preferential_est,226500
2024,liability,net_investment_income_tax,0
2024,result,total_payments,1519100
2024,result,refund,30000
```

| Column | Meaning |
|---|---|
| `tax_year` | The tax year |
| `section` | `income`, `memo`, `withheld`, `fica`, `liability` or `result` |
| `category` | An income or tax category (see [TAX.md](TAX.md#what-is-read-and-where-it-goes)), plus `total_income` in `income` and `tax_exempt` in `memo` |
| `amount_minor` | Amount in cents; negative only for a net capital loss. `total_income` excludes `memo` rows |

The CSV carries amounts only: no rates, warnings, income basis or document ids. Use the text
report for those.

### `tax delete`

```console
$ spend-analyzer tax delete 3
Deleted tax document #3.
$ spend-analyzer tax delete 99
no tax document with id 99
```

Deletes one document, every form and figure parsed from it, and a kept PDF copy if there is one.
Exits 1 when the id doesn't exist. Use it to remove a superseded original after importing a
corrected form.

## `export`

```console
$ spend-analyzer export --output transactions.csv
```

Exports transactions to CSV (default) or `--format json`, to a file or `-` for stdout.
**Never includes `accounts.mask`** (the last-4 account digits) — this is checked directly by
`tests/api/test_export.py`, not only by the response schema. `--user-id` / `--all-users` narrow
the export; with neither, everything is exported.

## `doctor`

```console
$ spend-analyzer doctor
PASS  Python version >= 3.12
PASS  Data directory (...) writable
PASS  Database migrated to head (0001_initial)
PASS  Keychain read/write
SKIP  LLM connectivity (disabled — run `spend-analyzer llm set ...` to configure)
```

Diagnoses the local install: Python version, data directory permissions, migration state,
credential-store access, and (only if an LLM is enabled) connectivity to it
(`PASS  LLM connectivity (...)` or `FAIL  LLM connectivity failed: ...`). Exits non-zero
on any `FAIL`.

## `eval`

Runs the classifier evaluation harness (precision/recall against a labelled CSV of
description → expected category) — used when tuning the built-in rules corpus, not day-to-day.

## `users`

```console
$ spend-analyzer users list
$ spend-analyzer users add "Alex"
$ spend-analyzer users set-default 2
```

Manage local users (D4: statements are imported against a user; the account itself is
auto-matched/auto-created). Most single-person installs never need this beyond the default user
`migrate` creates.

## `issuers`

```console
$ spend-analyzer issuers list
$ spend-analyzer issuers add "My Credit Union" --match-term "MY CREDIT UNION" --match-term "MYCU"
```

Manage issuers and their `match_terms` (§2f.4/§3.12a). Issuer names are **your own local data** —
none is hard-coded, none is ever sent anywhere (see [I1b](../README.md#privacy-model)) — adding
one before your first import from that bank lets later imports auto-confirm instead of asking.
