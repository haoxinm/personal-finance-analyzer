"""SQLAlchemy 2.x declarative models for every table in §3.2 (P0-3).

Dates and timestamps are stored as ISO-8601 **text** (SQLite convention: sortable, no timezone
ambiguity). All timestamps are UTC; all dates are naive (no time component).

The FK cycle between `issuers.default_spec_id` and `layout_specs.issuer_id` (A29) is declared with
`use_alter=True` on the `issuers.default_spec_id` foreign key, so Alembic can create the two
tables and add that constraint in a second step.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from personal_finance_analyzer.core.net_worth import BALANCE_CATEGORIES
from personal_finance_analyzer.core.tax_types import (
    SIGNED_INCOME_CATEGORIES,
    FormType,
    IncomeCategory,
    TaxCategory,
)


def utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string, for `created_at`/`updated_at` defaults."""
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pii_aliases: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # A11: JSON list
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class Issuer(Base):
    """A24: known issuers. `name` and `match_terms` are LOCAL ONLY (I1b) — never egressed."""

    __tablename__ = "issuers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    match_terms: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON list
    default_spec_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("layout_specs.id", use_alter=True, name="fk_issuers_default_spec_id"),
        nullable=True,
    )
    auto_confirm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    last_used_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("user_id", "issuer_id", "mask", "account_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    issuer_id: Mapped[int] = mapped_column(Integer, ForeignKey("issuers.id"), nullable=False)
    account_type: Mapped[str] = mapped_column(String, nullable=False)  # credit|checking|savings
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    # A29: NOT NULL DEFAULT '' -- SQLite treats NULLs as distinct in UNIQUE, which would
    # re-create maskless accounts on every import. LOCAL ONLY: never egressed, never exported.
    mask: Mapped[str] = mapped_column(String, nullable=False, default="")
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class LayoutSpec(Base):
    """A19/A21: named, versioned, immutable extractor specs. An edit inserts version+1."""

    __tablename__ = "layout_specs"
    __table_args__ = (
        UniqueConstraint("name", "version"),
        UniqueConstraint("parser_id"),
        Index("ix_specs_issuer", "issuer_id", "account_type", "version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_id: Mapped[str] = mapped_column(String, nullable=False)
    issuer_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("issuers.id"), nullable=True
    )  # LOCAL ONLY (I1b)
    account_type: Mapped[str | None] = mapped_column(String, nullable=True)
    spec_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)  # llm_proposed|pasted|user_authored
    approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    llm_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("llm_runs.id"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class Statement(Base):
    __tablename__ = "statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("accounts.id"), nullable=True
    )
    # The user who uploaded this statement (D4), set by `ingest.pipeline.propose_import` at
    # Phase 1 so Phase 2 (a separate process/request, possibly after a restart) can resolve or
    # create the account without depending on any in-process state. No `ondelete` (matches
    # every other `user_id` foreign key in this file: `accounts.user_id`, `transactions.user_id`,
    # `rules.user_id` — none cascades or nullifies on a user delete).
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    file_sha256: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str | None] = mapped_column(String, nullable=True)
    period_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    period_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON list of the 1-based pages to parse/send to the LLM; NULL = all pages.
    selected_pages: Mapped[str | None] = mapped_column(Text, nullable=True)
    issuer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("issuers.id"), nullable=True)
    layout_spec_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("layout_specs.id"), nullable=True
    )
    parser_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String, nullable=True)
    detect_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # A18
    shape_warnings: Mapped[str | None] = mapped_column(Text, nullable=True)  # A18: JSON list
    status: Mapped[str] = mapped_column(String, nullable=False)
    # pending|awaiting_extractor|parsed|no_text_layer|unsupported_layout|error
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    txn_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_total_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stated_total_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    opening_balance_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)  # A25
    closing_balance_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)  # A25
    ingested_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)

    transactions: Mapped[list[Transaction]] = relationship(
        "Transaction",
        cascade="all, delete-orphan",
        passive_deletes=True,
        back_populates="statement",
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class Subcategory(Base):
    __tablename__ = "subcategories"
    __table_args__ = (UniqueConstraint("category_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    key: Mapped[str] = mapped_column(String, nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    is_dynamic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    # active | pending_approval | merged
    merged_into: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("subcategories.id"), nullable=True
    )


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        UniqueConstraint("account_id", "dedupe_hash"),
        Index("ix_txn_user_date", "user_id", "posted_date"),
        Index("ix_txn_cat", "category_id", "subcategory_id"),
        Index("ix_txn_merchant", "merchant_key"),
        Index("ix_txn_review", "needs_review", sqlite_where=text("needs_review = 1")),
        Index("ix_txn_statement", "statement_id"),  # A10
        Index("ix_txn_account_date", "account_id", "posted_date"),  # A10
        CheckConstraint(
            "kind IN ('purchase','refund','payment','transfer','fee','interest','adjustment')",
            name="ck_txn_kind",
        ),
        CheckConstraint(
            "(category_id IS NULL) = (subcategory_id IS NULL)",
            name="ck_txn_category_subcategory_both_or_neither",
        ),  # I4
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("statements.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("accounts.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)

    posted_date: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    description_raw: Mapped[str] = mapped_column(Text, nullable=False)  # NEVER egressed
    description_clean: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # ONLY egress-eligible field
    merchant_key: Mapped[str] = mapped_column(Text, nullable=False)
    merchant_canonical: Mapped[str | None] = mapped_column(Text, nullable=True)
    issuer_category: Mapped[str | None] = mapped_column(Text, nullable=True)  # A16, LOCAL ONLY

    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)  # signed; positive = outflow
    currency: Mapped[str] = mapped_column(String, nullable=False)
    fx_amount_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fx_currency: Mapped[str | None] = mapped_column(String, nullable=True)
    fx_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    kind: Mapped[str] = mapped_column(String, nullable=False)
    is_spend: Mapped[bool] = mapped_column(
        Boolean, nullable=False
    )  # kind IN (purchase,fee,interest)

    category_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("categories.id"), nullable=True
    )
    subcategory_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("subcategories.id"), nullable=True
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    classified_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # cache|user_rule|builtin_rule|issuer|llm|user|kind
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    dedupe_hash: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)

    statement: Mapped[Statement] = relationship("Statement", back_populates="transactions")


class MerchantMap(Base):
    __tablename__ = "merchant_map"

    merchant_key: Mapped[str] = mapped_column(Text, primary_key=True)
    merchant_canonical: Mapped[str | None] = mapped_column(Text, nullable=True)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    subcategory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("subcategories.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String, nullable=False)  # llm|user|builtin|issuer (A29)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True)
    match_type: Mapped[str] = mapped_column(String, nullable=False)  # exact|contains|regex
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    category_id: Mapped[int] = mapped_column(Integer, ForeignKey("categories.id"), nullable=False)
    subcategory_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("subcategories.id"), nullable=False
    )
    kind_override: Mapped[str | None] = mapped_column(String, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(String, nullable=False)  # builtin|user
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class LlmRun(Base):
    __tablename__ = "llm_runs"
    __table_args__ = (Index("ix_llm_runs_group", "group_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[str] = mapped_column(String, nullable=False)  # A2: uuid4 per user action
    kind: Mapped[str] = mapped_column(String, nullable=False, default="classify")
    # classify | test | layout_spec (Phase 5)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String, nullable=False)
    schema_mode: Mapped[str] = mapped_column(
        String, nullable=False
    )  # json_schema|json_object|text_fallback
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # ok|partial|error
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_sha256: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    category_key: Mapped[str] = mapped_column(String, nullable=False)
    subcategory_key: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str] = mapped_column(String, nullable=False)
    llm_run_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("llm_runs.id"), nullable=True
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4
    kind: Mapped[str] = mapped_column(String, nullable=False)  # import | classify
    status: Mapped[str] = mapped_column(String, nullable=False)
    # queued | running | done | error | cancelled
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_run_group_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)


# --- Income and tax analysis (tax forms). Amount/sign rules: `core/tax_types.py` (D6). ---


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


_FORM_TYPES: tuple[str, ...] = get_args(FormType)
_INCOME_CATEGORIES: tuple[str, ...] = get_args(IncomeCategory)
_TAX_CATEGORIES: tuple[str, ...] = get_args(TaxCategory)
_SIGNED_INCOME: tuple[str, ...] = tuple(sorted(SIGNED_INCOME_CATEGORIES))


class TaxDocument(Base):
    """One uploaded tax file. `original_name` is LOCAL ONLY; `error_detail` is generic text,
    never document content."""

    __tablename__ = "tax_documents"
    __table_args__ = (
        # Dedupe is per user: the same PDF may be imported for two users (e.g. a joint W-2 copy).
        UniqueConstraint("user_id", "file_sha256"),
        Index("ix_taxdoc_user_year", "user_id", "tax_year"),
        CheckConstraint("status IN ('parsed','error','unsupported')", name="ck_taxdoc_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String, nullable=False)
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    stored_path: Mapped[str | None] = mapped_column(String, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tax_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)  # parsed | error | unsupported
    parser_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String, nullable=True)
    detect_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    warnings: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list[str]
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingested_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)

    forms: Mapped[list[TaxForm]] = relationship(
        "TaxForm",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class TaxForm(Base):
    """One form instance inside a tax document. `payer_name` and `account_last4` are LOCAL ONLY."""

    __tablename__ = "tax_forms"
    __table_args__ = (
        UniqueConstraint("user_id", "tax_year", "fingerprint"),
        Index("ix_taxform_user_year", "user_id", "tax_year"),
        Index("ix_taxform_document", "document_id"),
        CheckConstraint("role IN ('source','return')", name="ck_taxform_role"),
        CheckConstraint(f"form_type IN ({_sql_in(_FORM_TYPES)})", name="ck_taxform_type"),
        CheckConstraint(
            "payer_tin_last4 IS NULL OR length(payer_tin_last4) = 4", name="ck_taxform_tin_last4"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tax_documents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    form_type: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # source | return
    payer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    payer_tin_last4: Mapped[str | None] = mapped_column(String, nullable=True)
    account_last4: Mapped[str | None] = mapped_column(String, nullable=True)
    is_corrected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    filing_status: Mapped[str | None] = mapped_column(String, nullable=True)
    agi_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    taxable_income_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    #: How the figures got here: parser | llm | manual (validated in `ingest/tax/edit.py`, no DB
    #: CHECK: see migration 0006).
    extraction_method: Mapped[str] = mapped_column(
        String, nullable=False, default="parser", server_default=text("'parser'")
    )
    is_user_edited: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)

    document: Mapped[TaxDocument] = relationship("TaxDocument", back_populates="forms")
    income_items: Mapped[list[IncomeItem]] = relationship(
        "IncomeItem", cascade="all, delete-orphan", passive_deletes=True
    )
    tax_items: Mapped[list[TaxItem]] = relationship(
        "TaxItem", cascade="all, delete-orphan", passive_deletes=True
    )


class IncomeItem(Base):
    """One income line. `user_id`/`tax_year` are denormalized for the analytics index."""

    __tablename__ = "income_items"
    __table_args__ = (
        Index("ix_income_form", "form_id"),
        Index("ix_income_user_year_cat", "user_id", "tax_year", "category"),
        CheckConstraint(f"category IN ({_sql_in(_INCOME_CATEGORIES)})", name="ck_income_category"),
        CheckConstraint(
            f"amount_minor >= 0 OR category IN ({_sql_in(_SIGNED_INCOME)})", name="ck_income_sign"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tax_forms.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    source_form: Mapped[str] = mapped_column(String, nullable=False)
    box: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    is_memo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    origin: Mapped[str] = mapped_column(
        String, nullable=False, default="parser", server_default=text("'parser'")
    )  # parser | llm | manual


class TaxItem(Base):
    """One tax line (withheld, owed, paid). `amount_minor` is a non-negative magnitude."""

    __tablename__ = "tax_items"
    __table_args__ = (
        Index("ix_taxitem_form", "form_id"),
        Index("ix_taxitem_user_year_cat", "user_id", "tax_year", "category"),
        CheckConstraint(f"category IN ({_sql_in(_TAX_CATEGORIES)})", name="ck_taxitem_category"),
        CheckConstraint("amount_minor >= 0", name="ck_taxitem_sign"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tax_forms.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    source_form: Mapped[str] = mapped_column(String, nullable=False)
    box: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL = federal/FICA
    is_estimate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    origin: Mapped[str] = mapped_column(
        String, nullable=False, default="parser", server_default=text("'parser'")
    )  # parser | llm | manual


class BalanceEntry(Base):
    """One month-end balance for one account (net worth). LOCAL ONLY: never sent to an LLM and
    never part of `/api/export`. `balance_minor` is a non-negative magnitude (credit card = amount
    owed)."""

    __tablename__ = "balance_entries"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "category", "account_name", "year", "month", name="uq_balance_entry_period"
        ),
        Index("ix_balance_user_period", "user_id", "year", "month"),
        CheckConstraint(f"category IN ({_sql_in(BALANCE_CATEGORIES)})", name="ck_balance_category"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_balance_month"),
        CheckConstraint("year BETWEEN 1900 AND 2200", name="ck_balance_year"),
        CheckConstraint("balance_minor >= 0", name="ck_balance_nonneg"),
        CheckConstraint("length(trim(account_name)) > 0", name="ck_balance_account_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    account_name: Mapped[str] = mapped_column(String, nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="USD")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class NetWorthHistory(Base):
    """A user-entered year-end net worth for a past year that has no balance entries (net worth
    forecast). Signed: net worth can be negative. LOCAL ONLY, like `balance_entries`: never sent
    to an LLM and never part of `/api/export`."""

    __tablename__ = "net_worth_history"
    __table_args__ = (
        UniqueConstraint("user_id", "year", name="uq_net_worth_history_user_year"),
        CheckConstraint("year BETWEEN 1900 AND 2200", name="ck_nw_history_year"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)  # signed, USD
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


class ForecastPreset(Base):
    """A saved set of net worth forecast parameters. `user_id` NULL = shared with every user.
    `params` is a JSON object validated by `api/schemas.ForecastParams` on every write. SQLite
    treats NULLs as distinct in a UNIQUE constraint, so shared names get their own partial index."""

    __tablename__ = "forecast_presets"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_forecast_preset_user_name"),
        Index(
            "uq_forecast_preset_shared_name",
            "name",
            unique=True,
            sqlite_where=text("user_id IS NULL"),
        ),
        CheckConstraint("length(trim(name)) > 0", name="ck_forecast_preset_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    params: Mapped[str] = mapped_column(Text, nullable=False)  # JSON object
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utcnow_iso)
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )


#: Filing statuses a profile may hold (the 1040 parser's vocabulary).
TAX_FILING_STATUSES: tuple[str, ...] = ("single", "mfj", "mfs", "hoh", "qss")
_PROFILE_AMOUNTS: tuple[str, ...] = (
    "itemized_deductions_minor",
    "adjustments_minor",
    "other_income_minor",
    "capital_loss_carryover_minor",
    "nonrefundable_credits_minor",
    "refundable_credits_minor",
    "other_taxes_minor",
    "estimated_payments_minor",
    "other_payments_minor",
)


def _amount_column() -> Mapped[int]:
    return mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class TaxProfile(Base):
    """Manual inputs for one user's federal tax estimate for one year (`analytics/tax_estimate.py`).

    Everything the source forms cannot say: filing status, spouse link, deductions, credits,
    other taxes and payments. All amounts are non-negative integer cents. `joint_with_user_id` is
    the spouse for a married-filing-jointly profile (both spouses carry a profile that points at
    the other). LOCAL ONLY, like the rest of the tax data.
    """

    __tablename__ = "tax_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "tax_year", name="uq_tax_profile_user_year"),
        CheckConstraint(
            f"filing_status IN ({_sql_in(TAX_FILING_STATUSES)})", name="ck_taxprofile_status"
        ),
        CheckConstraint(
            "deduction_mode IN ('standard','itemized')", name="ck_taxprofile_deduction_mode"
        ),
        CheckConstraint("age65_or_blind_count BETWEEN 0 AND 4", name="ck_taxprofile_aged_blind"),
        CheckConstraint("age65_count BETWEEN 0 AND 2", name="ck_taxprofile_age65"),
        CheckConstraint("tax_year BETWEEN 1990 AND 2100", name="ck_taxprofile_year"),
        CheckConstraint(
            " AND ".join(f"{c} >= 0" for c in _PROFILE_AMOUNTS), name="ck_taxprofile_nonneg"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False)
    filing_status: Mapped[str] = mapped_column(String, nullable=False)
    joint_with_user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    deduction_mode: Mapped[str] = mapped_column(
        String, nullable=False, default="standard", server_default=text("'standard'")
    )
    itemized_deductions_minor: Mapped[int] = _amount_column()
    #: Boxes checked for the additional standard deduction (age 65+ and blind, per person).
    age65_or_blind_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    #: Of those, how many are people aged 65 or over (the 2025-2028 senior deduction).
    age65_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    adjustments_minor: Mapped[int] = _amount_column()
    other_income_minor: Mapped[int] = _amount_column()
    capital_loss_carryover_minor: Mapped[int] = _amount_column()
    nonrefundable_credits_minor: Mapped[int] = _amount_column()
    refundable_credits_minor: Mapped[int] = _amount_column()
    other_taxes_minor: Mapped[int] = _amount_column()
    estimated_payments_minor: Mapped[int] = _amount_column()
    other_payments_minor: Mapped[int] = _amount_column()
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=utcnow_iso, onupdate=utcnow_iso
    )
