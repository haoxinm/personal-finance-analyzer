"""Layout C parser — ``layout_c_credit`` (§2c "Layout C").

Layout C has **no section-heading lines**. Instead, the transaction table repeats its own
column-header row once per section, and the header row's second cell doubles as the section's
name:

- Header row 1: ``TRANS. DATE`` · ``PAYMENTS AND CREDITS`` · ``AMOUNT`` (3 columns)
- Header row 2: ``TRANS DATE`` · ``PURCHASES`` · ``MERCHANT CATEGORY`` · ``AMOUNT`` (4 columns)

Note the inconsistent punctuation between the two header spellings (``TRANS. DATE`` vs
``TRANS DATE``) — periods are stripped before matching so both are recognized.

Column bands are re-derived independently for each section, because the two sections have
different column counts (one global band map would misplace ``MERCHANT CATEGORY`` and
``AMOUNT`` in the payments-and-credits section, which has no category column at all). Each
section's bands come from its own header row's word x-positions directly, rather than from
`layout.infer_column_bands`'s recurring-gap heuristic: a fixed-width-rendered column always
*starts* at the same x-position on every row (header and data alike), because the printed cell
to its left is padded out to that column's full width — but a cell's own trailing whitespace is
invisible, so the *gap* an unrelated row happens to show before a following column varies with
how much of that column's width its own content fills. A merchant-description column is exactly
where that bites: a short header word (``PURCHASES``) sits inside a column sized for much longer
merchant names, so the recurring-gap heuristic (tuned for tables with roughly uniform cell
widths) locates the boundary using the header's own short content instead of the data's true
column width. Column *starts*, not gaps, are what stay put.

**Sign (I5):** payments and credits print with a leading ``-`` (negative = money returning to the
user); purchases print unsigned (positive = outflow). Values are mapped exactly as printed via
`layout.parse_money`, which already satisfies I5.

**Dates:** one column, no year. Year is inferred against the statement period the same way as
Layout B (`layout.infer_year`): a December date on a January statement lands in the prior year.
Reading the period is mandatory — without it a yearless date cannot be resolved, so a missing
summary block is a `ParserError`, never a guess from today's date.

**`MERCHANT CATEGORY` (A16):** captured verbatim and untrimmed into
`RawTransaction.issuer_category`. It is a local-only field (I1b) and is never folded into
`description` — a transaction's `description` only ever comes from the section-name column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    KindHint,
    ParsedStatement,
    RawTransaction,
)
from personal_finance_analyzer.ingest.layout import (
    Band,
    Row,
    assign_to_bands,
    cluster_rows,
    extract_period_and_balances,
    infer_year,
    parse_date,
    parse_money,
)

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%m/%d")

#: Header cell text is matched after stripping periods and collapsing whitespace, so
#: ``"TRANS. DATE"`` and ``"TRANS DATE"`` are the same token here.
_TRANS_DATE_WORDS = ("TRANS", "DATE")
_AMOUNT_WORD = "AMOUNT"
_MERCHANT_CATEGORY_WORDS = ("MERCHANT", "CATEGORY")

#: Generous width for the rightmost (amount) band — there is no following column to bound it,
#: and no printed amount comes close to this many points wide.
_AMOUNT_BAND_WIDTH = 300.0

#: A continuation line carrying a foreign-currency amount for the row above it, e.g.
#: ``"FX 125.00 EUR RATE 1.0870"`` (the rate suffix is optional).
_FX_RE = re.compile(
    r"^FX\s+(?P<amount>[\d,]+\.\d{2})\s+(?P<currency>[A-Z]{3})"
    r"(?:\s+RATE\s+(?P<rate>\d+(?:\.\d+)?))?$"
)

#: Section header second-cell text containing any of these words is the payments/credits
#: section (inflow to the user; ambiguous between `payment` and `refund` on its own — A29).
_PAYMENTS_SECTION_WORDS = {"PAYMENTS", "CREDITS", "CREDIT"}

_ACCOUNT_RE = re.compile(
    r"Account\s*(?:Number)?\s*(?:ending\s*in|[:#*Xx•\-\s]*)\s*(\d{4})\b", re.IGNORECASE
)


def _normalize_word(text: str) -> str:
    return text.rstrip(".").upper()


@dataclass(frozen=True, slots=True)
class _HeaderInfo:
    """One recognized Layout C header row: its section name, whether it has a
    ``MERCHANT CATEGORY`` column, and this section's column bands, keyed by role
    (``'date'``, ``'description'``, ``'issuer_category'`` [only when present], ``'amount'``)."""

    section_label: str
    has_category: bool
    bands: tuple[Band, ...]


def _parse_header_row(row: Row) -> _HeaderInfo | None:
    """Recognize ``row`` as a Layout C column-header row and derive this section's bands
    directly from its words' x-positions, or return `None` if ``row`` is not a header.

    ``section_label`` is the header's own second-cell text, as printed (with
    ``MERCHANT CATEGORY`` stripped off its tail when present).
    """
    words = row.words
    normed = [_normalize_word(w.text) for w in words]
    if len(normed) < 3:
        return None
    if normed[0] != _TRANS_DATE_WORDS[0] or normed[1] != _TRANS_DATE_WORDS[1]:
        return None
    if normed[-1] != _AMOUNT_WORD:
        return None
    middle_words = words[2:-1]
    middle_normed = normed[2:-1]
    if not middle_words:
        return None
    has_category = tuple(middle_normed[-2:]) == _MERCHANT_CATEGORY_WORDS
    section_words = middle_words[:-2] if has_category else middle_words
    if not section_words:
        return None
    section_label = " ".join(w.text for w in section_words)

    date_x0 = words[0].x0
    desc_x0 = section_words[0].x0
    amount_x0 = words[-1].x0
    bands: list[Band] = [Band(name="date", x0=date_x0, x1=desc_x0)]
    if has_category:
        category_x0 = middle_words[-2].x0
        bands.append(Band(name="description", x0=desc_x0, x1=category_x0))
        bands.append(Band(name="issuer_category", x0=category_x0, x1=amount_x0))
    else:
        bands.append(Band(name="description", x0=desc_x0, x1=amount_x0))
    bands.append(Band(name="amount", x0=amount_x0, x1=amount_x0 + _AMOUNT_BAND_WIDTH))

    return _HeaderInfo(section_label=section_label, has_category=has_category, bands=tuple(bands))


def _match_header(row: Row) -> tuple[str, bool] | None:
    """Return ``(section_label, has_category)`` when ``row`` is a Layout C header row, else
    `None`. A thin view onto `_parse_header_row` for callers that only need the section
    identity, not its bands."""
    info = _parse_header_row(row)
    return None if info is None else (info.section_label, info.has_category)


def _kind_hint_for_section(section_label: str) -> KindHint:
    words = {_normalize_word(w) for w in section_label.split()}
    if words & _PAYMENTS_SECTION_WORDS:
        return "payment_or_refund"
    return "purchase"


class LayoutCCreditParser:
    """`StatementParser` for Layout C (§2c "Layout C" — repeating column headers as section
    delimiters)."""

    id = "layout_c_credit"
    version = "1.0.0"
    account_type = "credit"

    def detect(self, doc: ExtractedDoc) -> float:
        """Score `doc` for Layout C. Never raises (per the `StatementParser` protocol).

        Returns 0.9 when a ``TRANS DATE``-shaped header row appears anywhere in the text
        alongside both ``PURCHASES`` and ``MERCHANT CATEGORY`` — that combination is this
        layout's distinctive fingerprint, absent from Layouts A, B, and D. Returns 0.0
        otherwise, including for an empty document.
        """
        text = doc.full_text.upper()
        has_trans_date = re.search(r"TRANS\.?\s*DATE", text) is not None
        has_purchases_and_category = "PURCHASES" in text and "MERCHANT CATEGORY" in text
        if has_trans_date and has_purchases_and_category:
            return 0.9
        return 0.0

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        """Parse `doc` into a `ParsedStatement`.

        Raises:
            ParserError: the statement period could not be read from the summary block (dates
                in this layout carry no year, so the period is mandatory).
            UnsupportedLayoutError: no Layout C column-header row was found at all.
        """
        summary = extract_period_and_balances(doc)
        if summary.period_start is None or summary.period_end is None:
            raise ParserError(
                "layout_c_credit: statement period not found in the summary block; Layout C "
                "dates carry no year and cannot be resolved without it"
            )

        rows: list[Row] = []
        for page in doc.pages:
            rows.extend(cluster_rows(page.words))

        header_infos = [_parse_header_row(row) for row in rows]
        header_positions = [i for i, info in enumerate(header_infos) if info is not None]
        if not header_positions:
            raise UnsupportedLayoutError(
                "layout_c_credit: no 'TRANS DATE ... AMOUNT' header row found"
            )

        transactions: list[RawTransaction] = []
        warnings: list[str] = []

        for pos, header_idx in enumerate(header_positions):
            info = header_infos[header_idx]
            assert info is not None  # by construction of header_positions; narrows for mypy
            block_end = header_positions[pos + 1] if pos + 1 < len(header_positions) else len(rows)
            block_rows = rows[header_idx + 1 : block_end]
            if not block_rows:
                continue

            kind_hint = _kind_hint_for_section(info.section_label)
            n_before = len(transactions)
            self._parse_block(
                block_rows,
                bands=info.bands,
                section_label=info.section_label,
                kind_hint=kind_hint,
                period_start=summary.period_start,
                period_end=summary.period_end,
                transactions=transactions,
                warnings=warnings,
            )
            # ``block_rows`` is non-empty (checked above), so a section that yields no
            # transactions is either all continuation lines or a genuine shape mismatch.
            if len(transactions) == n_before:
                warnings.append(
                    f"shape_warning: section {info.section_label!r} has no transactions"
                )

        account_hint = AccountHint(
            account_type="credit",
            mask=_extract_mask(doc),
            currency="USD",
        )

        if summary.opening_balance_minor is not None and summary.closing_balance_minor is not None:
            expected_delta = summary.closing_balance_minor - summary.opening_balance_minor
            actual_delta = sum(t.amount_minor for t in transactions)
            if expected_delta != actual_delta:
                warnings.append(
                    "shape_warning: balance equation mismatch "
                    f"(closing - opening = {expected_delta}, sum(transactions) = {actual_delta})"
                )

        return ParsedStatement(
            account_hint=account_hint,
            period_start=summary.period_start,
            period_end=summary.period_end,
            stated_total_minor=None,
            transactions=tuple(transactions),
            section_totals=(),
            warnings=tuple(warnings),
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
        )

    @staticmethod
    def _parse_block(
        block_rows: list[Row],
        *,
        bands: tuple[Band, ...],
        section_label: str,
        kind_hint: KindHint,
        period_start: date,
        period_end: date,
        transactions: list[RawTransaction],
        warnings: list[str],
    ) -> None:
        last_index_in_section: int | None = None
        for row in block_rows:
            cells = assign_to_bands(row, bands)
            amount_text = cells["amount"].strip()
            date_text = cells["date"].strip()
            parsed_amount = parse_money(amount_text) if amount_text else None

            if parsed_amount is not None and date_text:
                partial = parse_date(date_text, _DATE_FORMATS)
                if partial is None:
                    warnings.append(
                        f"shape_warning: unparseable date {date_text!r} in section "
                        f"{section_label!r}; row skipped"
                    )
                    continue
                posted_date = infer_year(partial, period_start, period_end)
                description = cells.get("description", "").strip()
                issuer_category = cells.get("issuer_category")
                if issuer_category == "":
                    issuer_category = None
                signed_minor = (
                    -parsed_amount.magnitude_minor
                    if parsed_amount.printed_negative
                    else parsed_amount.magnitude_minor
                )
                transactions.append(
                    RawTransaction(
                        posted_date=posted_date,
                        transaction_date=None,
                        description=description,
                        amount_minor=signed_minor,
                        currency="USD",
                        kind_hint=kind_hint,
                        section=section_label,
                        issuer_category=issuer_category,
                    )
                )
                last_index_in_section = len(transactions) - 1
                continue

            # No amount on this row: either a multi-line description continuation, or a
            # foreign-currency line for the transaction directly above it.
            fragment = cells.get("description", "").strip() or row.text.strip()
            if not fragment:
                continue
            if last_index_in_section is None:
                warnings.append(
                    f"shape_warning: continuation line {fragment!r} in section "
                    f"{section_label!r} precedes any transaction; discarded"
                )
                continue

            fx_match = _FX_RE.match(fragment)
            previous = transactions[last_index_in_section]
            if fx_match is not None:
                fx_amount = Decimal(fx_match.group("amount").replace(",", ""))
                fx_amount_minor = int((fx_amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
                rate_text = fx_match.group("rate")
                transactions[last_index_in_section] = replace(
                    previous,
                    fx_amount_minor=fx_amount_minor,
                    fx_currency=fx_match.group("currency"),
                    fx_rate=float(rate_text) if rate_text is not None else None,
                )
            else:
                transactions[last_index_in_section] = replace(
                    previous, description=f"{previous.description} {fragment}".strip()
                )


def _extract_mask(doc: ExtractedDoc) -> str | None:
    match = _ACCOUNT_RE.search(doc.full_text)
    return match.group(1) if match else None


#: Module-level singleton, matching the `StatementParser` protocol as a plain object (no
#: constructor arguments; discovered by the registry's module scan, P1-B).
layout_c_credit = LayoutCCreditParser()
