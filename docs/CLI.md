# CLI reference

Every command reads/writes the local database in the data directory (see
[INSTALL.md](INSTALL.md#data-location); override with `PFA_HOME`). None of them make a
network call unless you've enabled an LLM and run `classify`, `import` with the LLM extractor,
`doctor`, or `llm test`, or you run `tax params update` (one plain `GET` of the public tax
parameter file, no user data). Commands work the same on Windows, macOS and Linux.

The command is `pfa`; `personal-finance-analyzer` is the same program under its long name. The
former name, `spend-analyzer`, still works as a deprecated alias that prints one line on stderr
(`spend-analyzer has been renamed to pfa; ...`) and will be removed in a future release. Environment
variables use the `PFA_` prefix (`PFA_HOME`, `PFA_LLM_API_KEY`, `PFA_LLM__MODEL`, `PFA_TAX__AUTO_UPDATE_PARAMS`,
...); the old `SPEND_ANALYZER_` names are still read, and `PFA_` wins when both are set.

Run `pfa --help` or `pfa <command> --help` at any time for the exact,
current option list — this page shows the common case for each command.

## `migrate`

```console
$ pfa migrate
Database migrated and taxonomy synced.
```

Creates or upgrades the local SQLite database to the latest schema, seeds/updates the taxonomy
from `taxonomy.yaml`, syncs the built-in rules corpus, and ensures a default user exists. Safe to
run repeatedly, and required after every upgrade.

## `serve`

```console
$ pfa serve
Personal Finance Analyzer is running at http://127.0.0.1:8756/?token=<per-launch token>
```

Starts the local web server. Binds `127.0.0.1` only, mints a fresh random token for this one run,
and opens your browser to it automatically (`--no-open-browser` to skip that). See the
[privacy model](../README.md#privacy-model) for what the token and `Host` check protect against.

## `import`

```console
$ pfa import statement.pdf --user-id 1
```

Imports one or more statement PDFs (globs are expanded by your shell, not by this command).
Extracts text, proposes an issuer and parser, and — when the proposal is confident — parses and
persists the transactions in the same step. When it isn't confident, the statement is left
`awaiting_extractor` and the exact confirming command is printed:

```console
$ pfa import statement.pdf --user-id 1
statement.pdf: statement #7 awaiting_extractor (detect_score=0.4); confirm with:
  pfa import statement.pdf --user-id 1 --issuer-id 3 --parser-id layout_a_credit
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
$ pfa import odd-statement.pdf --user-id 1
odd-statement.pdf: statement #8 awaiting_extractor ...; confirm with:
  pfa import odd-statement.pdf --user-id 1 --parser-id llm --pages 1-4
$ pfa import odd-statement.pdf --user-id 1 --parser-id llm --pages 2-3
```

To see the exact text that would be sent first, use the Spending › Import screen's preview
(`GET /api/statements/{id}/llm-preview`). Set `pfa llm set --default-extractor llm` to
make the LLM the default for new imports.

## `classify`

```console
$ pfa classify
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
running `pfa serve`** — the file is re-read on every request. The same settings are on
the web UI's Settings screen, and as `[llm]` in `config.toml` (README: [Using an LLM](../README.md#using-an-llm-local-or-remote)).

```console
$ pfa llm show
config file: /home/you/.local/share/personal-finance-analyzer/config.toml
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
$ pfa llm set --preset ollama --model llama3.1 --enable
$ pfa llm set --preset lm-studio --model qwen2.5-7b-instruct --enable

# Remote
$ pfa llm set --preset openai --model gpt-4.1-mini --enable
$ pfa llm set-key
$ pfa llm set --preset anthropic --model claude-sonnet-4-5 --max-tokens 4096 --enable
$ pfa llm set-key

# Any OpenAI-compatible server (vLLM, llama.cpp, a gateway) with a custom path and extras
$ pfa llm set --base-url http://gpu-box:8000 --format openai_chat \
      --path /openai/v1/chat/completions --model my-model --temperature 0 \
      --reasoning-effort low --extra-body '{"top_p": 0.9}' --extra-header X-Team=finance --enable
```

### `llm enable` / `llm disable`

`enable` turns the LLM on (it needs a base URL — set it with `llm set`; a model is optional for single-model local servers); `disable`
turns it off. While it is off, nothing is ever sent anywhere.

### `llm set-key` / `llm clear-key`

```console
$ pfa llm set-key                       # hidden prompt, for the configured base URL
$ printf '%s' "$KEY" | pfa llm set-key  # or from stdin
$ pfa llm set-key --base-url https://api.openai.com
$ pfa llm clear-key
```

Stores/removes the API key in the OS credential store (macOS Keychain, Windows Credential Manager,
Linux Secret Service), **one key per host**, so a key saved for one endpoint is never sent to
another. Local servers usually need none. On a machine without a credential store, `set-key`
fails with setup instructions (it never saves to a file on its own): set the `PFA_LLM_API_KEY`
environment variable instead, or opt in to the plaintext key file with
`[secrets] allow_file_store = true` (see [INSTALL.md](INSTALL.md#headless-linux-wsl-and-ssh)), in
which case `set-key` says it used the file and prints the plaintext warning. `llm show` prints
where the key comes from (environment variable, keychain, key file), never the key.

### `llm test`

```console
$ pfa llm test
OK  ...
```

Makes one real round trip to the configured endpoint; exit code 1 on failure (the message says why:
unreachable, 401, unknown model, wrong path/format, ...).

Environment overrides (take precedence over `config.toml`): `PFA_LLM__<FIELD>` (for
example `PFA_LLM__MODEL`), and `PFA_LLM_API_KEY`.

## `report`

```console
$ pfa report --from 2026-01-01 --to 2026-01-31
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
$ pfa tax import w2-2024.pdf composite-1099-2024.pdf return-2024.pdf
w2-2024.pdf: tax document #1 -> parsed (2024: w2) 3 income / 5 tax item(s)
composite-1099-2024.pdf: tax document #2 -> parsed (2024: 1099-consolidated) 8 income / 1 tax item(s)
return-2024.pdf: tax document #3 -> parsed (2024: 1040) 6 income / 7 tax item(s)
```

Imports one or more tax-form PDFs in a single step (no confirm phase). Options: `--user-id` (whose
forms these are; defaults to the default user) and `--tax-year` (used only when a form doesn't
print its year) and `--no-llm` (skip the optional LLM fallback for this run; see
[TAX.md](TAX.md#llm-fallback-optional)). Re-importing an identical file is a no-op, and a form already imported from a
different file is skipped:

```console
$ pfa tax import w2-2024.pdf
w2-2024.pdf: tax document #1 -> duplicate
$ pfa tax import w2-copy.pdf
w2-copy.pdf: tax document #4 -> parsed (2024: w2) 0 income / 0 tax item(s), skipped 1 duplicate form(s)
  warning: 1 form(s) in this file were already imported from another file and were skipped.
```

A file that isn't a supported form, has no text layer, or can't be read is reported on stderr and
the command exits 1 (other files in the same call are still imported). A corrected form imports
with a warning:

```console
$ pfa tax import w2-corrected.pdf scanned.pdf
w2-corrected.pdf: tax document #5 -> parsed (2024: w2) 3 income / 5 tax item(s)
  warning: Form is marked as corrected; it may replace a form imported earlier.
scanned.pdf: tax document #6 -> unsupported: This PDF doesn't look like a supported tax form (W-2, 1099, 1040).
$ pfa tax import --tax-year 2024 no-year-printed.pdf
```

The uploaded PDF is not kept unless `[privacy] store_tax_pdf_copies = true` (see
[INSTALL.md](INSTALL.md#privacy-settings)). Running the statement `import` on a tax form prints a
pointer to this command instead of storing it.

### `tax list`

```console
$ pfa tax list
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
$ pfa tax report --year 2024
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
$ pfa tax report --year 2024 --csv
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

### `tax estimate`

```console
$ pfa tax estimate --year 2024
Federal tax estimate 2024, user #1  (filing status single, from default; parameters v1, bundled)
  Income
    Wages and salary                                    85432.10
    ...
    Gross income                                        85432.10
  Deduction and taxable income
    Adjusted gross income                               85432.10
    Deduction (standard)                                14600.00
    Taxable income                                      70832.10
  Tax
    Income tax                                          10635.00
    Total tax                                           10635.00
  Payments and result
    Federal tax withheld                                11876.40
    Total payments                                      11876.40
    Refund                                               1241.40
  Rates
    Effective rate            12.45%
    Marginal (ordinary)       22.00%
    Dividends and gains          n/a
  W-2 box 12 (memo, already excluded from wages)
    Retirement plan deferrals                            3200.00
  warning: No tax profile: filing status single and the standard deduction are assumed.
  Not modeled:
    - Alternative minimum tax.
    ...
```

(Abridged: the real output lists every line.) Computes one user's federal tax for one year from
the W-2 / 1099 forms and the tax profile (filing status, deduction, credits, payments: set it in
the web UI or with `PUT /api/tax/profiles/{user_id}/{tax_year}`), by the method in
[TAX.md](TAX.md#per-user-estimate). A Form 1040, when imported, appears as a cross-check with the
difference to the estimate and is never an input. For married filing jointly the household is
computed once and an "allocation" block shows this user's share. Options: `--year` (required; a
year with no bundled parameters is an error, see `tax params show`) and `--user-id` (default: the
default user). Local only: no network.

The total tax includes the net investment income tax and the Additional Medicare Tax and is never
negative. Refundable credits, the Additional Medicare Tax an employer already withheld and the
excess Social Security tax credit are listed under **payments**, not subtracted from the tax; for
2025-2028 a "Senior deduction" line appears when the profile has people aged 65 or over.

### `tax params`

```console
$ pfa tax params show
Tax parameters in use: version 1 (bundled); bundled v1, downloaded none, imported none
Years: 2022, 2023, 2024, 2025, 2026
Last update check: never; automatic yearly check: off ([tax] auto_update_params)
$ pfa tax params update
current: Parameters are up to date (version 1).
...
$ pfa tax params import my-params.yaml
$ pfa tax params clear
```

The brackets, deductions and limits behind `tax estimate` and the 1040 split. `show` reports the
file in effect and the state of the update check. `update` is the **only command that touches
the network for tax data**: one plain `GET` of the project's public parameter file (no user data),
whatever `[tax] auto_update_params` says (that setting only controls the automatic once-a-year
check); the downloaded file may only add tax years the app lacks, each checked against the year before
it (ranges, structure, year-over-year change), and the date of the check is recorded. It exits 1
when the download fails or is rejected. `import FILE` validates your own file and lets the years it
lists replace the bundled ones (the other years stay); `clear` removes it. Precedence and the opt-in
are described in
[TAX.md](TAX.md#where-the-parameters-come-from-and-the-opt-in-update).

### `tax delete`

```console
$ pfa tax delete 3
Deleted tax document #3.
$ pfa tax delete 99
no tax document with id 99
```

Deletes one document, every form and figure parsed from it, and a kept PDF copy if there is one.
Exits 1 when the id doesn't exist. Use it to remove a superseded original after importing a
corrected form.

## `export`

```console
$ pfa export --output transactions.csv
```

Exports transactions to CSV (default) or `--format json`, to a file or `-` for stdout.
**Never includes `accounts.mask`** (the last-4 account digits) — this is checked directly by
`tests/api/test_export.py`, not only by the response schema. `--user-id` / `--all-users` narrow
the export; with neither, everything is exported.

## `backup`

```console
$ pfa backup create ~/Desktop/
Passphrase (needed to restore; there is no recovery if you lose it):
Repeat for confirmation:
Backup written to /Users/you/Desktop/pfa-2026-10-30.pfabackup (48.2 MB)
Contains: 3 users, 7 accounts, 41 statements, 1893 transactions, 6 tax documents; 49 files.
Not included: your LLM API key (it stays in this machine's keychain; run `pfa llm set-key` on the new one), the extract cache and logs.

$ pfa backup inspect pfa-2026-10-30.pfabackup
$ pfa backup restore pfa-2026-10-30.pfabackup
```

Moves all your data to another machine: `create` on the old one, copy the one file, `restore` on
the new one. See [Moving to another machine](../README.md#moving-to-another-machine).

- **`backup create OUT`** writes the database (a consistent snapshot, so it is safe while `serve`
  is running), the kept statement PDFs, layout specs and `config.toml`. `OUT` is a file, or a
  directory to get a dated `pfa-YYYY-MM-DD.pfabackup` in. It is **encrypted with a
  passphrase by default** (at least 8 characters; prompted twice, or read from the first line of
  `--passphrase-file`). `--no-encrypt` is the explicit opt-out: the file then holds your statements
  and tax forms in clear. An existing `OUT` is only replaced with `--force`. Never included: the
  API key in your OS keychain, the extract cache, logs, and `[llm] extra_headers` / credentials in
  the LLM URL (they can carry secrets).
- **`backup inspect FILE`** prints the format, schema revision, creation time and counts
  (users, accounts, statements, transactions, tax documents, PDFs) without extracting or restoring
  anything. It shows no names, descriptions or amounts. An encrypted file needs its passphrase.
- **`backup restore FILE`** makes the backup this machine's data directory. The whole archive is
  verified (decryption, manifest, SHA-256 of every file, database integrity, schema revision)
  before anything is touched. If the data directory already holds data it refuses unless you pass
  `--replace`, which moves the current directory to `<name>.<timestamp>.before-restore` next to it
  (never deleted). A backup from an older version is migrated; one from a newer version is refused
  with a message to upgrade. Stop `pfa serve` first: a database still open in another
  process stops the restore. A data directory that holds only an empty, freshly migrated database
  (what `pfa migrate` leaves behind) does not count as data and needs no `--replace`.
  Machine-local secret state stays with the machine: the replaced directory's `secrets/` folder
  (the plaintext key file, if you opted in) and its `[secrets]` setting are carried over into the
  restored directory, and the archive's own `[secrets]` setting (an older version could write one)
  is ignored, so a restore never switches the plaintext key file on. The key file is moved, not
  copied: once the restore has succeeded, the original in the `*.before-restore` directory is
  overwritten and removed, so no plaintext key lingers there.

All three exit 1 with a message on any problem (wrong passphrase, damaged or modified file, unknown
format). The web UI has the same two actions under Settings -> Backup & restore.

## `doctor`

```console
$ pfa doctor
PASS  Python version >= 3.12
PASS  Data directory (...) writable
PASS  Database migrated to head (0001_initial)
PASS  Keychain backend: macOS Keychain
INFO  Key source / save target: none saved (a saved key would go to the OS keychain: macOS Keychain)
SKIP  Keychain write test (read-only by default; run `pfa doctor --write-test`)
SKIP  LLM connectivity (disabled — run `pfa llm set ...` to configure)
```

Diagnoses the local install: Python version, data directory permissions, migration state, which
secret backend is in use, and (only if an LLM is enabled) connectivity to it
(`PASS  LLM connectivity (...)` or `FAIL  LLM connectivity failed: ...`). Exits non-zero
on any `FAIL`.

The secret check is **read-only by default**: it reports the active keyring backend, whether it is
a usable secure one (not a missing, `null` or plaintext backend), whether the configured endpoint's
key can be read, and where a saved key would go (environment variable, keychain, or the opt-in
plaintext file, with its warning). It never writes to the keychain, because a keychain write can
raise an OS dialog (for example macOS's "A keychain cannot be found to store ...") or fail on a
machine with a missing login keychain. `WARN` lines (no keychain here, key file holds keys but is
disabled) do not fail the run.

```console
$ pfa doctor --write-test
...
PASS  Keychain write test (probe entry written, read back, deleted)
```

`--write-test` adds an explicit set/get/delete round trip of a probe entry in the OS keychain
(`FAIL  Keychain write test failed: ...` with the cause and fix if it cannot).

## `eval`

Runs the classifier evaluation harness (precision/recall against a labelled CSV of
description → expected category) — used when tuning the built-in rules corpus, not day-to-day.

## `users`

```console
$ pfa users list
$ pfa users add "Alex"
$ pfa users set-default 2
```

Manage local users (D4: statements are imported against a user; the account itself is
auto-matched/auto-created). Most single-person installs never need this beyond the default user
`migrate` creates.

## `issuers`

```console
$ pfa issuers list
$ pfa issuers add "My Credit Union" --match-term "MY CREDIT UNION" --match-term "MYCU"
```

Manage issuers and their `match_terms` (§2f.4/§3.12a). Issuer names are **your own local data** —
none is hard-coded, none is ever sent anywhere (see [I1b](../README.md#privacy-model)) — adding
one before your first import from that bank lets later imports auto-confirm instead of asking.
