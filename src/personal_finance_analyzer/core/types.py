"""Core data types shared by every ingest and classification module (§3.1).

Frozen and slotted: these values are immutable once constructed, which matters because a
`ParsedStatement` is produced by a parser and then consumed, unmodified, by the ingest pipeline,
reconciliation, and tests.

**Sign convention (I5):** `RawTransaction.amount_minor` is a *signed* integer count of minor
currency units (cents for USD). Positive means money leaving the user (a purchase, a fee); negative
means money returning to the user (a refund, a credit). Every parser must assert this sign
explicitly in its tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol, get_args, runtime_checkable

#: `Transaction.classified_by` for a one-time edit by the user. A transaction carrying it is final:
#: no rule, re-run, cache or LLM pass may change it (I6). It is never used for a merchant rule.
USER_CLASSIFIED = "user"

#: Every value `Transaction.classified_by` can hold: the single source of truth. The cascade writes
#: these (a row the LLM phase could not place is `llm` too: batching's placeholder is an
#: `Item`, so the import path and "Revert to automatic" agree), and the API's `ClassifiedBy`
#: type and the undo-token validation (`CLASSIFIED_BY_VALUES`) both derive from it.
ClassifiedBy = Literal["cache", "user_rule", "builtin_rule", "issuer", "llm", "user", "kind"]
CLASSIFIED_BY_VALUES: frozenset[str] = frozenset(get_args(ClassifiedBy))

#: Minimum median page character count for a document to be considered to have a text layer.
TEXT_LAYER_MIN_CHARS = 50


@dataclass(frozen=True, slots=True)
class Word:
    """A single extracted word/token with its bounding box, in PDF page coordinates (points,
    origin top-left, `top`/`bottom` increasing downward)."""

    text: str
    x0: float
    x1: float
    top: float
    bottom: float


@dataclass(frozen=True, slots=True)
class PageText:
    """The extracted content of one page."""

    page_number: int  # 1-based
    text: str
    words: tuple[Word, ...]
    char_count: int


@dataclass(frozen=True, slots=True)
class ExtractedDoc:
    """The result of extracting a PDF's text layer, before any layout-specific parsing."""

    file_sha256: str
    page_count: int
    pages: tuple[PageText, ...]
    has_text_layer: bool  # median page char_count >= TEXT_LAYER_MIN_CHARS

    @property
    def full_text(self) -> str:
        """All pages' text joined by a form-feed, matching how a PDF viewer's "select all"
        would present page breaks: ``"\\n\\f\\n"``."""
        return "\n\f\n".join(page.text for page in self.pages)

    def select_pages(self, pages: Sequence[int] | None) -> ExtractedDoc:
        """A copy of this document keeping only the given 1-based ``pages``.

        Original ``page_number`` values are preserved; ``page_count`` becomes the number of pages
        kept and ``has_text_layer`` is recomputed over them. Out-of-range pages are ignored, and
        an empty/``None`` selection (or one matching no page) returns the document unchanged.
        """
        if not pages:
            return self
        wanted = set(pages)
        kept = tuple(p for p in self.pages if p.page_number in wanted)
        if not kept:
            return self
        char_counts = sorted(p.char_count for p in kept)
        mid = len(char_counts) // 2
        median = (
            char_counts[mid]
            if len(char_counts) % 2
            else (char_counts[mid - 1] + char_counts[mid]) / 2
        )
        return ExtractedDoc(
            file_sha256=self.file_sha256,
            page_count=len(kept),
            pages=kept,
            has_text_layer=median >= TEXT_LAYER_MIN_CHARS,
        )


@dataclass(frozen=True, slots=True)
class AccountHint:
    """What a parser could infer about the account from the statement itself.

    Deliberately has no issuer field: issuer identity comes from issuer matching (§2f.4) or the
    user (D14), never from the parser.
    """

    account_type: Literal["credit", "checking", "savings"] | None
    mask: str | None  # last 4 digits only; LOCAL ONLY (I1b) — never egressed
    currency: str | None


#: The seven kinds a transaction can have. Exactly one is stored per transaction.
Kind = Literal["purchase", "refund", "payment", "transfer", "fee", "interest", "adjustment"]

#: A `Kind`, or the ambiguous `"payment_or_refund"` hint a parser may emit when a section
#: heading (e.g. "PAYMENTS AND OTHER CREDITS") cannot tell the two apart on its own (A29).
#: `classify/kinds.py` resolves this by pattern: a payment pattern match -> `payment`, else
#: `refund`.
KindHint = Kind | Literal["payment_or_refund"]


@dataclass(frozen=True, slots=True)
class RawTransaction:
    """One transaction row as a parser read it, before normalization, classification, or
    persistence."""

    posted_date: date
    transaction_date: date | None
    description: str
    amount_minor: int  # SIGNED. positive = outflow. See I5.
    currency: str
    fx_amount_minor: int | None = None
    fx_currency: str | None = None
    fx_rate: float | None = None
    kind_hint: KindHint | None = None  # e.g. 'payment_or_refund', from a section heading
    section: str | None = None  # statement section label, as printed
    issuer_category: str | None = None  # A16: issuer's own category column, if the layout has one


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    """The full result of parsing one statement PDF.

    **Balance equation (A25), positive = outflow (I5):**

    - credit card: ``closing_balance_minor - opening_balance_minor == sum(amount_minor)``
      (the balance owed rises with spending)
    - checking/savings: ``opening_balance_minor - closing_balance_minor == sum(amount_minor)``
      (the balance held falls)

    Balances themselves are stored exactly as printed (a credit balance on a card statement is
    negative).
    """

    account_hint: AccountHint
    period_start: date | None
    period_end: date | None
    stated_total_minor: int | None  # statement's own "new charges"/"total debits", if readable
    transactions: tuple[RawTransaction, ...]
    section_totals: tuple[tuple[str, int], ...] = ()  # A15: (section label, total minor units)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    opening_balance_minor: int | None = None  # A25
    closing_balance_minor: int | None = None  # A25


@runtime_checkable
class StatementParser(Protocol):
    """The interface every built-in parser, the generic fallback, and the layout-spec
    interpreter implement."""

    id: str  # stable, layout-based, e.g. 'layout_b_credit'; never a bank name (D14)
    version: str  # semver-ish; bumped when output changes; recorded per statement
    account_type: str  # credit | checking | savings

    def detect(self, doc: ExtractedDoc) -> float:
        """Return a confidence in ``[0.0, 1.0]`` that this parser handles ``doc``.

        Must never raise: an implementation that raises is treated by the registry as a score
        of ``0.0``.
        """
        ...

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        """Parse ``doc`` into a `ParsedStatement`.

        Raises:
            ParserError: on an unrecoverable layout failure (e.g. the statement period could not
                be read for a layout whose dates carry no year).
        """
        ...
