"""Layout D — ``layout_d_bank``: bank statement, debit/credit columns (A26, §2c).

The reference bank-statement layout, and the shape of most debit-card statements (D11). Columns
are ``Date · Description · Withdrawals/Debits · Deposits/Credits · Balance``, grouped under
section headings (``Deposits and Additions``, ``ATM & Debit Card Withdrawals``,
``Electronic Withdrawals``, ``Checks Paid``, ``Fees``), with a summary block carrying
``Beginning Balance`` / ``Ending Balance`` and the statement period as
``<Month D, YYYY> through <Month D, YYYY>``.

**Sign convention (I5):** there is no printed sign. The *column* a value is printed in decides the
sign: a value in the withdrawals/debits column is a positive (outflow) ``amount_minor``; a value in
the deposits/credits column is negative (inflow). A row with values in both columns is unparseable
and raises `ParserError` (§2c).

**Dates** carry no year (``MM/DD``); the year is inferred from the statement period via
`personal_finance_analyzer.ingest.layout.infer_year`, exactly as Layout B. `transaction_date` is always
`None` — this layout prints only one date per row.

**Reconciliation:** the running balance column is the strongest signal available. Every row that
prints a balance must satisfy ``balance[i] == balance[i-1] - amount_minor[i]``; a break is recorded
as a `shape_warning` rather than raised, so one dropped row does not fail the whole import (A18,
§2d.1). Rows that print no balance (some banks print it once per day) simply skip the check for
that row; the running total keeps accruing so the next printed balance is still checked against it.
"""

from __future__ import annotations

import re
from datetime import date

from personal_finance_analyzer.classify.kinds import looks_like_card_payment
from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    Kind,
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

#: Section headings, matched case-insensitively with whitespace collapsed. The mapping to
#: `KindHint` is A26's table; `Deposits and Additions` is refined per-row below (a refund pattern
#: overrides the section's default `transfer`).
_SECTION_KIND: dict[str, KindHint] = {
    "deposits and additions": "transfer",
    "atm & debit card withdrawals": "purchase",
    "electronic withdrawals": "purchase",
    "checks paid": "purchase",
    "fees": "fee",
}

_SECTION_HEADING_RE = {
    label: re.compile(rf"^\s*{re.escape(label)}\s*$", re.IGNORECASE) for label in _SECTION_KIND
}

_HEADER_ROW_RE = re.compile(
    r"\bdate\b.*\b(withdrawals|debits)\b.*\b(deposits|credits)\b", re.IGNORECASE
)

#: Merchant-refund patterns within `Deposits and Additions` (§2c): these are spend coming back,
#: not income, so they get `refund` instead of the section's default `transfer`.
_REFUND_RE = re.compile(r"\b(RETURN|REFUND|PURCHASE\s+RETURN)\b", re.IGNORECASE)

# Card payments (I11, A26): the spend was already counted on the card statement, so a payment *to*
# a credit card from this account is a `transfer`, never a `purchase`, no matter which section it
# printed in. The test lives in `classify.kinds` (`looks_like_card_payment`) so the parser and the
# hint-less fallback agree; it needs card wording or a card brand next to a payment word, since a
# bare PAYMENT/PMT/EPAY is as likely rent, a utility or a loan, which is real spend. A row this
# test misses keeps the `purchase` hint, which `resolve_kind` re-checks against the user's
# configured issuers.

#: A trailing foreign-currency line, e.g. ``FOREIGN AMOUNT 45.00 EUR RATE 1.0921``.
_FX_LINE_RE = re.compile(
    r"FOREIGN\s+AMOUNT\s+([\d,]+\.\d{2})\s+([A-Z]{3})\s+RATE\s+([\d.]+)", re.IGNORECASE
)

_ACCOUNT_MASK_RE = re.compile(r"account\s*(?:number|no\.?|#)?\s*[:\-]?\s*[Xx\*]{2,}(\d{4})\b")

_DATE_FORMATS = ("%m/%d",)


class LayoutDBankParser:
    """Bank-statement / debit-card statement parser (layout D)."""

    id = "layout_d_bank"
    version = "1.0.0"
    account_type = "checking"

    def detect(self, doc: ExtractedDoc) -> float:
        """Score confidence per §2c: 0.9 for the debit/credit + balance column combination, 0.6
        for the debit/credit pair alone, 0.0 otherwise. Never raises."""
        try:
            text = doc.full_text
        except Exception:
            return 0.0
        if not text:
            return 0.0
        has_pair = bool(_HEADER_ROW_RE.search(text))
        if not has_pair:
            return 0.0
        has_balance = bool(re.search(r"\bbalance\b", text, re.IGNORECASE))
        return 0.9 if has_balance else 0.6

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        """Parse ``doc`` into a `ParsedStatement`.

        Raises:
            ParserError: the summary block (period + balances) could not be read — this layout's
                dates carry no year, so the period is required to infer one (§2c); or a data row
                printed a value in both the debit and the credit column.
        """
        summary = extract_period_and_balances(doc)
        if (
            summary.period_start is None
            or summary.period_end is None
            or summary.opening_balance_minor is None
            or summary.closing_balance_minor is None
        ):
            raise ParserError(
                "layout_d_bank: could not read the statement period and opening/closing "
                "balances from the summary block; dates on this layout carry no year and "
                "cannot be inferred without the period"
            )
        period_start, period_end = summary.period_start, summary.period_end

        account_type = (
            "savings" if re.search(r"\bsavings\b", doc.full_text, re.IGNORECASE) else "checking"
        )
        mask_match = _ACCOUNT_MASK_RE.search(doc.full_text)
        account_hint = AccountHint(
            account_type=account_type,  # type: ignore[arg-type]
            mask=mask_match.group(1) if mask_match else None,
            currency="USD",
        )

        transactions, warnings = self._parse_rows(
            doc, period_start, period_end, summary.opening_balance_minor
        )

        return ParsedStatement(
            account_hint=account_hint,
            period_start=period_start,
            period_end=period_end,
            stated_total_minor=None,
            transactions=tuple(transactions),
            section_totals=(),
            warnings=tuple(warnings),
            opening_balance_minor=summary.opening_balance_minor,
            closing_balance_minor=summary.closing_balance_minor,
        )

    def _parse_rows(
        self,
        doc: ExtractedDoc,
        period_start: date,
        period_end: date,
        opening_balance_minor: int | None,
    ) -> tuple[list[RawTransaction], list[str]]:
        rows: list[Row] = []
        for page in doc.pages:
            rows.extend(cluster_rows(page.words))

        anchors = _locate_column_anchors(rows)
        if anchors is None:
            raise ParserError(
                "layout_d_bank: could not locate the Date/Withdrawals-Debits/Deposits-Credits "
                "column header row"
            )

        transactions: list[RawTransaction] = []
        warnings: list[str] = []
        section: str | None = None
        expected_balance: int | None = opening_balance_minor

        for row in rows:
            text = row.text.strip()
            if not text or _HEADER_ROW_RE.search(text):
                continue

            heading = self._match_section(text)
            if heading is not None:
                section = heading
                continue

            fx_match = _FX_LINE_RE.search(text)
            if fx_match is not None and transactions:
                magnitude = round(float(fx_match.group(1).replace(",", "")) * 100)
                transactions[-1] = self._with_fx(
                    transactions[-1],
                    fx_amount_minor=magnitude,
                    fx_currency=fx_match.group(2).upper(),
                    fx_rate=float(fx_match.group(3)),
                )
                continue

            date_partial = parse_date(row.words[0].text, _DATE_FORMATS) if row.words else None
            if date_partial is None:
                # Continuation of the previous transaction's multi-line description.
                if transactions and text:
                    transactions[-1] = self._append_description(transactions[-1], text)
                continue

            posted_date = infer_year(date_partial, period_start, period_end)
            amount_minor, balance_minor, description = _read_amount_and_balance(row, anchors)

            if section is None:
                raise ParserError(
                    f"layout_d_bank: data row before any recognized section heading: {text!r}"
                )
            kind_hint = self._kind_hint(section, description)

            transactions.append(
                RawTransaction(
                    posted_date=posted_date,
                    transaction_date=None,
                    description=description,
                    amount_minor=amount_minor,
                    currency="USD",
                    kind_hint=kind_hint,
                    section=section,
                )
            )

            if balance_minor is not None:
                if (
                    expected_balance is not None
                    and expected_balance - amount_minor != balance_minor
                ):
                    warnings.append(
                        f"running balance break at {posted_date.isoformat()} "
                        f"{description!r}: expected {expected_balance - amount_minor}, "
                        f"printed {balance_minor}"
                    )
                expected_balance = balance_minor
            elif expected_balance is not None:
                expected_balance -= amount_minor

        return transactions, warnings

    @staticmethod
    def _match_section(text: str) -> str | None:
        for label, pattern in _SECTION_HEADING_RE.items():
            if pattern.match(text):
                return label
        return None

    @staticmethod
    def _kind_hint(section: str, description: str) -> KindHint:
        if looks_like_card_payment(description):
            return "transfer"
        base: Kind = _SECTION_KIND[section]  # type: ignore[assignment]
        if section == "deposits and additions" and _REFUND_RE.search(description):
            return "refund"
        return base

    @staticmethod
    def _with_fx(
        txn: RawTransaction, *, fx_amount_minor: int, fx_currency: str, fx_rate: float
    ) -> RawTransaction:
        return RawTransaction(
            posted_date=txn.posted_date,
            transaction_date=txn.transaction_date,
            description=txn.description,
            amount_minor=txn.amount_minor,
            currency=txn.currency,
            fx_amount_minor=fx_amount_minor,
            fx_currency=fx_currency,
            fx_rate=fx_rate,
            kind_hint=txn.kind_hint,
            section=txn.section,
            issuer_category=txn.issuer_category,
        )

    @staticmethod
    def _append_description(txn: RawTransaction, extra: str) -> RawTransaction:
        return RawTransaction(
            posted_date=txn.posted_date,
            transaction_date=txn.transaction_date,
            description=f"{txn.description} {extra}".strip(),
            amount_minor=txn.amount_minor,
            currency=txn.currency,
            fx_amount_minor=txn.fx_amount_minor,
            fx_currency=txn.fx_currency,
            fx_rate=txn.fx_rate,
            kind_hint=txn.kind_hint,
            section=txn.section,
            issuer_category=txn.issuer_category,
        )


#: (debit_x, credit_x, balance_x) — the column header's word x0 positions, or `balance_x = None`
#: when the layout has no balance column (detect()'s 0.6 case).
_ColumnAnchors = tuple[float, float, float | None]


def _locate_column_anchors(rows: list[Row]) -> _ColumnAnchors | None:
    """Find the column-header row and return its Withdrawals/Debits, Deposits/Credits and
    Balance word x0 positions, so a data row's trailing money tokens can be assigned to the
    right column by nearest-anchor distance (I5: the column decides the sign)."""
    for row in rows:
        if not _HEADER_ROW_RE.search(row.text):
            continue
        debit_x: float | None = None
        credit_x: float | None = None
        balance_x: float | None = None
        for word in row.words:
            low = word.text.lower()
            if debit_x is None and ("withdrawal" in low or "debit" in low):
                debit_x = word.x0
            elif credit_x is None and ("deposit" in low or "credit" in low):
                credit_x = word.x0
            elif balance_x is None and "balance" in low:
                balance_x = word.x0
        if debit_x is not None and credit_x is not None:
            return debit_x, credit_x, balance_x
    return None


def _classify_column(x0: float, anchors: _ColumnAnchors) -> str:
    debit_x, credit_x, balance_x = anchors
    candidates: list[tuple[str, float]] = [("debit", debit_x), ("credit", credit_x)]
    if balance_x is not None:
        candidates.append(("balance", balance_x))
    return min(candidates, key=lambda c: abs(x0 - c[1]))[0]


#: This layout always prints amounts with cents (``1,234.56``, ``12.00``, ``(12.00)``). A bare
#: integer such as the ``1234`` in ``... ending in 1234``, ``STORE 4471`` or ``CHECK 1052`` is
#: description text, not money, even though `parse_money` (shared with layouts that print whole
#: amounts) would accept it.
_CENTS_RE = re.compile(r"\.\d{2}\D*$")


def _is_amount_token(text: str) -> bool:
    return _CENTS_RE.search(text) is not None and parse_money(text) is not None


def _read_amount_and_balance(row: Row, anchors: _ColumnAnchors) -> tuple[int, int | None, str]:
    """Split a data row into its signed amount (the column decides the sign, per I5), an
    optional printed balance, and the description text between the date and the trailing money
    tokens.

    Raises:
        ParserError: the row has no trailing amount, more than two trailing money tokens, or
            prints a value in both the debit and the credit column.
    """
    words = list(row.words)
    money_tokens: list[tuple[int, str]] = []  # (word index, text), rightmost first
    for idx in range(len(words) - 1, 0, -1):  # never treat the date (index 0) as money
        if _is_amount_token(words[idx].text):
            money_tokens.append((idx, words[idx].text))
        else:
            break
    money_tokens.reverse()  # left-to-right order among the trailing money tokens

    if not money_tokens:
        raise ParserError(f"layout_d_bank: data row has no amount: {row.text!r}")
    if len(money_tokens) > 2:
        raise ParserError(f"layout_d_bank: too many trailing amounts on row: {row.text!r}")

    description_end = money_tokens[0][0]
    description = " ".join(w.text for w in words[1:description_end])

    classified = [(_classify_column(words[idx].x0, anchors), text) for idx, text in money_tokens]

    if len(classified) == 1:
        column, text = classified[0]
        if column == "balance":
            raise ParserError(f"layout_d_bank: row has a balance but no amount: {row.text!r}")
        return _signed_amount(text, column), None, description

    labels = {column for column, _ in classified}
    if "debit" in labels and "credit" in labels:
        raise ParserError(
            f"layout_d_bank: row prints a value in both the debit and credit column: {row.text!r}"
        )
    if "balance" not in labels:
        raise ParserError(f"layout_d_bank: could not classify amounts on row: {row.text!r}")

    amount_column, amount_text = next(c for c in classified if c[0] != "balance")
    _, balance_text = next(c for c in classified if c[0] == "balance")
    balance_parsed = parse_money(balance_text)
    assert balance_parsed is not None  # already verified by parse_money above
    balance_minor = (
        -balance_parsed.magnitude_minor
        if balance_parsed.printed_negative
        else balance_parsed.magnitude_minor
    )
    return _signed_amount(amount_text, amount_column), balance_minor, description


def _signed_amount(text: str, column: str) -> int:
    """Map a printed amount to signed minor units by column (I5): debit -> positive (outflow),
    credit -> negative (inflow). The printed sign, if any, is ignored — this layout decides sign
    by column, not by print (§2c)."""
    parsed = parse_money(text)
    assert parsed is not None  # caller already verified this via parse_money
    return parsed.magnitude_minor if column == "debit" else -parsed.magnitude_minor


#: Module-level singleton, discovered by the registry's module scan (P1-B).
PARSER = LayoutDBankParser()
