# Security policy

## Threat model summary

Personal Finance Analyzer is a single-user, single-machine program: one local process, one local SQLite
database, one local web server. There is no multi-tenant deployment, no authentication beyond
"whoever can reach `127.0.0.1` on this machine," and no server component running anywhere but the
user's own computer (see the non-goals in the design record — hosted/multi-tenant deployment and
authentication are explicitly out of scope for v1).

Given that, the threats this project actually defends against are:

1. **A malicious or compromised web page open in the user's browser reaching the local API.**
   Binding `127.0.0.1` alone does not stop this — DNS rebinding lets a page's script address
   `127.0.0.1` directly once resolved, and a same-origin-looking `<form>` POST needs no CORS
   preflight. Mitigated by a `Host` header allowlist and a per-launch random token required on
   every `/api` request (`src/personal_finance_analyzer/api/security.py`).
2. **Statement or transaction data leaking more than intended to an LLM endpoint.** The core
   promise is that, by default, nothing leaves the machine, and that an LLM you enable sees only
   what you chose. There are exactly two egress seams: `classify/llm/egress.py` sends only
   normalized merchant descriptions (a `FORBIDDEN`-pattern guard raises rather than sanitizes, and
   a CI-enforced grep keeps local-only field names out of that package), and `llm/statement_parser.py`
   sends the text of user-selected pages for statements the user explicitly opted into LLM parsing —
   with the user's personal-info entries (`pii_terms`/user aliases: names in any case/order,
   phone and account numbers in any format) removed first by `core/pii.py`, emails replaced, any
   remaining long account/card numbers masked to the last 4, an exact-payload preview, and never auto-confirmed. Only `llm/client.py` may
   import an HTTP client (plus the opt-in, once-a-year, no-user-data fetch of the public tax parameter
   file, `api/services/tax_params_update.py`), and only those two seams may import it
   (`tests/test_architecture_guards.py`);
   `ingest/` can make no network call. An endpoint on `localhost` keeps everything on the machine;
   amounts and dates in parsed page text do go to a *remote* endpoint if you choose to parse with one.
3. **A malicious PDF or a malicious layout spec.** An uploaded statement is untrusted input
   (size-capped, magic-byte checked, never trusted for its filename); a layout spec — even a
   user-approved one — is untrusted input too (validated against a JSON Schema, every regex
   length-capped and screened for catastrophic backtracking before it is ever executed).
4. **A secret (an LLM API key) ending up somewhere it shouldn't.** Mitigated by storing it only in
   the OS credential store (macOS Keychain, Windows Credential Manager, Linux Secret Service; or the
   `PFA_LLM_API_KEY` environment variable on headless machines), never in the database
   or `config.toml`. The one exception is an explicit opt-in (`[secrets] allow_file_store = true`) to
   a `0600` plaintext key file for machines with no keychain, used only while none is available and
   never part of a backup. A defensive log filter additionally
   redacts anything key-shaped even if it reached a log call by some other path.

5. **Tax identifiers and balances leaking.** Tax-form PDFs contain full SSNs and EINs, and net
   worth uses account balances. Tax forms are parsed by local deterministic code only (no LLM
   extractor, and the tax and net-worth code can import no LLM or HTTP client), only the last four
   digits of a payer or account identifier are ever stored, no full identifier reaches the
   database, logs or extract cache, and the uploaded PDF is deleted unless
   `[privacy] store_tax_pdf_copies` is turned on; balances are never exported or sent anywhere.

6. **A backup file leaking.** `pfa backup create` and Settings -> Backup & restore bundle
   the database, kept PDFs and settings into one file to carry to another machine, so the file holds
   everything the app knows. It is encrypted with a passphrase by default (scrypt + AES-256-GCM,
   authenticated, so tampering is detected), never contains the keychain API key or
   `[llm] extra_headers`, and restore verifies the whole archive in a staging directory before it
   touches anything and never deletes the previous data directory. Writing an unencrypted backup
   needs an explicit opt-out. Keeping the file (and any `*.before-restore` directory) safe is the
   user's job, like any other copy of their data.

**Out of scope / accepted risk for v1** (see the design record's non-goals): database encryption
at rest (the project relies on OS-level disk encryption, e.g. FileVault, BitLocker, or LUKS); multi-user
authentication (there is none — this is a single-user local tool); protecting against an attacker
who already has full access to the user's own account on the machine running it.

See [docs/SECURITY_REVIEW.md](docs/SECURITY_REVIEW.md) for the itemized checklist this threat
model is verified against, re-checked against the shipped code rather than the design document.

## Supported versions

This project ships from a single, continuously-updated main line. Security fixes land on the
latest release; there is no separate long-term-support branch.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a security problem. Instead, open a
[private security advisory](../../security/advisories/new) on this repository (GitHub's
"Report a vulnerability" flow), or email the maintainer listed in the repository's contact
information, with:

- A description of the issue and its impact (in particular, whether it could cause a local-only
  field to leave the machine, or let something other than the user reach the local server).
- Steps to reproduce, or a minimal proof of concept.
- The version/commit you tested against.

Please allow a reasonable time for a fix before any public disclosure. There is no bug-bounty
program; reports are still very much welcome and will be credited in the fix's release notes
unless you ask otherwise.
