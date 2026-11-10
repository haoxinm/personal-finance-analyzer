# Installing Spend Analyzer

Spend Analyzer is a single local program: one `spend-analyzer` command that manages a SQLite
database on your machine and serves a web UI on `127.0.0.1`. There is no server to run elsewhere,
no account to create, and no data leaves your machine unless you turn on an LLM that is not running locally (see
[Privacy](#privacy), below).

## Requirements

- **Python 3.12** (3.13 is not yet supported; the package pins `>=3.12,<3.13`).
- **Windows 10+, macOS, or Linux.** Each uses its platform's standard per-user data directory (see
  [Data location](#data-location)).
- A credential store for the LLM API key, used automatically through
  [`keyring`](https://pypi.org/project/keyring/): macOS Keychain, Windows Credential Manager, or on
  Linux a Secret Service provider (GNOME Keyring, KWallet) — or the `keyrings.alt` package for a
  plain-file fallback. On a headless machine with none of these, set the
  `SPEND_ANALYZER_LLM_API_KEY` environment variable instead. Only needed for a remote LLM that
  requires a key; the default (LLM off) and local servers like Ollama need no secret at all.
- No Node.js/npm needed to *install* — the published wheel already contains the built web UI.
  Node is only needed to build the package from source (see
  [Building from source](#building-from-source)).

## Install

### Option A — `pipx` (recommended)

[`pipx`](https://pipx.pypa.io/) installs the command into its own isolated environment and puts
`spend-analyzer` on your `PATH`, without touching any other Python project's dependencies.

```console
$ pipx install spend-analyzer
```

Or, from a downloaded wheel (e.g. attached to a
[GitHub Release](https://github.com/)) rather than an index:

```console
$ pipx install ./spend_analyzer-1.0.0-py3-none-any.whl
```

### Option B — `pip` into a virtual environment

```console
$ python3.12 -m venv .venv-spend-analyzer
$ .venv-spend-analyzer/bin/pip install spend-analyzer
$ .venv-spend-analyzer/bin/spend-analyzer --help
```

On Windows (PowerShell), the interpreter is `py -3.12` and the scripts live in `Scripts\`:

```powershell
> py -3.12 -m venv .venv-spend-analyzer
> .venv-spend-analyzer\Scripts\Activate.ps1       # in cmd.exe: ...\Scripts\activate.bat
> pip install spend-analyzer
> spend-analyzer --help
```

If PowerShell refuses to run the activation script, either run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation and call
`.venv-spend-analyzer\Scripts\spend-analyzer.exe` directly. `pipx` (Option A) avoids all of this.

## First run

```console
$ spend-analyzer migrate
Database migrated and taxonomy synced.

$ spend-analyzer doctor
PASS  Python version >= 3.12
PASS  Data directory (/Users/you/Library/Application Support/SpendAnalyzer) writable
PASS  Database migrated to head (0001_initial)
PASS  Keychain read/write
SKIP  LLM connectivity (disabled — run `spend-analyzer llm set ...` to configure)

$ spend-analyzer serve
Spend Analyzer is running at http://127.0.0.1:8756/?token=<per-launch token>
Per-launch API token: <per-launch token>
```

`serve` opens your default browser to that URL automatically (pass `--no-open-browser` to skip
that). The URL and the printed token are only valid for this one running process — every launch of
`serve` mints a fresh token (`I8`/`A31`), and the server refuses any `/api` request that doesn't
carry it (as the `token` query parameter for the page load, or an `X-Spend-Token` header for API
calls) or whose `Host` header isn't `127.0.0.1`/`localhost`. It never binds to any interface other
than `127.0.0.1`, so nothing on your network — let alone the internet — can reach it.

If `doctor` reports a `FAIL`, fix that before continuing — it means a real problem: an
unwritable data directory, a database that failed to migrate, a broken Keychain/keyring backend,
or (only if you've enabled an LLM) an endpoint your machine can't reach.

From there:

```console
$ spend-analyzer import ~/Downloads/january-statement.pdf
$ spend-analyzer classify
$ spend-analyzer report --from 2026-01-01 --to 2026-01-31
```

or do the same from the web UI the browser just opened.

## Data location

| OS | Default location |
|---|---|
| macOS | `~/Library/Application Support/SpendAnalyzer/` |
| Windows | `%LOCALAPPDATA%\SpendAnalyzer\` (e.g. `C:\Users\you\AppData\Local\SpendAnalyzer`) |
| Linux / other | `$XDG_DATA_HOME/spend-analyzer/`, i.e. `~/.local/share/spend-analyzer/` by default |

Inside: `spend.db` (SQLite, WAL mode), `statements/<sha256>.pdf` (a copy of each imported PDF,
unless you set `[privacy] store_pdf_copies = false` in `config.toml`), `config.toml`, and
`logs/app.log`. Nothing here is ever uploaded anywhere by Spend Analyzer itself — there is no
telemetry, no analytics, and (unless you opt in to the yearly tax-parameter check, see
[Privacy settings](#privacy-settings)) no update check. `tax_params/` holds the optional
downloaded or imported tax-parameter files and the date of the last check.

To move this data to another computer, use `spend-analyzer backup create` and `backup restore`
(see [Moving to another machine](../README.md#moving-to-another-machine)) rather than copying the
directory by hand: a backup is consistent while the app runs, and restore fixes up the stored PDF
paths for the new location.

Override the location for a second profile, a test, or a portable install:

```console
$ SPEND_ANALYZER_HOME=/path/to/a/directory spend-analyzer serve         # macOS / Linux
```

```powershell
> $env:SPEND_ANALYZER_HOME = "D:\spend-data"; spend-analyzer serve      # Windows PowerShell
```

## Privacy

This is the whole reason to run this instead of a hosted budgeting app: your statements, your
transaction descriptions, and your spending stay on your machine unless you turn on an LLM that
runs elsewhere. The LLM is **off by default**. When on, it can receive (1) normalized, redacted
merchant descriptions of transactions your local rules couldn't classify — never dates, amounts,
account numbers or issuer names — and (2) only if you pick the LLM extractor for a specific
statement, the redacted text of the pages you selected (card/account numbers masked to the last 4,
emails and your configured PII terms removed). Both have an exact-payload preview in the web UI.
Point the LLM at a local server (`localhost`, e.g. Ollama or LM Studio) and nothing leaves your
machine at all. Configure it with `spend-analyzer llm set ...` or the Settings screen — no restart
needed; see [Using an LLM](../README.md#using-an-llm-local-or-remote). The full picture is in the
[README's privacy model](../README.md#privacy-model) and the code-verified checklist in
[docs/SECURITY_REVIEW.md](SECURITY_REVIEW.md).

### Privacy settings

The `[privacy]` table in `config.toml` controls what is kept on disk:

```toml
[privacy]
store_pdf_copies = true          # keep a copy of each imported statement PDF (default true)
store_extract_cache = false      # debugging only: cache extracted page text (default false)
store_tax_pdf_copies = false     # keep tax-form PDFs after `tax import` (default false)
pii_terms = []                   # names, numbers and addresses to remove before any LLM call
```

`store_tax_pdf_copies` is off by default because a W-2, 1099 or 1040 PDF contains full Social
Security and employer identification numbers. With it off, the PDF is deleted once its figures are
imported and only the parsed amounts (plus the last four digits of payer and account identifiers)
remain in the database. Turn it on only if you want the original file kept in the data directory's
`statements/` folder; `spend-analyzer tax delete` removes a kept copy. Tax-form text is never
written to the extract cache, whatever `store_extract_cache` says. The key is edited in the file
only, not on the Settings screen. See [TAX.md](TAX.md).

The `[tax]` table holds the one network opt-in outside the LLM:

```toml
[tax]
auto_update_params = false       # once a year, fetch the federal tax parameter file (default false)
```

With it on, the first tax estimate or summary opened in a calendar year makes one plain `GET` of a
public file in the project's GitHub repository (no query string, cookies or user data), keeps a
validated copy in `tax_params/` and does not ask again that year. It never blocks the page; a
failure is ignored. Leave it off and the app only ever uses the figures bundled with it (2022 -
2026); `spend-analyzer tax params update` (or **Check now** under Settings › Tax parameters, which
also has this toggle) checks on request. Details in
[TAX.md](TAX.md#where-the-parameters-come-from-and-the-opt-in-update).

## Upgrading

```console
$ pipx upgrade spend-analyzer      # or: pip install --upgrade spend-analyzer
$ spend-analyzer migrate           # re-run after every upgrade — brings the DB schema
                                    # and taxonomy up to date; safe to run when already current
```

## Uninstalling

```console
$ pipx uninstall spend-analyzer    # or: pip uninstall spend-analyzer
```

This removes the program only. Your data directory (see [above](#data-location)) is left in
place; delete it yourself if you want to remove your statements and database too.

## Troubleshooting

Run `spend-analyzer doctor` first — it checks the Python version, data directory permissions,
database migration state, credential-store access, and (if an LLM is enabled) LLM connectivity, and
prints one `PASS`/`FAIL` line per check.

- **`FAIL  Keychain access failed: ...`** — most often a headless Linux box (SSH session, container,
  WSL) with no Secret Service. Install and unlock one (`gnome-keyring`, KWallet), or
  `pip install keyrings.alt` for a plain-file, less-secure fallback — or skip the credential store
  and export `SPEND_ANALYZER_LLM_API_KEY`. It only matters for a remote LLM that needs a key;
  with the LLM off, or a local server, the app never touches the credential store.
- **`FAIL  LLM connectivity failed: ...`** — run `spend-analyzer llm show` and
  `spend-analyzer llm test`. Usual causes: wrong base URL, the wrong `api_format` or `path` for the
  server, a missing/incorrect key (`llm set-key`), or a model name the server doesn't know.
- **Browser shows a blank page / 404 for `/assets/...`** — the wheel you installed is missing the
  built web UI. This should never happen from a published release, but if you built the wheel
  yourself, see [Building from source](#building-from-source): the frontend build step must
  succeed and populate `src/spend_analyzer/web/` before packaging.
- **Port already in use** — another process (or a previous, still-running `spend-analyzer serve`)
  is using the configured port (default `8756`). Stop it, or set `[server] port = <other port>` in
  `config.toml`.
- **`spend-analyzer: command not found`** — the install didn't put its `bin`/`Scripts` directory on
  your `PATH`. `pipx` normally handles this for you (`pipx ensurepath`, then open a new shell); for
  a plain `pip install` into a virtualenv, activate that virtualenv or invoke the binary from its
  `bin/` (or `Scripts\`, on Windows) directory directly. On Windows, `pipx ensurepath` then a new
  terminal.

## Building from source

Building the wheel yourself (rather than installing a published release) additionally requires
**Node.js 22+ and npm** on `PATH`, because a `hatchling` build hook (`hatch_build.py`) runs the
frontend's own production build (`frontend/`'s `vite build`) and bundles its output into
`src/spend_analyzer/web/` before the wheel is packaged — that is what makes the installed wheel a
single self-contained program with no separate frontend build step for the person installing it.

```console
$ git clone <this repository>
$ cd spending-analysis-from-bills
$ uv build                 # writes dist/*.whl and dist/*.tar.gz
$ pip install dist/*.whl   # or: pipx install dist/*.whl
```

An editable/development install (`uv sync`, `pip install -e .`) skips the frontend build entirely
(so day-to-day Python development needs no Node.js at all) and serves the API standalone; point
`frontend`'s own dev server at it instead (`cd frontend && npm run dev`) to work on the UI. Set
`SPEND_ANALYZER_SKIP_FRONTEND_BUILD=1` to build a wheel deliberately without the UI (e.g. an API-
only environment); `spend-analyzer serve` still runs, it just won't have a page to serve at `/`.

After a `git pull` (or any change under `frontend/`), rebuild the web UI — an editable install
(`uv sync`) never does, and `spend-analyzer serve` keeps showing the old UI until you do:

```console
$ cd frontend && npm ci && npm run build
```

`serve` prints a warning at startup when the built UI is missing or older than the frontend sources.
