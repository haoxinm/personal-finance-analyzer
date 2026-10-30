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

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from spend_analyzer.core.net_worth import BalanceCategory


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


# --- Transactions ----------------------------------------------------------------------------------


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


class TransactionList(_Model):
    items: list[Transaction]
    total: int


class TransactionIds(_Model):
    """`GET /api/transactions/ids`: every id matching the filters (not one page of them)."""

    ids: list[int]
    total: int


class TransactionPatch(_Model):
    category_key: str | None = None
    subcategory_key: str | None = None
    kind: Kind | None = None
    notes: str | None = None
    create_rule: bool = False


class TransactionSnapshot(_Model):
    """One transaction's classification state, as `POST /review/accept` captured it before the
    change; sent back unchanged to `POST /review/undo`."""

    transaction_id: int
    category_id: int | None
    subcategory_id: int | None
    confidence: float | None
    classified_by: str | None
    needs_review: bool
    kind: str
    is_spend: bool
    merchant_canonical: str | None


class MerchantMapSnapshot(_Model):
    merchant_key: str
    existed: bool
    merchant_canonical: str | None = None
    category_id: int | None = None
    subcategory_id: int | None = None
    source: str | None = None
    confidence: float | None = None
    hit_count: int = 0


class RuleSnapshot(_Model):
    rule_id: int
    existed: bool
    category_id: int | None = None
    subcategory_id: int | None = None
    kind_override: str | None = None
    enabled: bool = True


class AcceptUndo(_Model):
    """Opaque undo token for one Review accept (everything the accept changed)."""

    transactions: list[TransactionSnapshot]
    merchant_map: MerchantMapSnapshot | None = None
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


class ReviewAcceptResponse(_Model):
    transaction: Transaction
    #: The `similar_ids` actually applied (its length is the "Applied to N more" count).
    similar_ids: list[int]
    undo: AcceptUndo


class ReviewUndoResponse(_Model):
    restored: int


class BulkUpdateRequest(_Model):
    transaction_ids: list[int]
    category_key: str | None = None
    subcategory_key: str | None = None
    kind: Kind | None = None


class BulkUpdateResponse(_Model):
    updated: int


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


class Settings(_Model):
    llm: LlmSettings
    privacy: PrivacySettings
    ingest: IngestSettings
    server: ServerSettings


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
    """A memo line (not part of total income): `qualified_dividends` or `tax_exempt`."""

    category: str
    label: str
    amount_minor: int


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


class TaxIncomePayer(_Model):
    tax_year: int
    user_id: int
    payer_name: str | None
    form_type: TaxFormType
    category: IncomeCategoryEnum
    label: str
    amount_minor: int
