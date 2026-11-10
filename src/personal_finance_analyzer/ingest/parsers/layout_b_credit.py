"""Layout B — ``layout_b_credit``: dual-table, capitalized credit-card statement (§2c).

Two distinct tables share the page: ``ACCOUNT ACTIVITY`` (the transaction list this parser
produces rows from) and ``INTEREST CHARGED`` (balance-type / APR rows — **not** transactions).
The label ``PURCHASES`` appears in both tables; this parser disambiguates by which table it is
currently inside, never by the label alone, and stops reading altogether once it reaches
``INTEREST CHARGED`` so that table can never contribute a row.

**Dates:** one column only, ``Date of Transaction``, with no year printed. `posted_date` is
resolved against the statement period via `layout.infer_year`; `transaction_date` is always
`None` — this layout never prints a second date.

**Sign (I5):** printed as-is already satisfies the convention — purchases print unsigned
(positive/outflow), credits print with a leading ``-`` (negative/inflow).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    KindHint,
    ParsedStatement,
    RawTransaction,
)
from personal_finance_analyzer.ingest.layout import (
    Row,
    cluster_rows,
    extract_period_and_balances,
    infer_year,
    parse_date,
    parse_money,
)

#: Distinctive column phrase (§2c): present only in this layout's ACCOUNT ACTIVITY header.
_MERCHANT_COLUMN_PHRASE = "Merchant Name or Transaction Description"

_ACCOUNT_ACTIVITY_RE = re.compile(r"^ACCOUNT ACTIVITY$")
_INTEREST_CHARGED_RE = re.compile(r"^INTEREST CHARGED\b")
_ACTIVITY_HEADER_RE = re.compile(re.escape(_MERCHANT_COLUMN_PHRASE), re.IGNORECASE)
_PURCHASES_SECTION = "PURCHASES"
_PAYMENTS_SECTION = "PAYMENTS AND OTHER CREDITS"
_KNOWN_SECTIONS = (_PAYMENTS_SECTION, _PURCHASES_SECTION)

_MASK_RE = re.compile(r"\bending in\s+(\d{4})\b", re.IGNORECASE)

#: A continuation line printing the original foreign-currency amount and exchange rate, e.g.
#: ``FOREIGN CURRENCY AMOUNT 35.00 GBP EXCH RATE 1.20000``.
_FX_LINE_RE = re.compile(
    r"^FOREIGN CURRENCY AMOUNT\s+([\d,]+\.\d{2})\s+([A-Z]{3})\s+EXCH(?:ANGE)? RATE\s+([\d.]+)$",
    re.IGNORECASE,
)

_ROW_DATE_FORMATS = ("%m/%d",)

_YEARLESS_DATE_HELP = (
    "layout_b_credit dates carry no year (§2c); the statement period must be read from the "
    "summary block to resolve them"
)


@dataclass
class _Pending:
    """A transaction under construction, mutable while later continuation rows are merged."""

    posted_date_text: str
    description_parts: list[str]
    amount_minor: int
    section: str
    fx_amount_minor: int | None = None
    fx_currency: str | None = None
    fx_rate: float | None = None


def _is_data_row(text: str) -> tuple[str, ...] | None:
    """Split ``text`` into tokens if its first token is a bare ``MM/DD`` date, else `None`."""
    tokens = text.split()
    if not tokens:
        return None
    if parse_date(tokens[0], _ROW_DATE_FORMATS) is None:
        return None
    return tuple(tokens)


def _resolve_kind_hint(section: str, amount_minor: int) -> KindHint:
    if section == _PAYMENTS_SECTION:
        # A29: a section heading alone cannot tell payment from refund; classify/kinds.py
        # resolves this hint by pattern later.
        return "payment_or_refund"
    # _PURCHASES_SECTION: outflow (positive) is a purchase; an inflow here is a mid-cycle
    # adjustment (e.g. a merchant-issued statement credit), not a refund.
    return "purchase" if amount_minor > 0 else "adjustment"


class LayoutBCreditParser:
    """`StatementParser` for Layout B (§2c) — dual-table, capitalized credit-card statement."""

    id = "layout_b_credit"
    version = "1.0.0"
    account_type = "credit"

    def detect(self, doc: ExtractedDoc) -> float:
        try:
            text = doc.full_text
            has_activity = "ACCOUNT ACTIVITY" in text.upper()
            has_column = bool(_ACTIVITY_HEADER_RE.search(text))
            if has_activity and has_column:
                return 0.95
            if has_activity or has_column:
                return 0.3
            return 0.0
        except Exception:  # pragma: no cover - detect() must never raise (§3.1)
            return 0.0

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        summary = extract_period_and_balances(doc)
        if summary.period_start is None or summary.period_end is None:
            raise ParserError(
                "layout_b_credit: could not read the statement period from the summary block "
                f"({_YEARLESS_DATE_HELP})"
            )

        mask_match = _MASK_RE.search(doc.full_text)
        mask = mask_match.group(1) if mask_match else None

        pendings = list(self._walk_rows(doc))
        if not pendings:
            raise ParserError(
                "layout_b_credit: no ACCOUNT ACTIVITY transactions found; the statement may be "
                "malformed or use a different layout"
            )

        transactions: list[RawTransaction] = []
        for pending in pendings:
            partial = parse_date(pending.posted_date_text, _ROW_DATE_FORMATS)
            assert partial is not None  # guaranteed by _is_data_row
            posted = infer_year(partial, summary.period_start, summary.period_end)
            description = " ".join(part for part in pending.description_parts if part)
            transactions.append(
                RawTransaction(
                    posted_date=posted,
                    transaction_date=None,
                    description=description,
                    amount_minor=pending.amount_minor,
                    currency="USD",
                    fx_amount_minor=pending.fx_amount_minor,
                    fx_currency=pending.fx_currency,
                    fx_rate=pending.fx_rate,
                    kind_hint=_resolve_kind_hint(pending.section, pending.amount_minor),
                    section=pending.section,
                    issuer_category=None,
                )
            )

        return ParsedStatement(
            account_hint=AccountHint(account_type="credit", mask=mask, currency="USD"),
            period_start=summary.period_start,
            period_end=summary.period_end,
            stated_total_minor=None,
            transactions=tuple(transactions),
            section_totals=(),
            warnings=(),
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
        )

    def _walk_rows(self, doc: ExtractedDoc) -> list[_Pending]:
        pendings: list[_Pending] = []
        in_activity = False
        section: str | None = None
        current: _Pending | None = None

        for page in doc.pages:
            rows: tuple[Row, ...] = cluster_rows(page.words)
            for row in rows:
                text = row.text.strip()
                if not text:
                    continue
                upper = text.upper()

                if _INTEREST_CHARGED_RE.match(upper):
                    # Everything from here on is the interest table: stop reading for good.
                    return pendings

                if _ACCOUNT_ACTIVITY_RE.match(upper):
                    in_activity = True
                    section = None
                    current = None
                    continue

                if not in_activity:
                    continue

                if _ACTIVITY_HEADER_RE.search(text):
                    continue  # the column-header row itself

                if upper in _KNOWN_SECTIONS:
                    section = upper
                    current = None
                    continue

                tokens = _is_data_row(text)
                if tokens is not None:
                    if section is None:
                        # A data-shaped row before any section heading is not this layout's
                        # table; ignore rather than mis-attribute a section.
                        continue
                    money = parse_money(tokens[-1])
                    if money is None:
                        continue
                    description = " ".join(tokens[1:-1])
                    amount_minor = (
                        -money.magnitude_minor if money.printed_negative else money.magnitude_minor
                    )
                    current = _Pending(
                        posted_date_text=tokens[0],
                        description_parts=[description],
                        amount_minor=amount_minor,
                        section=section,
                    )
                    pendings.append(current)
                    continue

                if current is None:
                    continue  # stray text before any transaction; nothing to attach it to

                fx_match = _FX_LINE_RE.match(text)
                if fx_match is not None:
                    fx_money = parse_money(fx_match.group(1))
                    current.fx_amount_minor = (
                        fx_money.magnitude_minor if fx_money is not None else None
                    )
                    current.fx_currency = fx_match.group(2).upper()
                    current.fx_rate = float(fx_match.group(3))
                    continue

                # A plain continuation line: part of a multi-line description.
                current.description_parts.append(text)

        return pendings


#: Module-level instance, discovered by the registry (P1-B) via module scan.
parser = LayoutBCreditParser()

__all__ = ["LayoutBCreditParser", "parser"]
