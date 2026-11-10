"""Domain types for income and tax analysis from tax forms (W-2, 1099 family, 1040).

Pure data and vocabulary: no I/O, no database, no network, and no LLM import. Tax data reaches an
egress seam only through the opt-in `TaxFallbackExtractor` (see below), never by default.

**Sign and unit convention (D6):** every amount is an integer count of minor currency units
(cents), USD only in v1. `ParsedIncomeItem.amount_minor` is the amount as received: positive is
income, and a net capital *loss* is negative (only `short_term_capital_gain` and
`long_term_capital_gain` may be negative). `ParsedTaxItem.amount_minor` is a non-negative
magnitude of tax withheld, owed or paid; for the "result" group (`refund`, `amount_owed`) the
category carries the direction and the amount stays non-negative. 1099-B proceeds and cost basis
are not stored, only the net gain or loss per holding term.

**Source versus return (D4):** W-2 and 1099 forms are "source" documents; a 1040 (or a state
return) is a "return" document. They describe the same income, so summing both double counts.
Aggregation takes income and withholding from source forms, and liability, refund, owed and the
preferential-versus-ordinary split from the return form. With no source forms for a year, income
falls back to the return's line items.

**Fallback (opt-in):** parsed items and forms carry their provenance (`origin`, `extraction_method`;
default ``"parser"``). Only the optional LLM fallback produces ``"llm"``; the user's manual edits
produce ``"manual"`` in the database layer.

**Privacy (D3):** a full SSN, ITIN or EIN never appears in these types; only the last four digits
(`payer_tin_last4`, `account_last4`). Recipient name and address are never captured. `payer_name`
is local only, like `issuers.name`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, Protocol, get_args, runtime_checkable

from personal_finance_analyzer.core.types import ExtractedDoc

FormType = Literal[
    "w2",
    "1099-int",
    "1099-div",
    "1099-b",
    "1099-nec",
    "1099-misc",
    "1099-consolidated",
    "1040",
    "state-return",
]

#: Whether each form type is a source document or a return document (D4).
FORM_ROLE: Final[Mapping[FormType, Literal["source", "return"]]] = MappingProxyType(
    {
        "w2": "source",
        "1099-int": "source",
        "1099-div": "source",
        "1099-b": "source",
        "1099-nec": "source",
        "1099-misc": "source",
        "1099-consolidated": "source",
        "1040": "return",
        "state-return": "return",
    }
)

IncomeCategory = Literal[
    "salary_wages",
    "interest",
    "ordinary_dividends",
    "qualified_dividends",  # a subset of ordinary dividends: memo
    "short_term_capital_gain",
    "long_term_capital_gain",
    "self_employment",
    "other",
    "retirement_deferral",  # W-2 box 12 codes D, E, F, G, S: already excluded from box 1, memo
    "hsa_employer",  # W-2 box 12 code W: employer HSA contributions, memo
]

#: Display order; the single source of truth for income categories.
INCOME_CATEGORIES: Final[tuple[IncomeCategory, ...]] = get_args(IncomeCategory)

#: Categories excluded from total income (D5). Any item parsed with `is_memo=True` is also memo.
MEMO_INCOME_CATEGORIES: Final[frozenset[IncomeCategory]] = frozenset(
    {"qualified_dividends", "retirement_deferral", "hsa_employer"}
)

#: Categories that may carry a negative amount (net capital loss).
SIGNED_INCOME_CATEGORIES: Final[frozenset[IncomeCategory]] = frozenset(
    {"short_term_capital_gain", "long_term_capital_gain"}
)

INCOME_CATEGORY_LABELS: Final[Mapping[IncomeCategory, str]] = MappingProxyType(
    {
        "salary_wages": "Wages and salary",
        "interest": "Interest",
        "ordinary_dividends": "Ordinary dividends",
        "qualified_dividends": "Qualified dividends (memo)",
        "short_term_capital_gain": "Short-term capital gain",
        "long_term_capital_gain": "Long-term capital gain",
        "self_employment": "Self-employment income",
        "other": "Other income",
        "retirement_deferral": "Retirement plan deferrals, W-2 box 12 (memo)",
        "hsa_employer": "Employer HSA contributions, W-2 box 12 (memo)",
    }
)

TaxCategory = Literal[
    # group "withholding"
    "federal_income_tax_withheld",
    "state_income_tax_withheld",
    "local_income_tax_withheld",
    # group "fica"
    "social_security_tax",
    "medicare_tax",
    # group "liability" (from a 1040 or state return)
    "total_federal_tax",
    "federal_tax_ordinary_est",
    "federal_tax_preferential_est",
    "net_investment_income_tax",
    "additional_medicare_tax",
    "self_employment_tax",
    "state_income_tax_liability",
    # group "payment"
    "estimated_tax_payments",
    "total_payments",
    # group "result"
    "refund",
    "amount_owed",
]

TAX_CATEGORIES: Final[tuple[TaxCategory, ...]] = get_args(TaxCategory)

TAX_GROUPS: Final[Mapping[str, tuple[TaxCategory, ...]]] = MappingProxyType(
    {
        "withholding": (
            "federal_income_tax_withheld",
            "state_income_tax_withheld",
            "local_income_tax_withheld",
        ),
        "fica": ("social_security_tax", "medicare_tax"),
        "liability": (
            "total_federal_tax",
            "federal_tax_ordinary_est",
            "federal_tax_preferential_est",
            "net_investment_income_tax",
            "additional_medicare_tax",
            "self_employment_tax",
            "state_income_tax_liability",
        ),
        "payment": ("estimated_tax_payments", "total_payments"),
        "result": ("refund", "amount_owed"),
    }
)

TAX_CATEGORY_LABELS: Final[Mapping[TaxCategory, str]] = MappingProxyType(
    {
        "federal_income_tax_withheld": "Federal income tax withheld",
        "state_income_tax_withheld": "State income tax withheld",
        "local_income_tax_withheld": "Local income tax withheld",
        "social_security_tax": "Social Security tax",
        "medicare_tax": "Medicare tax",
        "total_federal_tax": "Total federal tax",
        "federal_tax_ordinary_est": "Federal tax on ordinary income (est.)",
        "federal_tax_preferential_est": "Federal tax on dividends and gains (est.)",
        "net_investment_income_tax": "Net investment income tax",
        "additional_medicare_tax": "Additional Medicare tax",
        "self_employment_tax": "Self-employment tax",
        "state_income_tax_liability": "State income tax",
        "estimated_tax_payments": "Estimated tax payments",
        "total_payments": "Total payments",
        "refund": "Refund",
        "amount_owed": "Amount owed",
    }
)


#: Where a stored figure came from: a deterministic parser, the opt-in LLM fallback, or the user.
TaxOrigin = Literal["parser", "llm", "manual"]


@dataclass(frozen=True, slots=True)
class ParsedIncomeItem:
    """One income line from a form. `amount_minor` follows D6 (negative only for capital loss)."""

    category: IncomeCategory
    amount_minor: int
    source_form: str  # as printed: "W-2", "1099-INT", "1040", ...
    box: str | None  # display only, e.g. "1", "1a", "line 1a"
    label: str | None  # short label from our own table, never scraped text
    is_memo: bool = False
    origin: TaxOrigin = "parser"


@dataclass(frozen=True, slots=True)
class ParsedTaxItem:
    """One tax line from a form. `amount_minor` is a non-negative magnitude (D6)."""

    category: TaxCategory
    amount_minor: int
    source_form: str
    box: str | None
    label: str | None
    jurisdiction: str | None = None  # None = federal/FICA; "CA" for a state; locality (<=32 chars)
    is_estimate: bool = False
    origin: TaxOrigin = "parser"


@dataclass(frozen=True, slots=True)
class ParsedTaxForm:
    """One form instance. Carries only the last four digits of any taxpayer identifier."""

    form_type: FormType
    tax_year: int
    payer_name: str | None  # LOCAL ONLY; <=120 chars; never SSN-shaped
    payer_tin_last4: str | None  # exactly four digits, or None
    account_last4: str | None  # 1099 account number last four, else None
    is_corrected: bool
    filing_status: str | None  # 1040 only: single, mfj, mfs, hoh, qss
    agi_minor: int | None  # 1040 only
    taxable_income_minor: int | None  # 1040 only
    income: tuple[ParsedIncomeItem, ...]
    taxes: tuple[ParsedTaxItem, ...]
    warnings: tuple[str, ...] = ()  # never raw document text or digit runs of 6 or more
    extraction_method: TaxOrigin = "parser"


#: ``(original page number, x0, top, x1, bottom)`` in PDF points (origin top-left).
PageBox = tuple[int, float, float, float, float]


@dataclass(frozen=True, slots=True)
class UnparsedRegion:
    """A part of a file that looks like a tax form (or section) but yielded no figures.

    Structured so that a later fallback (manual entry, an optional AI pass) can address exactly
    the pages involved. `kind` is a form type or section name such as ``"1099-INT"`` or
    ``"unknown"``; `pages` are 1-based page numbers of the original file; `reason` is fixed
    generic text. Never holds document text.

    `boxes` (optional) are the rectangles of those pages that the region occupies, so a fallback
    can send only that part of a page (a page with two forms side by side where only one failed).
    A page of `pages` without a box is the whole page.
    """

    kind: str
    pages: tuple[int, ...]
    reason: str
    boxes: tuple[PageBox, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedTaxDocument:
    """All forms found in one file. `tax_year` is agreed across forms (mismatch is a ParserError).

    `unparsed` lists the regions that looked like forms but could not be read (partial success:
    the readable forms are still returned, with a warning per skipped region).
    """

    tax_year: int
    forms: tuple[ParsedTaxForm, ...]  # at least one
    warnings: tuple[str, ...] = ()
    unparsed: tuple[UnparsedRegion, ...] = ()


@dataclass(frozen=True, slots=True)
class FallbackResult:
    """What an opt-in fallback extractor returns: validated forms (``extraction_method="llm"``,
    items ``origin="llm"``) and fixed-text warnings. Never raises for a model problem."""

    forms: tuple[ParsedTaxForm, ...] = ()
    warnings: tuple[str, ...] = ()
    requests_sent: int = 0


class TaxFallbackExtractor(Protocol):
    """An optional extractor for the parts of a tax file the deterministic parsers could not read.

    Defined here (no LLM import) so the tax package can accept one without depending on any
    network code; the only implementation lives in ``llm/tax_parser.py`` and is created by
    ``api/services/tax_llm.py``. Deterministic results always win: callers pass only regions the
    parsers failed to read.
    """

    id: str
    version: str
    #: Requests sent so far by the latest `extract` call, kept up to date while it runs so a caller
    #: can still report them when `extract` raises.
    requests_sent: int

    def preview(self, doc: ExtractedDoc, regions: Sequence[UnparsedRegion]) -> list[str]:
        """Exactly the redacted texts that `extract` would send. No network."""
        ...

    def extract(
        self, doc: ExtractedDoc, regions: Sequence[UnparsedRegion], *, tax_year: int
    ) -> FallbackResult:
        """Read `regions` of `doc`. `tax_year` is the deterministic year (never from the model)."""
        ...


@runtime_checkable
class TaxFormParser(Protocol):
    """A tax-form parser. Deliberately has no `account_type`, so it can never satisfy
    `StatementParser` and be mis-registered as a bank parser."""

    id: str
    version: str
    form_types: tuple[FormType, ...]

    def detect(self, doc: ExtractedDoc) -> float:
        """Structure-based confidence in [0, 1]. Never raises."""
        ...

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        """Parse `doc`; `tax_year` is a user override. Raises `ParserError` on failure."""
        ...
