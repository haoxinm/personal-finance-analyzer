"""Layout A — ``layout_a_credit``: sectioned, dual-date credit-card statement (§2c).

Distinctive fingerprint: a ``Transactions`` table with a ``Reference Number`` / ``Account
Number`` column pair, split into four labelled sections (``Payments and Other Credits``,
``Purchases and Adjustments``, ``Fees``, ``Interest Charged``), each terminated by an all-caps
``TOTAL <SECTION> FOR THIS PERIOD`` line carrying that section's total.

Sign convention (I5): amounts are mapped exactly as printed — a purchase prints unsigned
(positive/outflow) and a credit prints with a leading ``-`` (negative/inflow). This already
satisfies the signed-minor-units contract; no per-section sign flip is needed.

Kind resolution: within ``Purchases and Adjustments`` the sign alone decides ``purchase`` vs.
``adjustment``. Within ``Payments and Other Credits`` a section heading cannot tell a payment
from a refund on its own (A29): this parser emits the ambiguous ``KindHint`` value
``'payment_or_refund'`` and leaves the pattern match to `classify/kinds.py`, which owns that
pattern as the single source of truth.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

from personal_finance_analyzer.core.errors import ParserError
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
    parse_date,
    parse_money,
)

#: Column order as printed (§2c), and the header label above each. ``total`` is a running
#: balance and is never used. The header labels drive `_bands_from_header`: each column's band
#: starts at its own header word's x-position — not at a gap-inferred midpoint, which drifts
#: left of the true boundary whenever a data row's content (e.g. a long description) runs
#: longer than the short label printed above it.
_COLUMN_ROLES = (
    "transaction_date",
    "posting_date",
    "description",
    "reference_number",
    "account_number",
    "amount",
    "total",
)
_HEADER_LABELS = (
    "Trans Date",
    "Post Date",
    "Description",
    "Reference Number",
    "Account Number",
    "Amount",
    "Total",
)

#: Section heading text (as printed, all-caps) -> the label stored on `RawTransaction.section`
#: and in `ParsedStatement.section_totals`.
_SECTION_HEADINGS: dict[str, str] = {
    "payments and other credits": "Payments and Other Credits",
    "purchases and adjustments": "Purchases and Adjustments",
    "fees": "Fees",
    "interest charged": "Interest Charged",
}

_TERMINATOR_RE = re.compile(
    r"^TOTAL\s+(?P<section>.+?)\s+FOR THIS PERIOD\b\s*(?P<amount>.*)$", re.IGNORECASE
)

#: A payment description ("ONLINE PAYMENT - THANK YOU", "AUTOPAY PAYMENT") vs. a refund.
#: Kept only for reference in `detect()`'s docstring; actual resolution is `classify/kinds.py`'s
#: job (A29) — this parser never applies it.
_DETECT_HEADING_RE = re.compile(
    r"\bTOTAL\s+[A-Za-z][A-Za-z \-]+\s+FOR THIS PERIOD\b", re.IGNORECASE
)

#: A foreign-currency line under a transaction row, e.g. ``"40.00 EUR X 1.1250"`` (§2c common
#: requirements: foreign-currency line handling).
_FX_LINE_RE = re.compile(
    r"^(?P<amount>[\d,]+\.\d{2})\s+(?P<currency>[A-Z]{3})(?:\s*[Xx@]\s*(?P<rate>[\d.]+))?\s*$"
)

_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")


class LayoutACredit:
    """Parser for Layout A — sectioned, dual-date credit-card statements."""

    id = "layout_a_credit"
    version = "1.0.0"
    account_type = "credit"

    def detect(self, doc: ExtractedDoc) -> float:
        """Score confidence per §2c: ``0.9`` when at least 3 of the 4 section headings and the
        ``Reference Number``/``Account Number`` column pair are present; ``0.6`` when only the
        ``TOTAL … FOR THIS PERIOD`` terminator pattern is found; ``0.0`` otherwise. Never raises.
        """
        try:
            text = doc.full_text
        except Exception:  # detect() must never raise (§3.1)
            return 0.0

        lower = text.lower()
        heading_hits = sum(1 for heading in _SECTION_HEADINGS if heading in lower)
        has_columns = "reference number" in lower and "account number" in lower
        if heading_hits >= 3 and has_columns:
            return 0.9
        if _DETECT_HEADING_RE.search(text):
            return 0.6
        return 0.0

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        """Parse ``doc`` into a `ParsedStatement`.

        Raises:
            ParserError: the column header row (carrying ``Reference Number`` and ``Account
                Number``) could not be located, column bands could not be inferred from it, or a
                data row's amount cell is present but not money-shaped.
        """
        summary = extract_period_and_balances(doc)
        rows = [row for page in doc.pages for row in cluster_rows(page.words)]

        header_index = _find_header_row(rows)
        if header_index is None:
            raise ParserError(
                "layout_a_credit: could not locate the 'Reference Number'/'Account Number' "
                "column header row"
            )

        bands = _bands_from_header(rows[header_index])
        if bands is None:
            raise ParserError(
                f"layout_a_credit: expected {sum(len(label.split()) for label in _HEADER_LABELS)} header "
                "words to derive column bands, found a different count"
            )
        role_by_band_name = dict(zip((b.name for b in bands), _COLUMN_ROLES, strict=True))

        warnings: list[str] = []
        section_totals: list[tuple[str, int]] = []
        transactions: list[RawTransaction] = []
        mask: str | None = None

        current_section: str | None = None
        current_kind_hint: KindHint | None = None
        closed_sections: set[str] = set()
        pending: RawTransaction | None = None

        def flush_pending() -> None:
            nonlocal pending
            if pending is not None:
                transactions.append(pending)
                pending = None

        for row in rows[header_index + 1 :]:
            text = row.text.strip()
            if not text:
                continue
            lower = text.lower()

            if lower in _SECTION_HEADINGS:
                flush_pending()
                current_section = _SECTION_HEADINGS[lower]
                current_kind_hint = _kind_hint_for_section(current_section)
                continue

            terminator = _TERMINATOR_RE.match(text)
            if terminator is not None and current_section is not None:
                flush_pending()
                total_money = parse_money(terminator.group("amount"))
                if total_money is not None:
                    total_minor = (
                        -total_money.magnitude_minor
                        if total_money.printed_negative
                        else total_money.magnitude_minor
                    )
                    section_totals.append((current_section, total_minor))
                else:
                    warnings.append(f"could not parse total for section {current_section!r}")
                closed_sections.add(current_section)
                current_section = None
                current_kind_hint = None
                if closed_sections.issuperset(_SECTION_HEADINGS.values()):
                    break
                continue

            if current_section is None:
                continue  # stray row outside any known section (e.g. table caption)

            fx_match = _FX_LINE_RE.match(text)
            if fx_match is not None and pending is not None:
                fx_money = parse_money(fx_match.group("amount"))
                if fx_money is not None:
                    rate_text = fx_match.group("rate")
                    pending = replace(
                        pending,
                        fx_amount_minor=fx_money.magnitude_minor,
                        fx_currency=fx_match.group("currency"),
                        fx_rate=float(rate_text) if rate_text else None,
                    )
                    continue

            assigned = assign_to_bands(row, bands)
            fields = {role_by_band_name[name]: value.strip() for name, value in assigned.items()}
            amount_text = fields["amount"]

            if not amount_text:
                # Continuation row: no amount, merge its description into the pending row.
                if pending is None:
                    warnings.append(f"orphan continuation row in section {current_section!r}")
                    continue
                extra = fields["description"]
                if extra:
                    pending = replace(pending, description=f"{pending.description} {extra}")
                continue

            amount = parse_money(amount_text)
            if amount is None:
                raise ParserError(f"layout_a_credit: unparseable amount {amount_text!r}")

            flush_pending()

            transaction_date = _parse_full_date(fields["transaction_date"])
            posting_date = _parse_full_date(fields["posting_date"])
            if posting_date is None:
                raise ParserError(
                    f"layout_a_credit: unparseable posting date {fields['posting_date']!r}"
                )

            amount_minor = (
                -amount.magnitude_minor if amount.printed_negative else amount.magnitude_minor
            )
            kind_hint = current_kind_hint
            if current_section == "Purchases and Adjustments":
                kind_hint = "adjustment" if amount.printed_negative else "purchase"

            if mask is None and fields["account_number"]:
                digits = re.sub(r"\D", "", fields["account_number"])
                if len(digits) >= 4:
                    mask = digits[-4:]

            pending = RawTransaction(
                posted_date=posting_date,
                transaction_date=transaction_date,
                description=fields["description"],
                amount_minor=amount_minor,
                currency="USD",
                kind_hint=kind_hint,
                section=current_section,
            )

        flush_pending()

        missing_sections = set(_SECTION_HEADINGS.values()) - closed_sections
        if missing_sections:
            warnings.append(f"sections never closed by a TOTAL line: {sorted(missing_sections)}")

        if summary.period_start is None or summary.period_end is None:
            warnings.append("statement summary block not found; period and balances unavailable")

        return ParsedStatement(
            account_hint=AccountHint(account_type="credit", mask=mask, currency="USD"),
            period_start=summary.period_start,
            period_end=summary.period_end,
            stated_total_minor=None,
            transactions=tuple(transactions),
            section_totals=tuple(section_totals),
            warnings=tuple(warnings),
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
        )


def _bands_from_header(header_row: Row) -> tuple[Band, ...] | None:
    """Column bands whose boundaries are each header label's own leading word x-position.

    Deliberately not `infer_column_bands`: that function infers a boundary from the *gap*
    between two words, which drifts left of the true column edge whenever a short header label
    (e.g. ``"Description"``) sits above a column that data rows fill much more fully — a long
    description would then straddle the inferred boundary and part of it would be misassigned
    to the next column. Header labels, unlike data, reliably start exactly at their column's
    left edge, so anchoring on their word positions is the robust choice here.

    Returns `None` when ``header_row`` does not have the expected number of words.
    """
    expected_word_counts = tuple(len(label.split()) for label in _HEADER_LABELS)
    words = header_row.words
    if len(words) != sum(expected_word_counts):
        return None

    starts: list[float] = []
    index = 0
    for count in expected_word_counts:
        starts.append(words[index].x0)
        index += count

    ends = [*starts[1:], starts[-1] + 10_000.0]
    return tuple(
        Band(name=f"col_{i}", x0=start, x1=end)
        for i, (start, end) in enumerate(zip(starts, ends, strict=True))
    )


def _find_header_row(rows: list[Row]) -> int | None:
    for index, row in enumerate(rows):
        lower = row.text.lower()
        if "reference number" in lower and "account number" in lower:
            return index
    return None


def _kind_hint_for_section(section: str) -> KindHint | None:
    if section == "Payments and Other Credits":
        return "payment_or_refund"
    if section == "Fees":
        return "fee"
    if section == "Interest Charged":
        return "interest"
    return None  # 'Purchases and Adjustments' is resolved per-row from the printed sign.


def _parse_full_date(text: str) -> date | None:
    if not text:
        return None
    partial = parse_date(text, _DATE_FORMATS)
    if partial is None or partial.year is None:
        return None
    return date(partial.year, partial.month, partial.day)


#: Module-level instance, discovered by the registry (P1-B) via module scan.
parser = LayoutACredit()

__all__ = ["LayoutACredit", "parser"]
