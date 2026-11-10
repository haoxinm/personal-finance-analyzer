"""Generic fallback parser (P1-B, D3).

Runs when no layout parser (P1-B1…B4) scores its ``detect()`` above ``0.5`` — the unknown-layout
funnel's first stop (§2d.2). Heuristic and honest about its limits: it recognizes only what the
shared primitives in `personal_finance_analyzer.ingest.layout` (A30) already expose — clustered rows, inferred
column bands, money and date parsing — and never re-implements them.

Two amount shapes are recognized (§2c Layout D, A26):

- a single **signed amount column** (card-style statements): a printed minus, parentheses, a
  trailing ``CR``, or a trailing ``-`` means a credit (I5: negative, money returning to the user);
  anything else is a purchase-shaped debit (positive, money leaving the user).
- a **debit/credit column pair**, optionally followed by a running **balance** column
  (bank-statement-style statements): the debit column is positive (outflow), the credit column is
  negative (inflow); a row with a value in both is a `ParserError` (mirrors Layout D).

A row this parser cannot confidently place a date for (an unrecognized continuation line, a
foreign-currency info line, ...) is folded into the previous transaction's description rather than
guessed at — the generic parser prefers dropping detail to inventing it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from personal_finance_analyzer.config import load_settings
from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    ParsedStatement,
    RawTransaction,
)
from personal_finance_analyzer.ingest.layout import (
    Band,
    Row,
    RowKind,
    TableBand,
    assign_to_bands,
    cluster_rows,
    extract_period_and_balances,
    infer_column_bands,
    infer_year,
    locate_table_bands,
    parse_date,
    parse_money,
)

#: Below this many recognized transactions, the layout is not usable (§ P1-B item 5); the
#: pipeline routes the statement to the layout mapper instead (§2d.2).
_MIN_TRANSACTIONS = 3

#: The minimum fraction of a column's non-blank cells that must parse as a date (or money) for
#: that column to be recognized as the date (or a money) column.
_ROLE_FRACTION_THRESHOLD = 0.5

_SAVINGS_RE = re.compile(r"\bsavings\b", re.IGNORECASE)

_SKIPPED_ROW_KINDS: frozenset[RowKind] = frozenset({"header", "total"})


@dataclass(frozen=True, slots=True)
class _ColumnRoles:
    """Which inferred `Band`s play which role in one table band's rows.

    ``money_bands`` is left-to-right: one band means a single signed-amount column; two means a
    debit/credit pair; three means debit, credit, then a running balance column.
    """

    date_band: Band
    description_bands: tuple[Band, ...]
    money_bands: tuple[Band, ...]


class GenericTableParser:
    """Heuristic fallback parser: locates a transaction table by shape alone and recognizes a
    signed-amount column or a debit/credit(/balance) column group. Implements the
    `personal_finance_analyzer.core.types.StatementParser` protocol."""

    id = "generic_table"
    version = "1.0.0"
    #: Nominal default. The *actual* account type of a parsed statement is carried by
    #: `ParsedStatement.account_hint.account_type`, inferred per document (D11): this parser
    #: handles both card-style and bank-style statements, unlike a real layout parser which is
    #: pinned to one account type.
    account_type = "credit"

    def detect(self, doc: ExtractedDoc) -> float:
        """Confidence that *some* transaction table is present, deliberately capped well below
        the registry's ``0.5`` selection threshold (§2d.2): the fallback must never outscore a
        real layout parser. Never raises."""
        try:
            bands = locate_table_bands(doc)
            total_data_rows = sum(kind == "data" for band in bands for kind in band.row_kinds)
        except Exception:  # detect() must never raise (§3.1)
            return 0.0
        return 0.3 if total_data_rows >= _MIN_TRANSACTIONS else 0.0

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        """Parse ``doc`` heuristically.

        Raises:
            UnsupportedLayoutError: fewer than 3 transactions were recognized.
            ParserError: a row printed a value in both the debit and credit columns, or a
                year-less date could not be resolved against the statement period.
        """
        settings = load_settings()
        date_formats = settings.ingest.date_format_hints
        currency = settings.ingest.default_currency

        table_bands = locate_table_bands(doc)
        if not table_bands:
            raise UnsupportedLayoutError("generic parser found no transaction table")

        summary = extract_period_and_balances(doc)
        transactions: list[RawTransaction] = []
        warnings: list[str] = []
        account_type: Literal["credit", "checking", "savings"] = "credit"
        account_type_set = False

        for table_band in table_bands:
            data_rows = [
                row
                for row, kind in zip(table_band.rows, table_band.row_kinds, strict=True)
                if kind == "data"
            ]
            if not data_rows:
                continue
            bands = _infer_bands(doc, table_band, data_rows)
            roles = _classify_columns(data_rows, bands, date_formats)
            if roles is None:
                # No recognizable date+money column shape in this band; skip it rather than
                # guess (honest about limits).
                continue

            if not account_type_set:
                account_type = "checking" if len(roles.money_bands) >= 2 else "credit"
                if _SAVINGS_RE.search(doc.full_text):
                    account_type = "savings"
                account_type_set = True

            _parse_table_band(
                table_band,
                bands,
                roles,
                date_formats=date_formats,
                period_start=summary.period_start,
                period_end=summary.period_end,
                currency=currency,
                opening_balance_minor=summary.opening_balance_minor,
                transactions=transactions,
                warnings=warnings,
            )

        if len(transactions) < _MIN_TRANSACTIONS:
            raise UnsupportedLayoutError(
                f"generic parser found only {len(transactions)} transaction(s); needs at least "
                f"{_MIN_TRANSACTIONS}"
            )

        _check_balance_equation(
            transactions,
            account_type=account_type,
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
            warnings=warnings,
        )

        return ParsedStatement(
            account_hint=AccountHint(account_type=account_type, mask=None, currency=currency),
            period_start=summary.period_start,
            period_end=summary.period_end,
            stated_total_minor=None,
            transactions=tuple(transactions),
            warnings=tuple(warnings),
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
        )


#: A ready-to-use singleton, the way every discovered built-in parser is expected to be exposed
#: (§3.1 `StatementParser`; discovered by `ingest.registry` as the guaranteed fallback).
generic_table = GenericTableParser()


# --------------------------------------------------------------------------------------------
# Column bands
# --------------------------------------------------------------------------------------------


def _infer_bands(
    doc: ExtractedDoc, table_band: TableBand, data_rows: Sequence[Row]
) -> tuple[Band, ...]:
    """Prefer bands inferred from the table's own header row, when one was located: the header
    always has every column populated, so it splits cleanly even for a debit/credit layout where
    each data row leaves one of the two money columns blank. Falls back to inferring from the
    data rows themselves when no header row was found."""
    if table_band.header_row_index is not None:
        page = doc.pages[table_band.page_number - 1]
        page_rows = cluster_rows(page.words)
        if 0 <= table_band.header_row_index < len(page_rows):
            header_bands = infer_column_bands([page_rows[table_band.header_row_index]])
            if header_bands:
                return header_bands
    return infer_column_bands(data_rows)


# --------------------------------------------------------------------------------------------
# Column-role classification
# --------------------------------------------------------------------------------------------


def _classify_columns(
    rows: Sequence[Row], bands: tuple[Band, ...], date_formats: tuple[str, ...]
) -> _ColumnRoles | None:
    if not bands:
        return None

    texts_by_band: dict[str, list[str]] = {
        band.name: [assign_to_bands(row, bands).get(band.name, "") for row in rows]
        for band in bands
    }

    def fraction(is_match: Callable[[str], bool], texts: list[str]) -> float:
        nonblank = [t for t in texts if t.strip()]
        if not nonblank:
            return 0.0
        return sum(1 for t in nonblank if is_match(t)) / len(nonblank)

    date_frac = {
        band.name: fraction(
            lambda t: parse_date(t.strip(), date_formats) is not None, texts_by_band[band.name]
        )
        for band in bands
    }
    money_frac = {
        band.name: fraction(lambda t: parse_money(t) is not None, texts_by_band[band.name])
        for band in bands
    }

    date_candidates = [band for band in bands if date_frac[band.name] > _ROLE_FRACTION_THRESHOLD]
    if not date_candidates:
        return None
    date_band = date_candidates[0]

    money_bands = tuple(
        band
        for band in bands
        if band.name != date_band.name and money_frac[band.name] > _ROLE_FRACTION_THRESHOLD
    )
    if not money_bands:
        return None

    money_names = {band.name for band in money_bands}
    description_bands = tuple(
        band for band in bands if band.name != date_band.name and band.name not in money_names
    )
    return _ColumnRoles(
        date_band=date_band, description_bands=description_bands, money_bands=money_bands
    )


# --------------------------------------------------------------------------------------------
# Row-level extraction
# --------------------------------------------------------------------------------------------


def _row_description(cells: dict[str, str], roles: _ColumnRoles) -> str:
    parts = [cells.get(band.name, "").strip() for band in roles.description_bands]
    return " ".join(part for part in parts if part).strip()


def _row_amount_minor(cells: dict[str, str], roles: _ColumnRoles) -> int | None:
    """Return the signed amount (I5) for one data row, or `None` if no money column is
    populated. Raises `ParserError` if both a debit and a credit are populated (Layout D rule)."""
    texts = [cells.get(band.name, "") for band in roles.money_bands]
    parsed = [parse_money(text) if text.strip() else None for text in texts]

    if len(roles.money_bands) == 1:
        amount = parsed[0]
        if amount is None:
            return None
        return -amount.magnitude_minor if amount.printed_negative else amount.magnitude_minor

    debit, credit = parsed[0], parsed[1]
    if debit is not None and credit is not None:
        raise ParserError("generic parser: row has a value in both the debit and credit columns")
    if debit is not None:
        return debit.magnitude_minor
    if credit is not None:
        return -credit.magnitude_minor
    return None


def _row_balance_minor(cells: dict[str, str], roles: _ColumnRoles) -> int | None:
    if len(roles.money_bands) < 3:
        return None
    text = cells.get(roles.money_bands[2].name, "")
    if not text.strip():
        return None
    parsed = parse_money(text)
    if parsed is None:
        return None
    return -parsed.magnitude_minor if parsed.printed_negative else parsed.magnitude_minor


def _append_description(txn: RawTransaction, extra_row_text: str) -> RawTransaction:
    extra = " ".join(extra_row_text.split())
    if not extra:
        return txn
    return replace(txn, description=f"{txn.description} {extra}".strip())


def _parse_table_band(
    table_band: TableBand,
    bands: tuple[Band, ...],
    roles: _ColumnRoles,
    *,
    date_formats: tuple[str, ...],
    period_start: date | None,
    period_end: date | None,
    currency: str,
    opening_balance_minor: int | None,
    transactions: list[RawTransaction],
    warnings: list[str],
) -> None:
    current_section: str | None = None
    prev_balance = opening_balance_minor
    pending_delta = 0

    for row, kind in zip(table_band.rows, table_band.row_kinds, strict=True):
        if kind == "section":
            current_section = row.text.strip() or None
            continue
        if kind in _SKIPPED_ROW_KINDS:
            continue

        cells = assign_to_bands(row, bands)
        date_text = cells.get(roles.date_band.name, "").strip()
        partial = parse_date(date_text, date_formats)

        if kind != "data" or partial is None:
            # A multi-line description continuation, a foreign-currency info line, or a "data"
            # row this parser could not confidently date: fold into the previous transaction
            # rather than guess (§ generic fallback parser, item 4).
            if transactions and row.text.strip():
                transactions[-1] = _append_description(transactions[-1], row.text)
            continue

        if partial.year is not None:
            posted_date = date(partial.year, partial.month, partial.day)
        elif period_start is not None and period_end is not None:
            posted_date = infer_year(partial, period_start, period_end)
        else:
            raise ParserError(
                "generic parser: a date has no year and the statement period could not be read"
            )

        amount_minor = _row_amount_minor(cells, roles)
        if amount_minor is None:
            # Classified as a data row (some money-like token triggered it) but this parser's
            # own column split found nothing usable: fold into the previous description instead
            # of inventing a zero-amount transaction.
            if transactions and row.text.strip():
                transactions[-1] = _append_description(transactions[-1], row.text)
            continue

        transactions.append(
            RawTransaction(
                posted_date=posted_date,
                transaction_date=None,
                description=_row_description(cells, roles),
                amount_minor=amount_minor,
                currency=currency,
                section=current_section,
            )
        )

        pending_delta += amount_minor
        balance_minor = _row_balance_minor(cells, roles)
        if balance_minor is not None:
            if prev_balance is not None:
                expected = prev_balance - pending_delta
                if expected != balance_minor:
                    warnings.append(
                        f"running balance mismatch after {posted_date.isoformat()}: "
                        f"expected {expected}, printed {balance_minor}"
                    )
            prev_balance = balance_minor
            pending_delta = 0


def _check_balance_equation(
    transactions: list[RawTransaction],
    *,
    account_type: str,
    opening_balance_minor: int | None,
    closing_balance_minor: int | None,
    warnings: list[str],
) -> None:
    if opening_balance_minor is None or closing_balance_minor is None:
        return
    total = sum(txn.amount_minor for txn in transactions)
    expected = (
        closing_balance_minor - opening_balance_minor
        if account_type == "credit"
        else opening_balance_minor - closing_balance_minor
    )
    if expected != total:
        warnings.append(
            f"balance equation mismatch: opening={opening_balance_minor} "
            f"closing={closing_balance_minor} sum(amounts)={total} expected={expected}"
        )
