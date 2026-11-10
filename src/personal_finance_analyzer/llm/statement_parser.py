"""The LLM statement parser: the user's opt-in alternative to the template/layout parsers.

The selected pages' text layer (redacted, see `redact`) is sent to the configured LLM endpoint,
which answers with a JSON object of statement metadata plus one entry per transaction. This module
and `classify/llm/egress.py` are the only ones allowed to import `personal_finance_analyzer.llm.client`
(I2/I3); `ingest/` reaches this module only via a lazy import in `pipeline._resolve_parser`.

Sign convention (I5): ``direction == "outflow"`` -> positive minor units, ``"inflow"`` -> negative.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from personal_finance_analyzer.config import LLMConfig, load_settings
from personal_finance_analyzer.core.errors import LLMError, ParserError
from personal_finance_analyzer.core.pii import compile_redactor
from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    KindHint,
    ParsedStatement,
    RawTransaction,
)
from personal_finance_analyzer.llm import client

LLM_PARSER_ID = "llm"
PROMPT_VERSION = "2026-10-25.1"

#: How much of the first selected page is repeated as context in every later request.
CONTEXT_HEADER_CHARS = 1500

_KINDS = frozenset({"purchase", "refund", "payment", "transfer", "fee", "interest", "adjustment"})
_ACCOUNT_TYPES = frozenset({"credit", "checking", "savings"})

SYSTEM_PROMPT = """\
You extract structured data from the text of a bank or credit-card statement. The text comes \
from a PDF text layer, so columns may be misaligned, wrapped or reordered. Reply with ONE JSON \
object and nothing else (no prose, no markdown fences), shaped exactly like:

{
  "account_type": "credit" | "checking" | "savings" | null,
  "currency": ISO 4217 code or null,
  "account_last4": last four digits of the account/card number or null,
  "period_start": "YYYY-MM-DD" or null,
  "period_end": "YYYY-MM-DD" or null,
  "opening_balance": decimal string or null,
  "closing_balance": decimal string or null,
  "stated_total": decimal string or null,
  "transactions": [
    {
      "posted_date": "YYYY-MM-DD",
      "transaction_date": "YYYY-MM-DD" or null,
      "description": merchant/payee text exactly as printed,
      "amount": positive decimal string without currency symbols or thousands separators,
      "direction": "outflow" | "inflow",
      "kind_hint": "purchase" | "refund" | "payment" | "transfer" | "fee" | "interest" \
| "adjustment" | null,
      "section": the statement section heading the row appears under, or null,
      "currency": ISO code if it differs from the statement currency, else null
    }
  ]
}

Rules:
- direction "outflow" is money leaving the user: purchases, fees, interest charges, transfers \
out, withdrawals. direction "inflow" is money coming back or in: refunds, credits, payments made \
to a credit card, deposits, transfers in.
- amount is always the absolute value; direction carries the sign.
- Dates that omit the year: infer it from the statement period (a statement spanning December \
to January uses the earlier year for December rows). Always output full YYYY-MM-DD.
- opening_balance / closing_balance are as printed (a credit balance on a card is negative). \
stated_total is the statement's own total of new charges / total debits, if one is printed.
- description is the merchant text only, verbatim, without the date or amount columns.
- Only extract individual transactions. Skip summary rows, subtotals, totals, running balances, \
page headers/footers, interest-rate tables, rewards summaries, minimum-payment boxes and \
advertisements.
- Do not invent rows. If a value is not present, use null. If there are no transactions, return \
an empty list.
- Some requests contain only part of a statement; a header marked "context only" is there to \
help you read dates and currency and must not be mined for transactions.
"""


class LLMTransaction(BaseModel):
    model_config = ConfigDict(extra="ignore")

    posted_date: str | None = None
    transaction_date: str | None = None
    description: str
    amount: str | float | int
    direction: Literal["outflow", "inflow"]
    kind_hint: str | None = None
    section: str | None = None
    currency: str | None = None


class LLMStatement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    account_type: Literal["credit", "checking", "savings"] | None = None
    currency: str | None = None
    account_last4: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    opening_balance: str | float | int | None = None
    closing_balance: str | float | int | None = None
    stated_total: str | float | int | None = None
    transactions: list[LLMTransaction] = []


# ------------------------------------------------------------------------------------------------
# Redaction of the outgoing page text
# ------------------------------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
#: Groups of 3+ digits joined by single spaces/dashes (card and account numbers). Two-digit
#: groups never match, so ISO dates (2026-01-15) and amounts (1234.56) are left alone.
_LONG_NUMBER_RE = re.compile(r"(?<![\d.,])(?<!\d[ -])\d{3,}(?:[ -]\d{3,})*(?!\d|\.\d)")


def _mask_number(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) < 8:
        return match.group(0)
    return "XXXX" + digits[-4:]


def redact(text: str, pii_terms: Sequence[str] = ()) -> str:
    """Mask e-mail addresses whole (so a name entry cannot leave `[NAME]@host` behind), remove the
    user's personal-info entries (names, phone and account numbers in any format), and only then
    mask remaining account/card numbers (keeping the last 4 digits). Amounts and dates are
    deliberately left intact. User entries run before the generic number rule so a phone or
    account entry is removed whole instead of being half-masked (XXXX + last 4)."""
    text = _EMAIL_RE.sub("[email]", text)
    text, _ = compile_redactor(pii_terms).redact(text)
    return _LONG_NUMBER_RE.sub(_mask_number, text)


def count_redactions(doc: ExtractedDoc, pii_terms: Sequence[str] = ()) -> int:
    """How many replacements the user's personal-info entries make across `doc`'s pages (each
    page counted once, however many requests repeat the header). Pure; for the preview."""
    redactor = compile_redactor(pii_terms)
    return sum(redactor.redact(page.text)[1] for page in doc.pages)


def build_requests(doc: ExtractedDoc, cfg: LLMConfig, pii_terms: Sequence[str] = ()) -> list[str]:
    """The user-message texts to send, one per request (chunked by
    `cfg.parse_pages_per_request`; 0 = everything in one request). Pure; no network."""
    pages = list(doc.pages)
    if not pages:
        return []
    size = cfg.parse_pages_per_request if cfg.parse_pages_per_request > 0 else len(pages)
    header = redact(pages[0].text[:CONTEXT_HEADER_CHARS], pii_terms)
    requests: list[str] = []
    for index in range(0, len(pages), size):
        chunk = pages[index : index + size]
        body = "\n\n".join(
            f"=== PAGE {p.page_number} ===\n{redact(p.text, pii_terms)}" for p in chunk
        )
        if index == 0:
            requests.append(body)
        else:
            requests.append(
                "[Statement header from the first page -- context only, do not extract "
                f"transactions from this header]\n{header}\n[end of context]\n\n{body}"
            )
    return requests


def preview(doc: ExtractedDoc, cfg: LLMConfig, pii_terms: Sequence[str] = ()) -> list[str]:
    """Exactly what would be sent (after redaction), without sending it."""
    return build_requests(doc, cfg, pii_terms)


# ------------------------------------------------------------------------------------------------
# Conversion
# ------------------------------------------------------------------------------------------------


def _to_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _to_minor(number: Decimal) -> int:
    return int((number * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y")


def _parse_date(value: str | None, anchor: date | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Year-less "MM-DD" / "MM/DD": take the year from the statement period.
    m = re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", text)
    if m and anchor is not None:
        month, day = int(m.group(1)), int(m.group(2))
        try:
            candidate = date(anchor.year, month, day)
        except ValueError:
            return None
        if candidate > anchor + timedelta(days=31):
            candidate = date(anchor.year - 1, month, day)
        return candidate
    return None


class LLMStatementParser:
    """`StatementParser` backed by the configured LLM endpoint."""

    id = LLM_PARSER_ID
    version = f"llm-{PROMPT_VERSION}"
    account_type = "credit"

    def __init__(self, cfg: LLMConfig, pii_terms: Sequence[str] = ()) -> None:
        self._cfg = cfg
        self._pii_terms = tuple(pii_terms)

    def detect(self, doc: ExtractedDoc) -> float:
        return 0.0

    def preview(self, doc: ExtractedDoc) -> list[str]:
        return preview(doc, self._cfg, self._pii_terms)

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        cfg = self._cfg
        if not cfg.is_configured:
            raise ParserError(
                "The LLM statement parser needs an LLM endpoint. Configure one in Settings "
                "(LLM) or with `pfa llm set`, then try again."
            )
        requests = build_requests(doc, cfg, self._pii_terms)
        if not requests:
            raise ParserError("No pages to send to the LLM.")

        chunks: list[dict[str, Any]] = []
        schema = LLMStatement.model_json_schema()
        for user_text in requests:
            try:
                response = client.complete(
                    cfg,
                    system=SYSTEM_PROMPT,
                    user=user_text,
                    json_schema=schema,
                    schema_name="statement",
                )
                data = client.extract_json(response.text)
            except LLMError as exc:
                hint = ""
                if "timed out" in str(exc):
                    hint = (
                        " -- raise Timeout in Settings -> LLM (Advanced), or lower 'Pages per "
                        "request' so each request is smaller, then import again"
                    )
                raise ParserError(f"The LLM could not parse this statement: {exc}{hint}") from exc
            if isinstance(data, list):  # a bare list of transactions
                data = {"transactions": data}
            if not isinstance(data, dict):
                raise ParserError("The LLM returned something other than a JSON object.")
            chunks.append(cast(dict[str, Any], data))
        return self._merge(chunks)

    # --------------------------------------------------------------------------------------

    def _merge(self, chunks: list[dict[str, Any]]) -> ParsedStatement:
        meta: dict[str, Any] = {}
        raw_rows: list[Any] = []
        for chunk in chunks:
            for key in (
                "account_type",
                "currency",
                "account_last4",
                "period_start",
                "period_end",
                "opening_balance",
                "closing_balance",
                "stated_total",
            ):
                if meta.get(key) in (None, "") and chunk.get(key) not in (None, ""):
                    meta[key] = chunk[key]
            rows = chunk.get("transactions")
            if isinstance(rows, list):
                raw_rows.extend(rows)

        account_type = meta.get("account_type")
        if account_type not in _ACCOUNT_TYPES:
            account_type = None
        period_start = _parse_date(_s(meta.get("period_start")), None)
        period_end = _parse_date(_s(meta.get("period_end")), None)
        anchor = period_end or period_start
        default_currency = load_settings().ingest.default_currency
        currency = (_s(meta.get("currency")) or default_currency).upper()

        transactions: list[RawTransaction] = []
        skipped = 0
        for raw in raw_rows:
            txn = self._convert_row(raw, anchor, currency)
            if txn is None:
                skipped += 1
            else:
                transactions.append(txn)

        warnings: tuple[str, ...] = ()
        if not transactions and not skipped:
            # A statement with no activity is legitimate: import it as 0 transactions.
            warnings = ("The LLM found no transactions in the selected pages",)
        if skipped:
            warnings += (f"{skipped} row(s) returned by the LLM were invalid and skipped",)

        digits = re.sub(r"\D", "", _s(meta.get("account_last4")) or "")
        mask = digits[-4:] if digits else None

        def money(key: str) -> int | None:
            number = _to_decimal(meta.get(key))
            return _to_minor(number) if number is not None else None

        return ParsedStatement(
            account_hint=AccountHint(account_type=account_type, mask=mask, currency=currency),
            period_start=period_start,
            period_end=period_end,
            stated_total_minor=money("stated_total"),
            transactions=tuple(transactions),
            warnings=warnings,
            opening_balance_minor=money("opening_balance"),
            closing_balance_minor=money("closing_balance"),
        )

    @staticmethod
    def _convert_row(raw: Any, anchor: date | None, currency: str) -> RawTransaction | None:
        try:
            row = LLMTransaction.model_validate(raw)
        except ValidationError:
            return None
        amount = _to_decimal(row.amount)
        description = row.description.strip()
        if amount is None or not description:
            return None
        posted = _parse_date(row.posted_date, anchor)
        txn_date = _parse_date(row.transaction_date, anchor)
        if posted is None:
            posted = txn_date
        if posted is None:
            return None
        minor = _to_minor(abs(amount))
        if row.direction == "inflow":
            minor = -minor
        hint = row.kind_hint.strip().lower() if row.kind_hint else None
        kind_hint = cast(KindHint, hint) if hint in _KINDS else None
        return RawTransaction(
            posted_date=posted,
            transaction_date=txn_date,
            description=description,
            amount_minor=minor,
            currency=(row.currency or currency).upper(),
            kind_hint=kind_hint,
            section=(row.section or None),
        )


def _s(value: object) -> str | None:
    return str(value).strip() if value not in (None, "") else None
