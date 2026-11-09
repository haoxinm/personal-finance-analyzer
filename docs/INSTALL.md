# Installing Personal Finance Analyzer

Personal Finance Analyzer is a single local program: one `pfa` command that manages a SQLite
database on your machine and serves a web UI on `127.0.0.1`. There is no server to run elsewhere,
no account to create, and no data leaves your machine unless you turn on an LLM that is not running locally (see
[Privacy](#privacy), below).

## Requirements

- **Python 3.12** (3.13 is not yet supported; the package pins `>=3.12,<3.13`).
- **Windows 10+, macOS, or Linux.** Each uses its platform's standard per-user data directory (see
  [Data location](#data-location)).
- A credential store for the LLM API key, used automatically through
  [`keyring`](https://pypi.org/project/keyring/): macOS Keychain, Windows Credential Manager, or on
  Linux a Secret Service provider (GNOME Keyring, KWallet) — see
  [Linux keychain setup](#linux-keychain-setup). On a machine with none of these, set the
  `PFA_LLM_API_KEY` environment variable instead, or opt in to the plaintext key file
  ([Headless Linux, WSL and SSH](#headless-linux-wsl-and-ssh)). Only needed for a remote LLM that
  requires a key; the default (LLM off) and local servers like Ollama need no secret at all.
- No Node.js/npm needed to *install* — the published wheel already contains the built web UI.
  Node is only needed to build the package from source (see
  [Building from source](#building-from-source)).

## Install

### Option A — `pipx` (recommended)

[`pipx`](https://pipx.pypa.io/) installs the command into its own isolated environment and puts
`pfa` on your `PATH`, without touching any other Python project's dependencies.

```console
$ pipx install personal-finance-analyzer
```

Or, from a downloaded wheel (e.g. attached to a
[GitHub Release](https://github.com/)) rather than an index:

```console
$ pipx install ./personal_finance_analyzer-1.0.0-py3-none-any.whl
```

### Option B — `pip` into a virtual environment

```console
$ python3.12 -m venv .venv-pfa
$ .venv-pfa/bin/pip install personal-finance-analyzer
$ .venv-pfa/bin/pfa --help
```

On Windows (PowerShell), the interpreter is `py -3.12` and the scripts live in `Scripts\`:

```powershell
> py -3.12 -m venv .venv-pfa
> .venv-pfa\Scripts\Activate.ps1       # in cmd.exe: ...\Scripts\activate.bat
> pip install personal-finance-analyzer
> pfa --help
```

If PowerShell refuses to run the activation script, either run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or skip activation and call
`.venv-pfa\Scripts\pfa.exe` directly. `pipx` (Option A) avoids all of this.

## First run

```console
$ pfa migrate
Database migrated and taxonomy synced.

$ pfa doctor
PASS  Python version >= 3.12
PASS  Data directory (/Users/you/Library/Application Support/PersonalFinanceAnalyzer) writable
PASS  Database migrated to head (0001_initial)
PASS  Keychain backend: macOS Keychain
INFO  Key source / save target: none saved (a saved key would go to the OS keychain: macOS Keychain)
SKIP  Keychain write test (read-only by default; run `pfa doctor --write-test`)
SKIP  LLM connectivity (disabled — run `pfa llm set ...` to configure)

$ pfa serve
Personal Finance Analyzer is running at http://127.0.0.1:8756/?token=<per-launch token>
Per-launch API token: <per-launch token>
```

`serve` opens your default browser to that URL automatically (pass `--no-open-browser` to skip
that). The URL and the printed token are only valid for this one running process — every launch of
`serve` mints a fresh token (`I8`/`A31`), and the server refuses any `/api` request that doesn't
carry it (as the `token` query parameter for the page load, or an `X-PFA-Token` header for API
calls) or whose `Host` header isn't `127.0.0.1`/`localhost`. It never binds to any interface other
than `127.0.0.1`, so nothing on your network — let alone the internet — can reach it.

If `doctor` reports a `FAIL`, fix that before continuing — it means a real problem: an
unwritable data directory, a database that failed to migrate, a failed `--write-test`,
or (only if you've enabled an LLM) an endpoint your machine can't reach. A `WARN` about the keychain
is not a failure: it means no keychain is available here, which only matters once you want to save
an API key (see [Headless Linux, WSL and SSH](#headless-linux-wsl-and-ssh)).

From there:

```console
$ pfa import ~/Downloads/january-statement.pdf
$ pfa classify
$ pfa report --from 2026-01-01 --to 2026-01-31
```

or do the same from the web UI the browser just opened.

## Data location

| OS | Default location |
|---|---|
| macOS | `~/Library/Application Support/PersonalFinanceAnalyzer/` |
| Windows | `%LOCALAPPDATA%\PersonalFinanceAnalyzer\` (e.g. `C:\Users\you\AppData\Local\PersonalFinanceAnalyzer`) |
| Linux / other | `$XDG_DATA_HOME/personal-finance-analyzer/`, i.e. `~/.local/share/personal-finance-analyzer/` by default |

Inside: `finance.db` (SQLite, WAL mode), `statements/<sha256>.pdf` (a copy of each imported PDF,
unless you set `[privacy] store_pdf_copies = false` in `config.toml`), `config.toml`, and
`logs/app.log`. Nothing here is ever uploaded anywhere by Personal Finance Analyzer itself — there is no
telemetry, no analytics, and (unless you opt in to the yearly tax-parameter check, see
[Privacy settings](#privacy-settings)) no update check. `tax_params/` holds the optional
downloaded or imported tax-parameter files and the date of the last check.

To move this data to another computer, use `pfa backup create` and `backup restore`
(see [Moving to another machine](../README.md#moving-to-another-machine)) rather than copying the
directory by hand: a backup is consistent while the app runs, and restore fixes up the stored PDF
paths for the new location.

Override the location for a second profile, a test, or a portable install (the former name of the
variable, `SPEND_ANALYZER_HOME`, is still read when `PFA_HOME` is not set):

```console
$ PFA_HOME=/path/to/a/directory pfa serve         # macOS / Linux
```

```powershell
> $env:PFA_HOME = "D:\finance-data"; pfa serve      # Windows PowerShell
```

## Linux keychain setup

On Linux the API key is kept by a **Secret Service** provider over D-Bus (GNOME Keyring or KWallet).
`keyring` reaches it through the `secretstorage` and `jeepney` packages, which this package
installs on Linux automatically. You need a provider running in a D-Bus session, unlocked:

| Distribution | Install |
| --- | --- |
| Debian / Ubuntu | `sudo apt install gnome-keyring libsecret-1-0` (KDE: `kwalletmanager`) |
| Fedora / RHEL | `sudo dnf install gnome-keyring libsecret` (KDE: `kwalletmanager`) |
| Arch | `sudo pacman -S gnome-keyring libsecret` (KDE: `kwallet`) |

A normal desktop login (GNOME, KDE, most others) starts and unlocks it for you; check with
`pfa doctor` (it should print `Keychain backend: Secret Service (...)`). `pfa doctor --write-test`
additionally stores, reads and deletes a probe entry.

## Headless Linux, WSL and SSH

With no desktop session there is usually no D-Bus session bus and no Secret Service, so the
keychain is unavailable (`pfa doctor` says so, and `pfa llm set-key` fails with these options).
Pick one, in order of preference:

1. **An environment variable** — nothing is stored anywhere:

   ```console
   $ export PFA_LLM_API_KEY=sk-...        # SPEND_ANALYZER_LLM_API_KEY is still accepted
   ```

2. **A throwaway Secret Service** for the shell session (needs `dbus` and `gnome-keyring`):

   ```console
   $ dbus-run-session -- bash
   $ echo -n 'a-password' | gnome-keyring-daemon --unlock --components=secrets
   $ pfa llm set-key
   ```

   Keys go to the keyring inside that session.
3. **The plaintext key file** (last resort). Add to `config.toml` (or set
   `PFA_SECRETS__ALLOW_FILE_STORE=true`, or tick the box in Settings -> LLM provider):

   ```toml
   [secrets]
   allow_file_store = false   # set to true to opt in
   ```

   Then `pfa llm set-key` writes `<data dir>/secrets/api_keys.toml` (directory `0700`, file `0600`,
   written atomically). **It is plaintext at rest**, protected only by those permissions: anyone
   who can read your account's files can read it. It is used for *saving* only while no keychain is
   available (it is still read when it holds a key), it is never included in a backup, and the
   app never turns it on by itself.

   **Turning it off does not leave the key on disk unnoticed.** Untick the box (or `PUT
   /api/settings/secret-storage`) while a key is still in the file and Settings asks what to do with
   it: *move* it into the OS keychain (offered, and the default, when a keychain works), *delete* the
   file (overwritten with zeros, then removed; the default when there is no keychain, behind an
   explicit button; you enter the key again to use an LLM) or *keep* it (it stays as plaintext on
   disk, unused, and Settings keeps saying so until you delete it). Overwriting is best effort: a
   copy-on-write filesystem, an SSD or a snapshot may keep older blocks, which is one more reason to
   prefer a keychain.

A key is looked up in this order: `PFA_LLM_API_KEY`, the OS keychain, then the key file (only if
enabled). On Windows the Credential Manager backend needs no setup.

## Privacy

This is the whole reason to run this instead of a hosted budgeting app: your statements, your
transaction descriptions, and your spending stay on your machine unless you turn on an LLM that
runs elsewhere. The LLM is **off by default**. When on, it can receive (1) normalized, redacted
merchant descriptions of transactions your local rules couldn't classify — never dates, amounts,
account numbers or issuer names — and (2) only if you pick the LLM extractor for a specific
statement, the redacted text of the pages you selected (card/account numbers masked to the last 4,
emails and your configured PII terms removed). Both have an exact-payload preview in the web UI.
Point the LLM at a local server (`localhost`, e.g. Ollama or LM Studio) and nothing leaves your
machine at all. Configure it with `pfa llm set ...` or the Settings screen — no restart
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
`statements/` folder; `pfa tax delete` removes a kept copy. Tax-form text is never
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
2026); `pfa tax params update` (or **Check now** under Settings › Tax parameters, which
also has this toggle) checks on request. Details in
[TAX.md](TAX.md#where-the-parameters-come-from-and-the-opt-in-update).

## Upgrading

```console
$ pipx upgrade personal-finance-analyzer      # or: pip install --upgrade personal-finance-analyzer
$ pfa migrate           # re-run after every upgrade — brings the DB schema
                                    # and taxonomy up to date; safe to run when already current
```

### Upgrading from Spend Analyzer

The program was called *Spend Analyzer* (package `spend-analyzer`, command `spend-analyzer`) before
it became Personal Finance Analyzer. Your data and settings carry over by themselves; the only
manual step is swapping the installed package, because the old and new packages both ship a
`spend-analyzer` command and cannot be installed side by side:

```console
$ pipx uninstall spend-analyzer               # or: pip uninstall spend-analyzer
$ pipx install personal-finance-analyzer      # or: pip install personal-finance-analyzer
$ pfa migrate
```

Uninstalling the old package leaves your data directory alone. The first time you run a command
that opens the database with the new version (`pfa migrate`, `pfa serve`, `pfa import`, any
command that reads or writes statements, tax documents or settings in the database), the carry-over
below happens once. Read-only commands (`pfa llm show`, `pfa backup inspect`, ...) never move or
rename anything; until then they keep reading the old directory.

- **Data directory.** The old default directory (`SpendAnalyzer` on macOS and Windows,
  `spend-analyzer` on Linux) is *moved* (renamed, not copied) to the new one named in
  [Data location](#data-location). If it cannot be moved, the old directory keeps being used and a
  warning says so. It is also left where it is while an old `spend-analyzer serve` still has its
  database open (the old server would keep writing into the old path): stop the old server, then run
  any database command again to finish the move. If both exist, the new one is used and a warning names the old one, which is left
  untouched. A directory you chose with `SPEND_ANALYZER_HOME` or `PFA_HOME` is never moved.
- **Database.** `spend.db` is renamed to `finance.db` in whichever data directory you use. A database
  another process still has open is left under its old name until it is closed.
- **Kept PDF copies.** The database records each kept statement and tax PDF by absolute path.
  After the directory is moved those paths are rewritten to the new location, and a copy whose
  recorded path no longer exists is looked up at `statements/<sha256>.pdf` under the current data
  directory, so re-parsing, viewing and deleting keep working.
- **LLM API key.** A key saved in the OS credential store under the old service name is copied to
  the new one the first time it is read, then the old entry is deleted.
- **Environment variables.** Every `SPEND_ANALYZER_*` variable (`SPEND_ANALYZER_HOME`,
  `SPEND_ANALYZER_LLM_API_KEY`, `SPEND_ANALYZER_LLM__MODEL`, ...) is still honored; the `PFA_*`
  variable of the same name wins when both are set.
- **Command.** `spend-analyzer` still runs the same program (installed with the new package) but
  prints a one-line notice on stderr; it will be removed in a future release. Use `pfa`.
- **Backups.** `.spendbackup` files made before the rename can still be inspected and restored; new
  ones are `.pfabackup`.
- **Web UI.** Your remembered user selection is carried over in the browser.

## Uninstalling

```console
$ pipx uninstall personal-finance-analyzer    # or: pip uninstall personal-finance-analyzer
```

(If you still have the program installed under its former name, `spend-analyzer`, remove that too:
see [Upgrading from Spend Analyzer](#upgrading-from-spend-analyzer).)

This removes the program only. Your data directory (see [above](#data-location)) is left in
place; delete it yourself if you want to remove your statements and database too.

## Troubleshooting

Run `pfa doctor` first — it checks the Python version, data directory permissions,
database migration state, which secret backend is in use (read-only), and (if an LLM is enabled) LLM
connectivity, and prints one `PASS`/`FAIL` line per check.

- **`WARN  No usable OS keychain ...`** or **`could not store the key: ...`** — most often a headless
  Linux box (SSH session, container, WSL) with no Secret Service, or a macOS login keychain that is
  missing or locked. The message names the cause and the fix; see
  [Linux keychain setup](#linux-keychain-setup) and
  [Headless Linux, WSL and SSH](#headless-linux-wsl-and-ssh). It only matters for a remote LLM that
  needs a key; with the LLM off, or a local server, the app never touches the credential store.
- **macOS: "A keychain cannot be found to store ..."** — the login keychain is missing. Open
  Keychain Access and create/reset it (File > New Keychain, or Settings > Reset My Default
  Keychain), or `security unlock-keychain ~/Library/Keychains/login.keychain-db`. `pfa doctor` never
  writes to the keychain unless you pass `--write-test`, so it cannot trigger this dialog by itself.
- **`FAIL  LLM connectivity failed: ...`** — run `pfa llm show` and
  `pfa llm test`. Usual causes: wrong base URL, the wrong `api_format` or `path` for the
  server, a missing/incorrect key (`llm set-key`), or a model name the server doesn't know.
- **Browser shows a blank page / 404 for `/assets/...`** — the wheel you installed is missing the
  built web UI. This should never happen from a published release, but if you built the wheel
  yourself, see [Building from source](#building-from-source): the frontend build step must
  succeed and populate `src/personal_finance_analyzer/web/` before packaging.
- **Port already in use** — another process (or a previous, still-running `pfa serve`)
  is using the configured port (default `8756`). Stop it, or set `[server] port = <other port>` in
  `config.toml`.
- **`pfa: command not found`** — the install didn't put its `bin`/`Scripts` directory on
  your `PATH`. `pipx` normally handles this for you (`pipx ensurepath`, then open a new shell); for
  a plain `pip install` into a virtualenv, activate that virtualenv or invoke the binary from its
  `bin/` (or `Scripts\`, on Windows) directory directly. On Windows, `pipx ensurepath` then a new
  terminal.

## Building from source

Building the wheel yourself (rather than installing a published release) additionally requires
**Node.js 22+ and npm** on `PATH`, because a `hatchling` build hook (`hatch_build.py`) runs the
frontend's own production build (`frontend/`'s `vite build`) and bundles its output into
`src/personal_finance_analyzer/web/` before the wheel is packaged — that is what makes the installed wheel a
single self-contained program with no separate frontend build step for the person installing it.

```console
$ git clone https://github.com/haoxinm/personal-finance-analyzer
$ cd personal-finance-analyzer
$ uv build                 # writes dist/*.whl and dist/*.tar.gz
$ pip install dist/*.whl   # or: pipx install dist/*.whl
```

An editable/development install (`uv sync`, `pip install -e .`) skips the frontend build entirely
(so day-to-day Python development needs no Node.js at all) and serves the API standalone; point
`frontend`'s own dev server at it instead (`cd frontend && npm run dev`) to work on the UI. Set
`PFA_SKIP_FRONTEND_BUILD=1` to build a wheel deliberately without the UI (e.g. an API-
only environment); `pfa serve` still runs, it just won't have a page to serve at `/`.

After a `git pull` (or any change under `frontend/`), rebuild the web UI — an editable install
(`uv sync`) never does, and `pfa serve` keeps showing the old UI until you do:

```console
$ cd frontend && npm ci && npm run build
```

`serve` prints a warning at startup when the built UI is missing or older than the frontend sources.
