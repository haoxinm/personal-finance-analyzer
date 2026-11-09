"""Pydantic request/response models for every route (§3.12).

One model per shape in the OpenAPI schema this module ultimately generates (`scripts/
export_openapi.py` writes it to `frontend/src/api/openapi.json`); every route sets
`response_model` so that schema is complete enough for `openapi-typescript` to build a typed
frontend client from it.

Amounts are minor units everywhere (I5). `mask` never appears here (A12/export leak guard;
`accounts.mask` is local-only and is never returned by any route in this module, including
`/export`).
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from personal_finance_analyzer.core.forecast import (
    MAX_AMOUNT_MINOR,
    MAX_EVENTS,
    MAX_HORIZON_YEARS,
    MAX_YEAR,
    MIN_YEAR,
)
from personal_finance_analyzer.core.net_worth import BalanceCategory
from personal_finance_analyzer.core.types import ClassifiedBy


class Kind(StrEnum):
    """The seven `Kind` values (§3.1), as a real enum rather than a `Literal` alias: FastAPI
    only emits a named, reusable `components.schemas.Kind` in the OpenAPI document (which the
    frontend's generated client references as `components["schemas"]["Kind"]`) for an enum type
    — a `Literal` field is inlined per-field instead."""

    PURCHASE = "purchase"
    REFUND = "refund"
    PAYMENT = "payment"
    TRANSFER = "transfer"
    FEE = "fee"
    INTEREST = "interest"
    ADJUSTMENT = "adjustment"


AccountType = Literal["credit", "checking", "savings"]
LlmMode = Literal["none", "local", "remote"]
ApiFormat = Literal["openai_chat", "openai_responses", "anthropic_messages"]
StatementStatus = Literal[
    "pending",
    "awaiting_extractor",
    "parsed",
    "no_text_layer",
    "unsupported_layout",
    "error",
]
JobStatus = Literal["queued", "running", "done", "error", "cancelled"]
JobKind = Literal["import", "classify"]


class _Model(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Users -------------------------------------------------------------------------------------


class User(_Model):
    id: int
    name: str
    is_default: bool


class UserCreate(_Model):
    name: str
    is_default: bool = False


class UserUpdate(_Model):
    name: str | None = None
    is_default: bool | None = None


# --- Accounts ------------------------------------------------------------------------------------


class Account(_Model):
    id: int
    user_id: int
    issuer_id: int | None
    account_type: AccountType
    currency: str


class AccountCreate(_Model):
    user_id: int
    issuer_id: int | None = None
    account_type: AccountType
    mask: str = ""
    currency: str = "USD"


class AccountUpdate(_Model):
    issuer_id: int | None = None
    account_type: AccountType | None = None
    mask: str | None = None
    currency: str | None = None


# --- Statements ----------------------------------------------------------------------------------


class Statement(_Model):
    id: int
    user_id: int | None = None
    account_id: int | None
    issuer_id: int | None = None
    parser_id: str | None = None
    layout_spec_id: int | None = None
    status: StatementStatus
    period_start: date | None
    period_end: date | None
    txn_count: int | None
    reconciliation_delta_minor: int | None = None
    detect_score: float | None
    shape_warnings: str | None
    error_detail: str | None
    created_at: datetime
    page_count: int | None = None
    #: 1-based page numbers kept for parsing (and sent to the LLM, for the LLM parser);
    #: `None` = every page.
    selected_pages: list[int] | None = None


class ExtractorProposal(_Model):
    issuer_id: int | None
    parser_id: str | None
    layout_spec_id: int | None
    confidence: float
    auto_confirmed: bool


class StatementUploadResponse(_Model):
    statement: Statement
    proposal: ExtractorProposal
    #: This exact file was already imported (`statement` is the earlier, parsed one): nothing
    #: new will be added by importing it again.
    duplicate: bool = False


class ExtractRequest(_Model):
    issuer_id: int
    layout_spec_id: int | None = None
    #: A built-in parser id, or `"llm"` for the LLM statement parser.
    parser_id: str | None = None
    remember: bool = False
    #: 1-based pages to keep/parse; `None` = the statement's saved selection (default: all).
    pages: list[int] | None = None


class StatementPageInfo(_Model):
    """`GET /api/statements/{id}/pages` row: enough to choose pages without opening the PDF.
    A thumbnail is at `GET /api/statements/{id}/pages/{page_number}/image`."""

    page_number: int
    char_count: int
    snippet: str  # first ~300 characters of the page's extracted text
    selected: bool


class LlmParsePreview(_Model):
    """`GET /api/statements/{id}/llm-preview?pages=1,2`: the exact page text (after
    redaction) that the LLM statement parser would send, one entry per request. No network."""

    mode: LlmMode
    resolved_url: str
    model: str
    pages: list[int]
    requests: list[str]
    total_chars: int
    #: Replacements made by the user's personal-info entries (names, phone/account numbers...).
    redactions: int = 0


class StatementPatch(_Model):
    user_id: int | None = None
    account_id: int | None = None


class JobIdResponse(_Model):
    job_id: str


# --- Jobs ----------------------------------------------------------------------------------------


class Job(_Model):
    id: str
    kind: JobKind
    status: JobStatus
    progress: float = Field(ge=0.0, le=1.0)
    message: str | None = None
    group_id: str | None = None
    error_detail: str | None = None
    #: Set on an `import` job that chained a `classify` job for the rows it inserted (job_tasks.
    #: py's `enqueue_import_then_classify`) — `None` on any other job, or an import job that
    #: inserted nothing to classify.
    classify_job_id: str | None = None
    #: The chained classify job's own result counts, once *it* is `done` (looked up via
    #: `classify_job_id`; `None` until then). Lets a caller show an exact "N need review" for
    #: the statement this import job was for without polling the classify job separately.
    classified: int | None = None
    needs_review: int | None = None
    #: On a finished `import` job: transactions newly added, and rows skipped because the same
    #: transaction was already present (e.g. from an overlapping statement). `None` otherwise.
    inserted: int | None = None
    skipped_duplicates: int | None = None
    #: On a finished re-parse job: hand edits (category/kind or notes) kept on the rebuilt rows,
    #: and edits dropped because no single rebuilt row could be matched to them. `None` otherwise.
    restored_edits: int | None = None
    unmatched_edits: int | None = None


# --- Transactions ----------------------------------------------------------------------------------


#: Where a one-click category suggestion comes from (`classify/candidates.py`), in precedence
#: order: a one-time edit you made, a payment/transfer's fixed filing, your rules (and legacy
#: pins), built-in rules, the issuer's own label, the learned merchant cache, and the saved
#: answer of an earlier LLM run.
CandidateSource = Literal[
    "one_time_edit", "kind", "user_rule", "builtin_rule", "issuer", "merchant_cache", "llm"
]


class TransactionCandidate(_Model):
    """One cascade layer's answer for one transaction. Clicking it is an ordinary one-time edit
    (`PATCH /transactions/{id}` with this category, subcategory and `kind` when set)."""

    source: CandidateSource
    category_key: str
    subcategory_key: str
    #: A rule's kind override; send it with the category so the click does what the rule does.
    kind: Kind | None = None
    #: The rule's pattern, the issuer's own label, or where the cached answer was learned.
    label: str
    rule_id: int | None = None
    #: This is the answer the row is filed under now (the one-time edit, for an edited row).
    is_current: bool
    #: This is what the row gets (or, for an edited row, would get) with no one-time edit: the
    #: winning layer. "Revert to automatic" files an edited row under it.
    is_automatic: bool = False


class TransactionCandidates(_Model):
    """`GET /api/transactions/{id}/candidates`: what each layer would say, in precedence order."""

    transaction_id: int
    candidates: list[TransactionCandidate]
    #: At least two layers name different categories, and the row has no one-time edit.
    conflict: bool


class Transaction(_Model):
    id: int
    statement_id: int
    account_id: int
    posted_date: date
    transaction_date: date | None
    description_raw: str
    description_clean: str
    amount_minor: int
    currency: str
    kind: Kind
    category_key: str | None
    subcategory_key: str | None
    merchant_key: str | None
    notes: str | None
    needs_review: bool
    #: Only present when the list was asked for it (`include_candidates` / `include_conflict`).
    candidates: list[TransactionCandidate] | None = None
    conflict: bool | None = None


class TransactionList(_Model):
    items: list[Transaction]
    total: int


class TransactionIds(_Model):
    """`GET /api/transactions/ids`: every id matching the filters (not one page of them)."""

    ids: list[int]
    total: int


class TransactionPatch(_Model):
    """A one-time edit of one transaction: it changes only that transaction and always wins over
    every rule and re-run. Nothing is remembered about the merchant unless `create_rule` is set."""

    category_key: str | None = None
    subcategory_key: str | None = None
    kind: Kind | None = None
    notes: str | None = None
    #: Also save an exact user rule for this transaction's merchant ("always categorize this
    #: merchant this way"), for future transactions. Needs a category and subcategory. Off by
    #: default: an edit is otherwise a one-time patch.
    create_rule: bool = False


class TransactionState(_Model):
    """The classification fields one accept (or "apply to existing") can change on a
    transaction. Values are validated: a token naming an unknown kind or `classified_by` is
    rejected."""

    category_id: int | None
    subcategory_id: int | None
    confidence: float | None = Field(default=None, ge=0, le=1)
    classified_by: ClassifiedBy | None
    needs_review: bool
    kind: Kind
    is_spend: bool
    merchant_canonical: str | None


class TransactionSnapshot(_Model):
    """One transaction as the operation found it (`before`) and left it (`after`); sent back
    unchanged to `POST /review/undo`, which restores `before` only if the row still equals
    `after`."""

    transaction_id: int
    before: TransactionState
    after: TransactionState
    #: The row's dedupe hash; undo skips a row whose hash differs (its id was reused).
    dedupe_hash: str


class RuleState(_Model):
    category_id: int
    subcategory_id: int
    kind_override: Kind | None = None
    enabled: bool = True


class RuleSnapshot(_Model):
    """The merchant's exact rule around an accept: `before=None` means the accept created it."""

    rule_id: int
    before: RuleState | None = None
    after: RuleState


class AcceptUndo(_Model):
    """Opaque undo token for one Review accept or one "apply rule to existing" (everything it
    changed): each patched transaction before and after and, for an "always categorize" accept,
    the merchant's rule. Held by the client and validated on use."""

    transactions: list[TransactionSnapshot]
    rule: RuleSnapshot | None = None


class ReviewAcceptRequest(_Model):
    transaction_id: int
    category_key: str
    subcategory_key: str
    #: Also make this the merchant's standing exact rule ("always categorize this way").
    create_rule: bool = False
    #: Other pending rows of the same merchant to categorize the same way; ids that do not
    #: qualify (different merchant, no longer pending, hand-corrected) are skipped.
    similar_ids: list[int] = Field(default_factory=list)
    #: Also set this transaction's kind (a suggestion from a rule with a kind override carries
    #: one). Applies to `transaction_id` only, never to `similar_ids`.
    kind: Kind | None = None


class ReviewAcceptResponse(_Model):
    transaction: Transaction
    #: The `similar_ids` actually applied (its length is the "Applied to N more" count).
    similar_ids: list[int]
    undo: AcceptUndo


class ReviewUndoResponse(_Model):
    restored: int
    #: Transactions left as they are because they changed (or were deleted) after the operation
    #: being undone: a later edit is never reverted.
    skipped: list[int] = Field(default_factory=list)
    #: The accept's rule was left alone because it was edited, deleted or replaced since.
    rule_skipped: bool = False


class BulkUpdateRequest(_Model):
    """Edit every listed transaction one time each (no rule, no merchant memory)."""

    transaction_ids: list[int]
    category_key: str | None = None
    subcategory_key: str | None = None
    kind: Kind | None = None


class BulkUpdateResponse(_Model):
    updated: int


class RevertToAutomaticResponse(_Model):
    """`POST /transactions/{id}/revert-to-automatic`: the row, handed back to automatic
    classification (never an LLM call), and the same undo token an accept carries
    (`POST /review/undo` puts the one-time edit back)."""

    transaction: Transaction
    undo: AcceptUndo


class BulkRevertRequest(_Model):
    transaction_ids: list[int]


class BulkRevertResponse(_Model):
    """`reverted` rows had an edit and went back to automatic; `skipped` were not edited by hand
    (or no longer exist) and are untouched. `undo` restores every reverted row."""

    reverted: int
    skipped: int
    undo: AcceptUndo


# --- Classify --------------------------------------------------------------------------------------


class ClassifyRunRequest(_Model):
    user_id: int | None = None
    statement_id: int | None = None
    #: Only reconsider rows with no category, parked in `others/uncategorized`, or still flagged
    #: `needs_review`; `False` reconsiders everything. Either way a transaction the user
    #: corrected by hand is never touched.
    only_unclassified: bool = True


class ClassifyRunResponse(_Model):
    job_id: str
    group_id: str
    #: How many transactions this run will reconsider.
    transaction_count: int = 0


class ClassifyPreviewRow(_Model):
    merchant_key: str
    description_clean: str


class LlmRun(_Model):
    id: int
    group_id: str
    provider: str
    model: str
    schema_mode: str
    cost_usd: float | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    created_at: datetime


# --- Analytics -------------------------------------------------------------------------------------


class AnalyticsRow(_Model):
    period: str | None = None
    category: str | None = None
    subcategory: str | None = None
    user: int | None = None
    account: int | None = None
    merchant: str | None = None
    currency: str
    total_minor: int
    txn_count: int
    avg_minor: int


class TopMerchantRow(_Model):
    merchant: str
    #: A friendlier label for `merchant` (a lowercase `merchant_key`): the most common
    #: `merchant_canonical` recorded for it, or else the A5 representative `description_clean`.
    #: Local UI only — never egressed (`description_clean` is egress-eligible in general, but
    #: this endpoint never sends it anywhere else).
    display_name: str
    total_minor: int
    txn_count: int


# --- Taxonomy --------------------------------------------------------------------------------------


class Subcategory(_Model):
    id: int
    key: str
    name: str
    pending: bool
    merged_into: str | None = None


class Category(_Model):
    key: str
    name: str
    subcategories: list[Subcategory]


class MergeRequest(_Model):
    into_id: int


# --- Issuers ---------------------------------------------------------------------------------------


class Issuer(_Model):
    id: int
    name: str
    slug: str
    match_terms: list[str]
    default_spec_id: int | None = None


class IssuerCreate(_Model):
    name: str
    match_terms: list[str] = Field(default_factory=list)


class IssuerUpdate(_Model):
    name: str | None = None
    match_terms: list[str] | None = None
    default_spec_id: int | None = None


class IssuerConflictDetail(_Model):
    message: str
    existing_issuer_id: int


class IssuerConflictResponse(_Model):
    """The 409 body for an issuer create/rename that collides with an existing issuer; mirrors
    the layout-spec 422 convention of a structured `detail`."""

    detail: IssuerConflictDetail


class ConflictResponse(_Model):
    detail: str


class IssuerMatchPreviewRequest(_Model):
    match_terms: list[str]


class IssuerMatchPreviewRow(_Model):
    statement_id: int
    original_name: str
    status: StatementStatus
    period_start: date | None = None
    period_end: date | None = None


# --- Layout specs ------------------------------------------------------------------------------------


class LayoutSpec(_Model):
    id: int
    name: str
    version: int
    issuer_id: int | None
    approved: bool
    source: str


class LayoutSpecCreate(_Model):
    name: str
    spec_yaml: str
    issuer_id: int | None = None


class LayoutSpecFieldError(_Model):
    field: str
    message: str


class LayoutSpecDryRunRequest(_Model):
    statement_id: int
    spec_yaml: str


class LayoutSpecDryRunRow(_Model):
    description_clean: str
    posted_date: date
    amount_minor: int


class LayoutSpecDryRunResponse(_Model):
    txn_count: int
    total_minor: int
    currency: str
    sample_rows: list[LayoutSpecDryRunRow]
    warnings: list[str]
    reconciliation_delta_minor: int | None = None


class StatementPreviewWord(_Model):
    text: str
    x0: float
    x1: float
    top: float
    bottom: float


class StatementPreviewPage(_Model):
    page_number: int
    width: float
    height: float
    words: list[StatementPreviewWord]


# --- Rules -------------------------------------------------------------------------------------------


class Rule(_Model):
    id: int
    pattern: str
    category_key: str
    subcategory_key: str
    kind: Kind | None = None
    source: str
    #: `exact`, `contains` or `regex`, matched against the merchant key.
    match_type: str = "contains"
    #: Lower runs first. Among user rules, `priority` then `id` decides which one matches.
    priority: int = 100
    #: Only on `POST /rules`: how many existing transactions applying the new rule would change
    #: (`POST /rules/{id}/apply` applies it). `None` everywhere else.
    existing_matches: int | None = None


class RuleApplyResponse(_Model):
    updated: int
    #: Hand to `POST /review/undo` to reverse this application (rows edited since are skipped).
    undo: AcceptUndo


class RuleApplyPreview(_Model):
    """What `POST /rules/{id}/apply` would do right now, for the confirmation step."""

    #: Existing transactions that would change.
    matches: int
    #: How many distinct users own them.
    user_count: int
    #: How many of them would get a different `kind` from the rule's kind override.
    kind_changes: int
    #: The rule has no owner, so it applies to every user's transactions.
    all_users: bool


class RuleCreate(_Model):
    pattern: str
    match_type: Literal["exact", "contains", "regex"] = "contains"
    category_key: str
    subcategory_key: str
    kind: Kind | None = None


class RuleUpdate(_Model):
    pattern: str | None = None
    category_key: str | None = None
    subcategory_key: str | None = None
    kind: Kind | None = None


# --- Settings ----------------------------------------------------------------------------------------


class LlmSettings(_Model):
    """`[llm]` (one endpoint for classification and opt-in statement parsing). `mode`,
    `resolved_url` and `has_key` are derived and read-only: ignored on `PUT`."""

    enabled: bool = False
    base_url: str = ""
    api_format: ApiFormat = "openai_chat"
    path: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str = ""
    extra_body: dict[str, Any] = Field(default_factory=dict)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    structured_output: bool = True
    batch_size: int = 50
    timeout_s: int = 120
    confidence_threshold: float = 0.7
    parse_pages_per_request: int = 0
    # --- derived, read-only ---
    mode: LlmMode = "none"  # none (disabled/incomplete) | local (localhost) | remote
    resolved_url: str = ""
    has_key: bool = False


class PrivacySettings(_Model):
    store_pdf_copies: bool
    store_extract_cache: bool
    pii_terms: list[str]


class PiiTestRequest(_Model):
    """`POST /api/settings/pii-test`: apply personal-info entries to a pasted sample. `terms`
    (unsaved draft entries) defaults to the saved `[privacy] pii_terms`. Nothing leaves the
    machine and nothing is stored."""

    text: str = Field(max_length=100_000)
    terms: list[str] | None = Field(default=None, max_length=500)


class PiiTestResponse(_Model):
    redacted_text: str
    count: int


class IngestSettings(_Model):
    always_confirm_extractor: bool
    default_extractor: Literal["auto", "llm"] = "auto"
    tax_llm_fallback: bool = False
    date_format_hints: list[str]
    default_currency: str


class ServerSettings(_Model):
    port: int


class TaxSettings(_Model):
    """`[tax]`: `auto_update_params` is the opt-in once-a-year check for updated federal tax
    parameters (a plain GET of one public file, no user data; off by default)."""

    auto_update_params: bool = False


class WatchSettings(_Model):
    """`[watch]`: import PDFs put into the documents folder by hand, in the background."""

    enabled: bool = False
    #: Seconds between two looks at the folder (10 to 86400; out-of-range values are clamped).
    interval_seconds: int = 60
    #: Read-only (ignored on `PUT`): the folder that is watched (the documents folder).
    watched_folder: str = ""


class Settings(_Model):
    llm: LlmSettings
    privacy: PrivacySettings
    ingest: IngestSettings
    server: ServerSettings
    #: Optional on `PUT` (an older client that does not send it keeps the saved value).
    tax: TaxSettings = Field(default_factory=TaxSettings)
    #: Optional on `PUT`, like `tax`.
    watch: WatchSettings = Field(default_factory=WatchSettings)
    #: Read-only (ignored on `PUT`): the settings an environment variable currently controls, as
    #: ``section.field`` (e.g. ``llm.enabled``). Their values here are the effective ones; saving
    #: never writes them to `config.toml`.
    env_overrides: list[str] = Field(default_factory=list)


class TestLlmRequest(_Model):
    """Optional `POST /api/settings/test-llm` body: test these (unsaved, draft) LLM settings
    instead of the saved ones. Omit the body to test what is saved."""

    llm: LlmSettings | None = None


class TestLlmResponse(_Model):
    ok: bool
    detail: str | None = None
    mode: LlmMode = "none"
    resolved_url: str = ""
    latency_ms: int | None = None


class ApiKeyPut(_Model):
    """`PUT /api/settings/api-key` body (I7: the key is written straight to the OS keychain via
    `config.set_api_key` — never to `config.toml`, the database, a response, or a log line).
    The key is stored for `base_url`'s host (default: the saved `[llm] base_url`), so it is only
    ever sent to that host."""

    api_key: str
    base_url: str = ""


class SecretStorageStatus(_Model):
    """`GET /api/settings/secret-storage`: which secret backend holds (or would hold) the LLM API
    key on this machine. Never contains key material."""

    #: Where the current endpoint's key comes from: an environment variable, the OS keychain, the
    #: opt-in plaintext file, or nowhere.
    key_source: Literal["env", "keychain", "file", "none"]
    #: Where a newly saved key would go; `none` means a save will fail (no keychain, file store off).
    storage: Literal["keychain", "file", "none"]
    keychain_name: str
    keychain_usable: bool
    keychain_detail: str
    env_var: str | None
    #: The key was found only under the pre-rename keychain service ("spend-analyzer"); it is moved
    #: to the current one the next time it is used.
    key_legacy: bool = False
    file_store_enabled: bool
    file_store_path: str
    file_store_has_keys: bool
    #: Whether the pre-rename keychain service was looked at. The Settings screen does not on load
    #: (reading another build's entry can make macOS prompt), so `key_legacy` then only means "not
    #: checked"; `?check_legacy=true` does it on request.
    legacy_checked: bool = False
    #: What the `PUT` that just ran did with the plaintext key file: `moved` (into the keychain),
    #: `deleted` (overwritten and removed), `kept` (left on disk, unused) or `none`. `moved_keys`
    #: counts the keys moved; `kept_keys` those whose keychain key already existed and was kept.
    #: Always `none` / 0 on a `GET`.
    file_key_outcome: Literal["none", "moved", "deleted", "kept"] = "none"
    moved_keys: int = 0
    #: Keys dropped from the file because the keychain already held a key for that account (the
    #: keychain's value was kept, not overwritten).
    kept_keys: int = 0
    #: Platform-specific setup advice, filled in when no usable keychain exists.
    guidance: str
    #: The plaintext-at-rest warning, filled in while the file store is enabled.
    warning: str
    #: The environment variable that controls `file_store_enabled` right now, if any. While it is
    #: set the opt-in cannot be changed through the API (`PUT` answers `409`).
    file_store_env_var: str | None = None


class SecretStoragePut(_Model):
    """`PUT /api/settings/secret-storage`: switch the plaintext file store on or off
    (`[secrets] allow_file_store`).

    Turning it off (or calling again with it already off) while the key file still holds keys
    needs a decision about them, `file_key_action`: `move` them into the OS keychain (the default
    when one works), `delete` the file (overwritten, then removed) or `keep` it on disk, unused.
    Without a working keychain there is no default: the call is refused with `409` until the
    caller chooses `delete` or `keep`, so a key never stays on disk by accident."""

    allow_file_store: bool
    file_key_action: Literal["move", "delete", "keep"] | None = None


# --- Net worth ---------------------------------------------------------------------------------


class BalanceEntry(_Model):
    id: int
    user_id: int
    category: BalanceCategory
    account_name: str
    year: int
    month: int
    balance_minor: int
    currency: str
    note: str | None
    created_at: str
    updated_at: str


#: Largest accepted balance (minor units): keeps sums of many accounts far inside int64.
MAX_BALANCE_MINOR = 10**15
_NOTE_MAX_LENGTH = 500


class BalanceEntryUpsert(_Model):
    user_id: int
    category: BalanceCategory
    account_name: str = Field(min_length=1, max_length=100)
    year: int = Field(ge=1900, le=2200)
    month: int = Field(ge=1, le=12)
    balance_minor: int = Field(ge=0, lt=MAX_BALANCE_MINOR)
    currency: Literal["USD"] = "USD"  # USD only, like the tax tables
    note: str | None = Field(default=None, max_length=_NOTE_MAX_LENGTH)


class BalanceEntryPatch(_Model):
    balance_minor: int | None = Field(default=None, ge=0, lt=MAX_BALANCE_MINOR)
    note: str | None = Field(default=None, max_length=_NOTE_MAX_LENGTH)


class NetWorthCategoryTotal(_Model):
    category: BalanceCategory
    balance_minor: int
    signed_minor: int


class NetWorthAccountBalance(_Model):
    user_id: int
    category: BalanceCategory
    account_name: str
    balance_minor: int
    signed_minor: int
    as_of: str
    carried: bool
    #: Carried forward from an entry more than `STALE_AFTER_MONTHS` (3) months old.
    stale: bool = False
    #: Whole months between `as_of` and the snapshot period, or, for the default latest snapshot,
    #: the current month if that is later.
    months_old: int = 0


class NetWorthPoint(_Model):
    period: str
    by_category: dict[str, int]
    assets_minor: int
    liabilities_minor: int
    net_worth_minor: int


class NetWorthAccountPoint(_Model):
    period: str
    balance_minor: int
    carried: bool


class NetWorthAccountSeries(_Model):
    user_id: int
    category: BalanceCategory
    account_name: str
    points: list[NetWorthAccountPoint]


class NetWorthSnapshot(_Model):
    period: str
    assets_minor: int
    liabilities_minor: int
    net_worth_minor: int
    previous_period: str | None
    change_minor: int | None
    categories: list[NetWorthCategoryTotal]
    accounts: list[NetWorthAccountBalance]


class NetWorthSummary(_Model):
    currency: str
    user_id: int | None
    periods: list[str]
    series: list[NetWorthPoint]
    account_series: list[NetWorthAccountSeries]
    snapshot: NetWorthSnapshot | None
    #: Data problems that change a figure, e.g. two entries of one account in a month whose
    #: names differ only in letter case (only the most recently entered one is used).
    warnings: list[str] = Field(default_factory=list)


# --- Income & tax (tax forms) ------------------------------------------------------------------
#
# Amounts are integer minor units (USD). Only the last four digits of any taxpayer identifier are
# ever exposed (`payer_tin_last4`, `account_last4`); `stored_path` and full TINs never appear here
# (tripwire in `tests/api/test_tax.py`). `payer_name` is local-only data, like `issuers.name`.


class TaxFormType(StrEnum):
    """Mirror of `core.tax_types.FormType` (kept in sync by a test), as a named OpenAPI enum."""

    W2 = "w2"
    F1099_INT = "1099-int"
    F1099_DIV = "1099-div"
    F1099_B = "1099-b"
    F1099_NEC = "1099-nec"
    F1099_MISC = "1099-misc"
    F1099_CONSOLIDATED = "1099-consolidated"
    F1040 = "1040"
    STATE_RETURN = "state-return"


class IncomeCategoryEnum(StrEnum):
    """Mirror of `core.tax_types.IncomeCategory`."""

    SALARY_WAGES = "salary_wages"
    INTEREST = "interest"
    ORDINARY_DIVIDENDS = "ordinary_dividends"
    QUALIFIED_DIVIDENDS = "qualified_dividends"
    SHORT_TERM_CAPITAL_GAIN = "short_term_capital_gain"
    LONG_TERM_CAPITAL_GAIN = "long_term_capital_gain"
    SELF_EMPLOYMENT = "self_employment"
    OTHER = "other"
    RETIREMENT_DEFERRAL = "retirement_deferral"
    HSA_EMPLOYER = "hsa_employer"


class TaxCategoryEnum(StrEnum):
    """Mirror of `core.tax_types.TaxCategory`."""

    FEDERAL_INCOME_TAX_WITHHELD = "federal_income_tax_withheld"
    STATE_INCOME_TAX_WITHHELD = "state_income_tax_withheld"
    LOCAL_INCOME_TAX_WITHHELD = "local_income_tax_withheld"
    SOCIAL_SECURITY_TAX = "social_security_tax"
    MEDICARE_TAX = "medicare_tax"
    TOTAL_FEDERAL_TAX = "total_federal_tax"
    FEDERAL_TAX_ORDINARY_EST = "federal_tax_ordinary_est"
    FEDERAL_TAX_PREFERENTIAL_EST = "federal_tax_preferential_est"
    NET_INVESTMENT_INCOME_TAX = "net_investment_income_tax"
    ADDITIONAL_MEDICARE_TAX = "additional_medicare_tax"
    SELF_EMPLOYMENT_TAX = "self_employment_tax"
    STATE_INCOME_TAX_LIABILITY = "state_income_tax_liability"
    ESTIMATED_TAX_PAYMENTS = "estimated_tax_payments"
    TOTAL_PAYMENTS = "total_payments"
    REFUND = "refund"
    AMOUNT_OWED = "amount_owed"


TaxFormRole = Literal["source", "return"]
TaxDocumentStatus = Literal["parsed", "error", "unsupported"]
TaxImportStatus = Literal["parsed", "duplicate", "error", "unsupported"]
TaxIncomeBasis = Literal["source_documents", "return", "none", "mixed"]
#: Where a form's / item's figures came from (`manual` = typed or confirmed by the user).
TaxExtractionMethod = Literal["parser", "llm", "manual"]
TaxItemOrigin = Literal["parser", "llm", "manual"]
TaxFilingStatus = Literal["single", "mfj", "mfs", "hoh", "qss"]


class IncomeItemOut(_Model):
    id: int
    category: IncomeCategoryEnum
    amount_minor: int
    currency: str
    source_form: str
    box: str | None
    label: str | None
    is_memo: bool
    origin: TaxItemOrigin


class TaxItemOut(_Model):
    id: int
    category: TaxCategoryEnum
    amount_minor: int
    currency: str
    source_form: str
    box: str | None
    label: str | None
    jurisdiction: str | None
    is_estimate: bool
    origin: TaxItemOrigin


class TaxFormOut(_Model):
    id: int
    form_type: TaxFormType
    role: TaxFormRole
    tax_year: int
    payer_name: str | None
    payer_tin_last4: str | None
    account_last4: str | None
    filing_status: str | None
    agi_minor: int | None
    taxable_income_minor: int | None
    is_corrected: bool
    extraction_method: TaxExtractionMethod
    is_user_edited: bool
    #: True while any item is still AI-extracted and unreviewed (cleared by `confirm`).
    needs_review: bool


class TaxFormDetail(TaxFormOut):
    income_items: list[IncomeItemOut]
    tax_items: list[TaxItemOut]


class TaxDocument(_Model):
    id: int
    user_id: int
    original_name: str
    tax_year: int | None
    status: TaxDocumentStatus
    form_types: list[TaxFormType]
    page_count: int | None
    warnings: list[str]
    error_detail: str | None
    created_at: str
    #: Is a PDF copy kept (`[privacy] store_tax_pdf_copies`)? Page thumbnails and reading chosen
    #: pages with the LLM need it.
    has_pdf: bool
    #: A copy was kept but is no longer in the documents folder (moved out or deleted by hand and
    #: not found by content either): the UI says "PDF not found". Only true when `has_pdf` is false.
    pdf_missing: bool = False
    #: When the PDF the user re-selected for this document stops being available (ISO-8601 UTC);
    #: `None` = none staged. Only meaningful when `has_pdf` is false.
    staged_until: str | None = None


class TaxDocumentDetail(TaxDocument):
    forms: list[TaxFormDetail]


class TaxPageInfo(_Model):
    """`GET /api/tax/documents/{id}/pages` row. A thumbnail is at
    `GET /api/tax/documents/{id}/pages/{page_number}/image`."""

    page_number: int
    char_count: int
    #: Would the automatic LLM fallback send (part of) this page?
    suggested: bool
    #: Why this page can never be sent (no text, Form 1040); `None` = it can.
    excluded_reason: str | None


class TaxSourceResponse(_Model):
    """`POST /api/tax/documents/{id}/source`: the re-selected file is the document's original;
    it is held temporarily (never kept) so its pages can be shown and read."""

    pages: list[TaxPageInfo]
    expires_at: str


class TaxLlmReadRequest(BaseModel):
    """`POST /api/tax/documents/{id}/llm-fallback`: the pages the user agreed to send."""

    model_config = ConfigDict(extra="forbid")

    pages: list[int] = Field(min_length=1, max_length=500)


class TaxLlmReadResponse(_Model):
    document: TaxDocumentDetail
    #: The pages that were sent (after dropping pages that cannot be).
    pages: list[int]
    requests_sent: int
    inserted_forms: int
    skipped_duplicate_forms: int
    #: Stored consolidated forms that received sections the parser had not read.
    merged_forms: int
    income_item_count: int
    tax_item_count: int
    warnings: list[str]


# Manual edits (request bodies). Unknown fields are rejected; for the PATCH models only the fields
# actually sent are applied (`model_fields_set`), so an explicit `null` clears a nullable field.
_MAX_MINOR = 10**12


class _TaxInput(_Model):
    model_config = ConfigDict(from_attributes=True, extra="forbid")


class TaxManualDocumentCreate(_TaxInput):
    user_id: int
    tax_year: int = Field(ge=2000, le=2099)
    original_name: str | None = Field(default=None, max_length=200)


class TaxDocumentPatch(_TaxInput):
    """Reassign a document (with its forms and items) to another user."""

    user_id: int


class IncomeItemIn(_TaxInput):
    category: IncomeCategoryEnum
    amount_minor: int = Field(ge=-_MAX_MINOR, le=_MAX_MINOR)
    box: str | None = Field(default=None, max_length=8)
    label: str | None = Field(default=None, max_length=80)
    is_memo: bool = False
    source_form: str | None = Field(default=None, max_length=20)


class IncomeItemPatch(_TaxInput):
    category: IncomeCategoryEnum | None = None
    amount_minor: int | None = Field(default=None, ge=-_MAX_MINOR, le=_MAX_MINOR)
    box: str | None = Field(default=None, max_length=8)
    label: str | None = Field(default=None, max_length=80)
    is_memo: bool | None = None
    source_form: str | None = Field(default=None, max_length=20)


class TaxItemIn(_TaxInput):
    category: TaxCategoryEnum
    amount_minor: int = Field(ge=0, le=_MAX_MINOR)
    box: str | None = Field(default=None, max_length=8)
    label: str | None = Field(default=None, max_length=80)
    jurisdiction: str | None = Field(default=None, max_length=64)
    is_estimate: bool = False
    source_form: str | None = Field(default=None, max_length=20)


class TaxItemPatch(_TaxInput):
    category: TaxCategoryEnum | None = None
    amount_minor: int | None = Field(default=None, ge=0, le=_MAX_MINOR)
    box: str | None = Field(default=None, max_length=8)
    label: str | None = Field(default=None, max_length=80)
    jurisdiction: str | None = Field(default=None, max_length=64)
    is_estimate: bool | None = None
    source_form: str | None = Field(default=None, max_length=20)


_Last4 = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}$")]


class TaxFormCreate(_TaxInput):
    form_type: TaxFormType
    tax_year: int | None = Field(default=None, ge=2000, le=2099)
    payer_name: str | None = Field(default=None, max_length=120)
    payer_tin_last4: _Last4 | None = None
    account_last4: _Last4 | None = None
    is_corrected: bool = False
    filing_status: TaxFilingStatus | None = None
    agi_minor: int | None = Field(default=None, ge=-_MAX_MINOR, le=_MAX_MINOR)
    taxable_income_minor: int | None = Field(default=None, ge=0, le=_MAX_MINOR)
    income_items: list[IncomeItemIn] = Field(default_factory=list, max_length=200)
    tax_items: list[TaxItemIn] = Field(default_factory=list, max_length=200)


class TaxFormPatch(_TaxInput):
    form_type: TaxFormType | None = None
    tax_year: int | None = Field(default=None, ge=2000, le=2099)
    payer_name: str | None = Field(default=None, max_length=120)
    payer_tin_last4: _Last4 | None = None
    account_last4: _Last4 | None = None
    is_corrected: bool | None = None
    filing_status: TaxFilingStatus | None = None
    agi_minor: int | None = Field(default=None, ge=-_MAX_MINOR, le=_MAX_MINOR)
    taxable_income_minor: int | None = Field(default=None, ge=0, le=_MAX_MINOR)


class TaxSkippedFrom(_Model):
    """An earlier document whose form made this upload's form a skipped duplicate."""

    document_id: int
    original_name: str
    form_type: str


class TaxImportResponse(_Model):
    document: TaxDocument
    status: TaxImportStatus
    inserted_forms: int
    skipped_duplicate_forms: int
    skipped_from: list[TaxSkippedFrom]
    income_item_count: int
    tax_item_count: int
    warnings: list[str]
    error: str | None
    #: True when the opt-in LLM fallback was consulted for this file.
    llm_used: bool = False
    #: Newly stored forms that were read entirely by the LLM fallback (they need review).
    llm_form_count: int = 0


class IncomeCategoryAmount(_Model):
    category: IncomeCategoryEnum
    label: str
    amount_minor: int
    is_memo: bool


class TaxCategoryAmount(_Model):
    category: TaxCategoryEnum
    label: str
    amount_minor: int


class TaxMemoAmount(_Model):
    """A memo line (not part of total income): `qualified_dividends`, `tax_exempt`, or the W-2 box 12
    memos `retirement_deferral` and `hsa_employer`."""

    category: str
    label: str
    amount_minor: int


class TaxSupersededForm(_Model):
    """A source form replaced by a corrected form with the same payer and account (not counted)."""

    form_id: int
    document_id: int
    user_id: int
    form_type: TaxFormType
    superseded_by_form_id: int


class TaxHeadline(_Model):
    """What to show as the tax paid for the year (minor units). `federal_basis` says where the
    federal figure comes from: `return` (the 1040's total tax with the Additional Medicare Tax
    counted once, in `fica_minor`), `estimate` (the per-user estimate, no return), `withholding`
    (federal tax withheld), or `mixed` (an all-users view whose users differ)."""

    federal_minor: int
    federal_basis: Literal["return", "estimate", "withholding", "mixed"]
    fica_minor: int
    state_minor: int
    total_minor: int


class TaxYearSummary(_Model):
    tax_year: int
    user_id: int | None
    income_basis: TaxIncomeBasis
    income_by_category: list[IncomeCategoryAmount]
    memo_by_category: list[TaxMemoAmount]
    total_income_minor: int
    withheld: list[TaxCategoryAmount]
    fica: list[TaxCategoryAmount]
    liability: list[TaxCategoryAmount]
    payments_minor: int | None
    refund_minor: int | None
    amount_owed_minor: int | None
    refund_or_owed_derived: bool
    federal_effective_rate: float | None
    all_in_rate: float | None
    withholding_rate: float | None
    document_count: int
    form_count: int
    warnings: list[str]
    #: `None` when there is no tax figure at all (no withholding, FICA, return or estimate).
    headline: TaxHeadline | None = None
    superseded_forms: list[TaxSupersededForm] = Field(default_factory=list)


# --- Per-user federal tax estimate ----------------------------------------------------------------

TaxDeductionMode = Literal["standard", "itemized"]
#: Largest amount accepted in a tax profile field: $10 billion in cents.
MAX_TAX_AMOUNT_MINOR = 10**12


class TaxProfileIn(_Model):
    """`PUT /api/tax/profiles/{user_id}/{tax_year}` body: the manual inputs of the estimate.
    Amounts are non-negative cents. `joint_with_user_id` (the spouse, another existing user) is
    only valid with `filing_status` `mfj`. A profile describes its own user: with a linked spouse
    who has a profile too, the two profiles are added together, so each spouse enters their own
    boxes (the estimate limits each to 2 boxes and 1 person aged 65+). A joint profile whose
    spouse has no profile of their own carries the household total.
    `age65_or_blind_count` counts the boxes checked for the additional standard deduction (age 65
    or older, blind): at most 2 per person, 4 for `mfj` (both spouses). `age65_count` is the
    number of people aged 65 or older (for the 2025-2028 senior deduction): at most 1, 2 for
    `mfj`, and not more than the boxes."""

    filing_status: TaxFilingStatus
    joint_with_user_id: int | None = None
    deduction_mode: TaxDeductionMode = "standard"
    itemized_deductions_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    age65_or_blind_count: int = Field(default=0, ge=0, le=4)
    age65_count: int = Field(default=0, ge=0, le=2)
    adjustments_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    other_income_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    capital_loss_carryover_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    nonrefundable_credits_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    refundable_credits_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    other_taxes_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    estimated_payments_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)
    other_payments_minor: int = Field(default=0, ge=0, le=MAX_TAX_AMOUNT_MINOR)

    @model_validator(mode="after")
    def _check_boxes(self) -> TaxProfileIn:
        max_boxes, max_seniors = (4, 2) if self.filing_status == "mfj" else (2, 1)
        if self.age65_or_blind_count > max_boxes:
            raise ValueError(
                f"age65_or_blind_count is at most {max_boxes} for filing status "
                f"{self.filing_status}"
            )
        if self.age65_count > max_seniors:
            raise ValueError(
                f"age65_count is at most {max_seniors} for filing status {self.filing_status}"
            )
        if self.age65_count > self.age65_or_blind_count:
            raise ValueError("age65_count cannot exceed age65_or_blind_count (it is a subset)")
        return self


class TaxProfile(TaxProfileIn):
    user_id: int
    tax_year: int
    updated_at: str


class TaxEstimateCalc(_Model):
    """The computation (the whole household's for a joint return), all `_minor` fields in cents and
    the rates as fractions (0.22 = 22%)."""

    filing_status: TaxFilingStatus
    # income
    wages_minor: int
    interest_minor: int
    ordinary_dividends_minor: int
    qualified_dividends_minor: int
    short_term_gain_minor: int
    long_term_gain_minor: int
    capital_loss_carryover_in_minor: int
    capital_result_counted_minor: int
    capital_loss_carryover_out_minor: int
    self_employment_minor: int
    other_income_minor: int
    profile_other_income_minor: int
    gross_income_minor: int
    # AGI, deduction, taxable income
    adjustments_minor: int
    se_tax_deduction_minor: int
    agi_minor: int
    deduction_mode: TaxDeductionMode
    #: Standard or itemized deduction, before the senior deduction.
    deduction_minor: int
    senior_deduction_minor: int
    taxable_income_minor: int
    preferential_income_minor: int
    # tax: income_tax - nonrefundable_credits + se_tax + net_investment_income_tax +
    # additional_medicare_tax + other_taxes = total_tax (never negative)
    income_tax_minor: int
    ordinary_tax_minor: int
    preferential_tax_minor: int
    #: The nonrefundable credits actually used (capped at the income tax).
    nonrefundable_credits_minor: int
    nonrefundable_credits_entered_minor: int
    se_tax_minor: int
    net_investment_income_tax_minor: int
    additional_medicare_tax_minor: int
    other_taxes_minor: int
    total_tax_minor: int
    # payments (federal withholding + estimated + other + refundable credits + Additional Medicare
    # Tax withheld + excess Social Security credit) and result (`result_minor` > 0 refund,
    # < 0 owed)
    federal_withheld_minor: int
    estimated_payments_minor: int
    other_payments_minor: int
    #: Refundable credits are payments (1040 lines 27-31), not negative tax.
    refundable_credits_minor: int
    additional_medicare_withheld_minor: int
    excess_social_security_credit_minor: int
    total_payments_minor: int
    result_minor: int
    refund_minor: int
    owed_minor: int
    effective_rate: float | None
    marginal_rate: float | None
    preferential_rate: float | None
    # W-2 box 12 memo items (never subtracted again)
    retirement_deferral_minor: int
    hsa_employer_minor: int


class TaxEstimateUser(_Model):
    """This user's part. For `scope` `individual` it equals the computation; for
    `household_allocation` `total_tax_minor` is an allocation of the household tax by `share` of
    gross income (an allocation, not a separate liability) and the payments are the user's own."""

    gross_income_minor: int
    share: float | None
    total_tax_minor: int
    payments_minor: int
    result_minor: int
    refund_minor: int
    owed_minor: int
    #: This user's part of the W-2 Medicare tax that is Additional Medicare Tax (already in FICA).
    additional_medicare_withheld_minor: int = 0


class TaxEstimateCrossCheck(_Model):
    """The latest Form 1040 for the year beside the estimate (never an input). Deltas are
    estimate minus return; `null` when the return did not print the figure."""

    return_filing_status: str | None
    return_total_tax_minor: int | None
    return_withheld_minor: int | None
    return_refund_minor: int | None
    return_owed_minor: int | None
    return_result_minor: int | None
    delta_total_tax_minor: int | None
    delta_withheld_minor: int | None
    delta_result_minor: int | None
    #: The estimate compared is the household's.
    household: bool


class TaxEstimateParameters(_Model):
    tax_year: int
    source: Literal["bundled", "cached", "override"]
    params_version: int
    sources: list[str]


class TaxEstimate(_Model):
    """`GET /api/tax/estimate`."""

    user_id: int
    tax_year: int
    scope: Literal["individual", "household_allocation"]
    household_user_ids: list[int]
    filing_status: TaxFilingStatus
    filing_status_source: Literal["profile", "return", "default"]
    calc: TaxEstimateCalc
    user: TaxEstimateUser
    cross_check: TaxEstimateCrossCheck | None
    #: The saved profile of the requested user, or `null` when defaults were used.
    profile: TaxProfile | None
    parameters: TaxEstimateParameters
    warnings: list[str]
    not_modeled: list[str]


class TaxParameterYear(_Model):
    """One year of `GET /api/tax/parameters` (cents)."""

    tax_year: int
    sources: list[str]
    standard_deduction_minor: dict[str, int]
    additional_standard_deduction_unmarried_minor: int
    additional_standard_deduction_married_minor: int
    capital_loss_limit_minor: int
    capital_loss_limit_mfs_minor: int
    ss_wage_base_minor: int
    elective_deferral_limit_minor: int
    niit_threshold_minor: dict[str, int]
    additional_medicare_threshold_minor: dict[str, int]
    #: 2025-2028 senior deduction per person (0 in other years), its phase-out rate in basis
    #: points and the income above which it phases out, per status that may claim it.
    senior_deduction_minor: int = 0
    senior_phaseout_rate_bp: int = 0
    senior_phaseout_threshold_minor: dict[str, int] = Field(default_factory=dict)


class TaxParametersStatus(_Model):
    """Which parameter file is in effect and the state of the opt-in yearly update check."""

    source: Literal["bundled", "cached", "override"]
    params_version: int
    bundled_version: int
    cached_version: int | None
    override_version: int | None
    #: Date (ISO, local) of the last update check, `null` if never; `last_check_outcome` is
    #: `updated`, `current`, `rejected`, `unreachable`, or `pending`.
    last_checked: str | None
    last_check_outcome: str | None
    #: `[tax] auto_update_params`: the opt-in, off by default.
    auto_update: bool


class TaxParameters(TaxParametersStatus):
    years: list[TaxParameterYear]


class TaxParametersRefreshResponse(_Model):
    """`POST /api/tax/parameters/refresh`: one update check, run now (an explicit action, so it
    does not need the opt-in)."""

    outcome: Literal["updated", "current", "rejected", "unreachable"]
    detail: str
    parameters: TaxParameters


class TaxIncomePayer(_Model):
    tax_year: int
    user_id: int
    payer_name: str | None
    form_type: TaxFormType
    category: IncomeCategoryEnum
    label: str
    amount_minor: int


# --- Net worth forecast ------------------------------------------------------------------------
#
# Rates are percent per year (3.0 = 3%); amounts are integer USD minor units. The projection itself
# is computed in the browser; these schemas carry history in and presets out.


class NetWorthHistoryPut(_Model):
    amount_minor: int = Field(gt=-MAX_AMOUNT_MINOR, lt=MAX_AMOUNT_MINOR)  # signed
    note: str | None = Field(default=None, max_length=_NOTE_MAX_LENGTH)


class NetWorthHistory(_Model):
    id: int
    user_id: int
    year: int
    amount_minor: int
    note: str | None
    created_at: str
    updated_at: str


#: "carried": nothing was recorded for the year, so an earlier balance is carried forward.
#: "mixed" only in the all-users view, when people's years came from different kinds (the choice is
#: made per person per year, then summed).
ForecastNetWorthSource = Literal["entries", "manual", "carried", "mixed"]


class ForecastHistoryRow(_Model):
    year: int
    #: Year-end net worth: from balance entries recorded in the year, else the manual value, else
    #: the balance carried forward from earlier entries.
    net_worth_minor: int | None
    source: ForecastNetWorthSource | None
    #: "YYYY-MM" of the entries-derived value (the year's last month with data; for a carried
    #: value, the month of the real entry it comes from).
    as_of: str | None
    #: The manual value for the year, even when entries override it (so the editor can show both).
    manual_net_worth_minor: int | None
    manual_note: str | None
    #: Gross income on the imported tax forms, and the year's headline total tax (federal + FICA +
    #: state; see `TaxHeadline`).
    income_minor: int | None
    #: The wages + self-employment part of `income_minor` (interest, dividends and gains excluded).
    earned_income_minor: int | None
    tax_minor: int | None
    income_basis: TaxIncomeBasis | None
    #: Net spending (purchases, fees, interest, refunds netted) and the months it covers.
    spending_minor: int | None
    spending_months: int
    #: income - tax - spending; only for a year with all three and 12 months of spending.
    savings_minor: int | None


class ForecastDefaults(_Model):
    """Values suggested from history; each is None when the data does not support it.

    Each level is as of its own `*_year`, and the years can differ; the screen rebases income and
    spending to the chosen starting year."""

    net_worth_minor: int | None
    net_worth_year: int | None
    #: Earned income (wages + self-employment) in `income_year`; investment income is left out
    #: because the forecast's investment return already models it.
    annual_income_minor: int | None
    income_year: int | None
    #: All-in tax / earned income in `income_year`, capped at 100.
    effective_tax_rate_pct: float | None
    annual_spending_minor: int | None
    spending_year: int | None
    #: True when `annual_spending_minor` is a partial year scaled up to 12 months.
    spending_annualized: bool
    #: Compound annual growth over the years with data; None with fewer than two such years.
    income_growth_pct: float | None
    spending_growth_pct: float | None
    #: True when the observed rate was outside the allowed -50..50 range (typically a partial
    #: first year) and `*_growth_pct` was clamped to it.
    income_growth_clamped: bool
    spending_growth_clamped: bool
    savings_rate_pct: float | None


class ForecastBaseline(_Model):
    currency: Literal["USD"]
    user_id: int | None
    history: list[ForecastHistoryRow]
    defaults: ForecastDefaults


class ForecastEvent(_Model):
    year: int = Field(ge=MIN_YEAR, le=MAX_YEAR)
    amount_minor: int = Field(gt=-MAX_AMOUNT_MINOR, lt=MAX_AMOUNT_MINOR)  # signed
    label: str = Field(default="", max_length=100)


class ForecastParams(_Model):
    """One forecast scenario. A None starting value means "use the figure derived from history"."""

    #: Years after the starting year to project; None (built-ins) keeps the user's current target.
    horizon_years: int | None = Field(default=None, ge=1, le=MAX_HORIZON_YEARS)
    #: The starting year the horizon is relative to; None = the latest year with data.
    base_year: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    start_net_worth_minor: int | None = Field(
        default=None, gt=-MAX_AMOUNT_MINOR, lt=MAX_AMOUNT_MINOR
    )
    annual_income_minor: int | None = Field(default=None, ge=0, lt=MAX_AMOUNT_MINOR)
    income_growth_pct: float = Field(default=3.0, ge=-50.0, le=50.0)
    tax_rate_pct: float | None = Field(default=None, ge=0.0, le=100.0)
    annual_spending_minor: int | None = Field(default=None, ge=0, lt=MAX_AMOUNT_MINOR)
    spending_growth_pct: float = Field(default=2.5, ge=-50.0, le=50.0)
    #: Fixed amount added to spending every year, on top of the percentage growth.
    spending_change_minor: int = Field(default=0, gt=-MAX_AMOUNT_MINOR, lt=MAX_AMOUNT_MINOR)
    return_pct: float = Field(default=6.0, ge=-20.0, le=30.0)
    inflation_pct: float = Field(default=2.5, ge=-10.0, le=30.0)
    #: Last year with salary; later years earn `retirement_income_minor` instead. None = never.
    retirement_year: int | None = Field(default=None, ge=MIN_YEAR, le=MAX_YEAR)
    #: Pre-tax annual income in retirement, in today's dollars (grows with inflation).
    retirement_income_minor: int = Field(default=0, ge=0, lt=MAX_AMOUNT_MINOR)
    events: list[ForecastEvent] = Field(default_factory=list, max_length=MAX_EVENTS)


class BuiltinForecastPreset(_Model):
    key: str
    name: str
    description: str
    params: ForecastParams


class ForecastPreset(_Model):
    id: int
    #: None = shared with every user.
    user_id: int | None
    name: str
    params: ForecastParams
    created_at: str
    updated_at: str


class InvalidForecastPreset(_Model):
    """A saved preset whose stored parameters no longer validate: only identity is returned."""

    id: int
    user_id: int | None
    name: str


class ForecastPresetList(_Model):
    builtin: list[BuiltinForecastPreset]
    saved: list[ForecastPreset]
    #: Saved presets that cannot be loaded; they can still be renamed, repaired or deleted.
    invalid: list[InvalidForecastPreset] = Field(default_factory=list)


class ForecastPresetCreate(_Model):
    name: str = Field(min_length=1, max_length=60)
    #: None saves a shared preset (also what the all-users view does).
    user_id: int | None = None
    params: ForecastParams


class ForecastPresetUpdate(_Model):
    """Rename and/or replace the parameters; omitted fields are left as they are."""

    name: str | None = Field(default=None, min_length=1, max_length=60)
    params: ForecastParams | None = None


# --- Folder imports (`watch/`) --------------------------------------------------------------------

WatchOutcome = Literal["imported", "known", "failed", "ignored"]


class WatchScanSummary(_Model):
    started_at: str
    finished_at: str | None = None
    scanned: int
    imported: int
    known: int
    failed: int
    ignored: int
    #: New files that were still settling, or not whole PDFs yet.
    waiting: int
    #: Another scan (another process) held the lock: nothing was done.
    busy: bool = False
    error: str | None = None


class WatchStatus(_Model):
    enabled: bool
    interval_seconds: int
    documents_dir: str
    #: A scan is running right now.
    running: bool
    next_scan_at: str | None = None
    last_scan: WatchScanSummary | None = None
    #: Why scans cannot run (database not migrated, folder missing...), if so.
    problem: str | None = None


class WatchScanStarted(_Model):
    #: False when a scan was already running (nothing new was started).
    started: bool
    status: WatchStatus


class WatchFile(_Model):
    id: int
    path: str
    outcome: WatchOutcome
    code: str | None = None
    message: str | None = None
    hint: str | None = None
    kind: Literal["statement", "tax"] | None = None
    #: The person folder as named on disk.
    folder_person: str | None = None
    user_id: int | None = None
    statement_id: int | None = None
    tax_document_id: int | None = None
    warnings: list[str] = Field(default_factory=list)
    first_seen: str
    last_seen: str


class WatchFileList(_Model):
    items: list[WatchFile]
    total: int
