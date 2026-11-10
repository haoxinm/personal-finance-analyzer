"""Form 1099-B (Proceeds From Broker and Barter Exchange Transactions) parser, id ``tax_1099_b``.

Software-style layout: label and value share a visual row. Two table shapes are understood and may
both be present:

* a summary table, one row per term/category::

      Short-term (covered)   <proceeds>  <cost basis>  <wash sale adj>  <net gain/loss>

* transaction detail rows under a ``Short-term transactions ...`` / ``Long-term transactions ...``
  heading::

      <description>  <date acquired>  <date sold>  <proceeds>  <cost>  <wash adj>  <gain/loss>

Emitted (plan 2.5): net short-term gain/loss -> ``short_term_capital_gain``, net long-term ->
``long_term_capital_gain`` (both may be negative, D6); an "ordinary/undetermined" term becomes
``other`` (only when positive) with a warning. Per term the summary rows win; the detail rows are
used when no summary row exists. Box 4 federal withholding and a ``State XX State income tax
withheld`` row become tax items (only when greater than zero). Proceeds and cost are never stored.

Warnings (fixed text, no document content): ``proceeds - cost + wash adjustment != printed gain``
per term, and detail rows that do not add up to the summary.

`parse_b_section(block, year)` parses ONE 1099-B block and is reused by the consolidated parser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import (
    FormType,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
)
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.layout import Row, cluster_rows, parse_money
from personal_finance_analyzer.ingest.tax.common import (
    build_document,
    collect,
    halve_if_other_1099,
    require_year,
    sanitize_payer_name,
)
from personal_finance_analyzer.ingest.tax.fields import (
    compile_label,
    corrected_marker,
    merge_docs,
    money_for_label,
    signed,
    split_blocks,
    text_for_label,
)
from personal_finance_analyzer.ingest.tax.parsers._boxes import without_continuations
from personal_finance_analyzer.ingest.tax.tin import mask_tin

_TITLE_RE = re.compile(r"\bForm\s*1099-B\b")
_FORM_START_RE = re.compile(r"^\s*Form\s*1099-B\b", re.IGNORECASE)
_PROCEEDS_RE = re.compile(r"\bProceeds\b", re.IGNORECASE)
_BASIS_RE = re.compile(r"\b(?:Cost\s+basis|Wash\s+sale|Gain/Loss|Net\s+gain)", re.IGNORECASE)
_RETURN_RE = re.compile(r"U\.S\.\s+Individual\s+Income\s+Tax\s+Return", re.IGNORECASE)

_PAYER_NAME_RE = compile_label(r"^Payer(?:'s)?\s+name\b:?")
_PAYER_TIN_RE = compile_label(r"^Payer(?:'s)?\s+TIN\b:?")
_ACCOUNT_RE = compile_label(r"^Account\s+number\b:?")
_FED_WITHHELD_RE = compile_label(r"^Federal\s+income\s+tax\s+withheld\b")

_TERM_START = re.compile(
    r"^(?:detail:\s*)?(short|long)[-\s]?term\b|^(?:detail:\s*)?(ordinary|undetermined)\b", re.I
)
_OTHER_HEADING = re.compile(r"^other\s+transactions\b", re.I)
_STATE_ROW = re.compile(r"^State\s+([A-Z]{2})\s+State\s+income\s+tax\s+withheld\b")
_DATE_TOKEN = re.compile(r"^(?:\d{1,2}/\d{1,2}/\d{2,4}|various|inherited)$", re.IGNORECASE)
#: A bracketed or parenthesized footnote / adjustment code: ``(W)``, ``[1]``, ``(A1)``.
_FOOTNOTE = re.compile(r"^[(\[][A-Za-z0-9]{1,3}[)\]]$")
_SUPERSCRIPTS = "\u00b9\u00b2\u00b3\u2070\u2074\u2075\u2076\u2077\u2078\u2079"
_TRAILING_MARKS = "*\u2020\u2021" + _SUPERSCRIPTS
#: A bracketed footnote glued to an amount: ``100.00[1]``.
_GLUED_NOTE = re.compile(r"(?:\[[A-Za-z0-9]{1,2}\])+$")
#: A short code printed in a trade's tail (adjustment code, footnote letter, marker): a bare one or
#: two letters (``W``, ``D``, ``X``, ``e``), a run of marks (``*``, a dagger, superscript digits).
_CODE_TOKEN = re.compile(rf"^(?:[A-Za-z]{{1,2}}|[*\u2020\u2021#{_SUPERSCRIPTS}]+)$")
#: A heading of a supplementary table printed after the 1099-B one (amortization / accretion
#: recap, year-to-date and life-to-date columns): its rows are never trades.
_SUPPLEMENTARY_RE = re.compile(
    r"amortization|accretion|year[-\s]to[-\s]date|life[-\s]to[-\s]date"
    r"|realized\s+cost\s+basis\s+information",
    re.I,
)
_NOT_COMPUTED = re.compile(r"^\(?(?:N/?C|N/?A|--+)\)?$", re.I)
#: Whitespace the extractor left inside a parenthesized amount: ``( 12.34 )`` or ``(12.34 )``.
_OPEN_GAP = re.compile(r"\(\s+(?=[\d$.])")
_CLOSE_GAP = re.compile(r"(?<=[\d.])\s+\)")
#: A term total printed by the broker: "NET SHORT TERM CAPITAL GAINS AND LOSSES  <amounts>".
_NET_RE = re.compile(r"^NET\s+(short|long)[-\s]?term\b", re.I)
#: A covered / noncovered category subtotal: "Covered Short Term ... Subtotal  <amounts>".
_CATEGORY_SUBTOTAL_RE = re.compile(
    r"^(?:non-?covered|covered)\s+(short|long)[-\s]?term\b.*\bsubtotal\b", re.I
)
#: A one-figure recap: "COVERED SHORT TERM GAINS/LOSSES  <amount>".
_RECAP_RE = re.compile(
    r"^(?:non-?covered|covered)\s+(short|long)[-\s]?term\s+(?:capital\s+)?gains?\b", re.I
)

_GRAND_TOTAL_RE = re.compile(
    r"^(?:sales\s+proceeds\s+and\s+net\s+gains|grand\s+total|total\s+(?:proceeds|sales))", re.I
)

_CENT_TOLERANCE = 1

_TERM_LABEL = {"short": "short-term", "long": "long-term", "other": "ordinary or undetermined-term"}


@dataclass(slots=True)
class _Totals:
    proceeds: int = 0
    cost: int = 0
    wash: int = 0
    net: int = 0
    rows: int = 0
    reconciles: bool = True

    def add(self, proceeds: int, cost: int, wash: int, net: int) -> None:
        self.proceeds += proceeds
        self.cost += cost
        self.wash += wash
        self.net += net
        self.rows += 1
        if abs(proceeds - cost + wash - net) > _CENT_TOLERANCE:
            self.reconciles = False


@dataclass(slots=True)
class _Scan:
    """What a 1099-B block prints, per term, from each kind of source. Exactly ONE source is used
    per term (see `parse_b_section`), so subtotals, term totals and the trades they summarise are
    never added together."""

    summary: dict[str, _Totals] = field(default_factory=dict)  # "Short-term  P C W G" rows
    detail: dict[str, _Totals] = field(default_factory=dict)  # one row per trade
    net_lines: dict[str, int] = field(default_factory=dict)  # "NET SHORT TERM ..." totals
    category_subtotals: dict[str, int] = field(default_factory=dict)  # covered + noncovered
    recaps: dict[str, int] = field(default_factory=dict)  # one-figure recap rows
    #: term -> printed label -> value. A total line printed twice (summary, then after the
    #: detail) is one figure (the last one printed); distinct labels are summed in `_scan`.
    labelled: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    grand_total: bool = False  # the whole-statement total row was seen: the table is finished
    unreadable_rows: bool = False
    not_computed_rows: bool = False  # trades printed with "N/C" in place of a gain


def _term_key(match: re.Match[str]) -> str:
    return "other" if match.group(2) else match.group(1).lower()


def _rows(block: ExtractedDoc) -> list[Row]:
    return [row for page in block.pages for row in cluster_rows(page.words)]


def _tail_tokens(words: list[str]) -> list[str]:
    """Tokens after the last date, with a parenthesized amount that the extractor split across
    words (``(12.34`` + ``)``) put back together."""
    joined = _CLOSE_GAP.sub(")", _OPEN_GAP.sub("(", " ".join(words)))
    return joined.split()


def _trade_tail(words: list[str], last_date: int) -> list[int] | str | None:
    """Signed amounts after a trade row's last date: a list; ``"not_computed"`` when the gain is
    printed as N/C; ``None`` when the row is not a trade row (a word such as "Dividend" follows
    the date); ``"unreadable"`` when it looks like a trade but has too few amounts or a token
    that is neither an amount nor a short code. Codes (``W``, ``(W)``, ``[1]``, ``*``, ...) are
    ignored wherever they sit in the tail."""
    amounts: list[int] = []
    for token in _tail_tokens(words[last_date + 1 :]):
        if _FOOTNOTE.match(token):
            continue
        if _NOT_COMPUTED.match(token):
            return "not_computed"
        money = parse_money(_GLUED_NOTE.sub("", token.rstrip(_TRAILING_MARKS)) or token)
        if money is None:
            if _CODE_TOKEN.match(token) and (amounts or not token.islower() or len(token) == 1):
                continue  # an adjustment code such as W, D, X, e or a marker
            if not amounts:
                return None  # a word right after the dates: not a trade row
            return "unreadable"
        amounts.append(-money.magnitude_minor if money.printed_negative else money.magnitude_minor)
    if not amounts:
        return None
    return amounts if len(amounts) >= 3 else "unreadable"


def _scan(block: ExtractedDoc) -> _Scan:
    scan = _Scan()
    current: str | None = None
    for row in _rows(block):
        words = [w.text for w in row.words]
        dates = [i for i, text in enumerate(words) if _DATE_TOKEN.match(text)]
        if not dates and _SUPPLEMENTARY_RE.search(row.text):
            current = None  # a supplementary table: no row below it is a trade
            continue
        if not dates:
            money = [m for m in (parse_money(t) for t in words) if m is not None]
            start = _TERM_START.match(row.text)
            if start is not None and len(money) >= 4:
                key = _term_key(start)
                nums = [signed(m) for m in money[-4:]]
                p, c, w, n = (0 if v is None else v for v in nums)
                scan.summary.setdefault(key, _Totals()).add(p, c, w, n)
                continue
            if start is not None:
                current = _term_key(start)
                continue
            if _OTHER_HEADING.match(row.text) and not money:
                current = "other"
                continue
            if not money:
                continue
            last = signed(money[-1]) or 0
            if _GRAND_TOTAL_RE.match(row.text):
                scan.grand_total = True
                current = None  # the transaction table is finished
                continue
            net = _NET_RE.match(row.text)
            if net is not None:
                key = net.group(1).lower()
                _record(scan, "net", key, row, last)
                continue
            sub = _CATEGORY_SUBTOTAL_RE.match(row.text)
            if sub is not None:
                key = sub.group(1).lower()
                _record(scan, "sub", key, row, last)
                continue
            recap = _RECAP_RE.match(row.text)
            if recap is not None and len(money) == 1:
                key = recap.group(1).lower()
                _record(scan, "recap", key, row, last)
            continue
        if current is None:
            continue
        if len(dates) < 2:
            continue  # a trade has an acquired and a sold date; one date is another kind of row
        tail = _trade_tail(words, dates[-1])
        if tail is None:
            continue
        if tail == "not_computed":
            scan.not_computed_rows = True
            continue
        if tail == "unreadable" or isinstance(tail, str):
            scan.unreadable_rows = True
            continue
        proceeds, cost, wash, gain = tail[0], tail[1], (tail[-2] if len(tail) >= 4 else 0), tail[-1]
        scan.detail.setdefault(current, _Totals()).add(proceeds, cost, wash, gain)
    for kind, target in (
        ("net", scan.net_lines),
        ("sub", scan.category_subtotals),
        ("recap", scan.recaps),
    ):
        for key, by_label in scan.labelled.get(kind, {}).items():
            target[key] = sum(by_label.values())
    return scan


def _record(scan: _Scan, kind: str, key: str, row: Row, value: int) -> None:
    label = " ".join(w.text.lower() for w in row.words if parse_money(w.text) is None)
    scan.labelled.setdefault(kind, {}).setdefault(key, {})[label] = value


def _row_money_last(row: Row) -> int | None:
    values = [m for m in (parse_money(w.text) for w in row.words) if m is not None]
    return values[-1].magnitude_minor if values else None


def _identity(block: ExtractedDoc) -> tuple[str | None, str | None, str | None]:
    name = sanitize_payer_name(text_for_label(block, _PAYER_NAME_RE, mode="same_row"))
    tin = mask_tin(text_for_label(block, _PAYER_TIN_RE, mode="same_row"))
    account = mask_tin(text_for_label(block, _ACCOUNT_RE, mode="same_row"))
    return name, tin, account


def parse_b_section(block: ExtractedDoc, year: int) -> ParsedTaxForm:
    """Parse one 1099-B block (a whole form, or a section of a consolidated document).

    Payer name / TIN last four / account last four are filled when the block prints them (else
    ``None``; the consolidated parser overrides them). Income items are net gain or loss per term
    (`ParsedIncomeItem.amount_minor` may be negative, D6); tax items are withholding.

    Raises:
        ParserError: the block has neither a summary row nor any readable transaction row
            (generic message, no document text).
    """
    scan = _scan(block)
    if not (
        scan.summary or scan.detail or scan.net_lines or scan.category_subtotals or scan.recaps
    ):
        raise ParserError("No 1099-B transaction totals were found in this form.")
    warnings: list[str] = []
    income: list[ParsedIncomeItem] = []
    for key in ("short", "long", "other"):
        summary, detail = scan.summary.get(key), scan.detail.get(key)
        label = _TERM_LABEL[key]
        net: int | None
        if summary is not None:
            net = summary.net
            chosen: _Totals | None = summary
        elif key != "other" and key in scan.net_lines:
            net, chosen = scan.net_lines[key], None
        elif key != "other" and key in scan.category_subtotals:
            net, chosen = scan.category_subtotals[key], None
        elif key != "other" and key in scan.recaps:
            net, chosen = scan.recaps[key], None
        elif detail is not None:
            net, chosen = detail.net, detail
        else:
            continue
        if chosen is not None and not chosen.reconciles:
            warnings.append(
                f"1099-B {label} figures do not reconcile: proceeds less cost basis plus wash "
                "sale adjustment differs from the printed gain or loss."
            )
        if (
            detail is not None
            and chosen is not detail
            and not scan.not_computed_rows
            and not scan.unreadable_rows
            and abs(net - detail.net) > _CENT_TOLERANCE
        ):
            warnings.append(f"1099-B {label} transaction rows do not add up to the summary.")
        if key == "short":
            income.append(
                ParsedIncomeItem(
                    "short_term_capital_gain",
                    net,
                    "1099-B",
                    "ST",
                    "Net short-term gain or loss",
                )
            )
        elif key == "long":
            income.append(
                ParsedIncomeItem(
                    "long_term_capital_gain",
                    net,
                    "1099-B",
                    "LT",
                    "Net long-term gain or loss",
                )
            )
        else:
            warnings.append(
                "1099-B ordinary or undetermined-term transactions are reported as other income."
            )
            if net > 0:
                income.append(
                    ParsedIncomeItem(
                        "other", net, "1099-B", None, "Ordinary or undetermined-term gain"
                    )
                )
            elif net < 0:
                warnings.append(
                    "A 1099-B ordinary or undetermined-term loss was not recorded as income."
                )
    if scan.unreadable_rows:
        warnings.append("Some 1099-B transaction rows could not be read.")
    if scan.not_computed_rows:
        warnings.append(
            "Some 1099-B transactions have no computed gain or loss and are not included."
        )
    taxes: list[ParsedTaxItem] = []
    fed = money_for_label(block, _FED_WITHHELD_RE, mode="same_row")
    if fed is not None and fed.magnitude_minor > 0:
        taxes.append(
            ParsedTaxItem(
                "federal_income_tax_withheld",
                fed.magnitude_minor,
                "1099-B",
                "4",
                "Federal income tax withheld",
            )
        )
    for row in _rows(block):
        state = _STATE_ROW.match(row.text)
        amount = _row_money_last(row) if state else None
        if state and amount:
            taxes.append(
                ParsedTaxItem(
                    "state_income_tax_withheld",
                    amount,
                    "1099-B",
                    "16",
                    "State income tax withheld",
                    jurisdiction=state.group(1),
                )
            )
    name, tin, account = _identity(block)
    form_type: FormType = "1099-b"
    return ParsedTaxForm(
        form_type=form_type,
        tax_year=year,
        payer_name=name,
        payer_tin_last4=tin,
        account_last4=account,
        is_corrected=corrected_marker(block),
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=tuple(income),
        taxes=tuple(taxes),
        warnings=tuple(dict.fromkeys(warnings)),
    )


_IDENTITY_ROW = re.compile(r"\b(?:Payer|Recipient|Account\s+(?:number|no)|TIN)\b", re.IGNORECASE)


def _is_finished(group: list[ExtractedDoc]) -> bool:
    scan = _scan(group[0] if len(group) == 1 else merge_docs(group))
    return bool(scan.summary) or scan.grand_total


def _merge_running_headings(blocks: tuple[ExtractedDoc, ...]) -> tuple[ExtractedDoc, ...]:
    """A transaction table that spans pages repeats its ``Form 1099-B`` heading on each page. A
    block that follows an unfinished one (no summary rows and no whole-statement total yet) and
    prints no payer / recipient / account row of its own is that table's next page, not a new
    form: it is merged into the previous block."""
    merged: list[list[ExtractedDoc]] = []
    for block in blocks:
        has_identity = any(_IDENTITY_ROW.search(page.text) for page in block.pages)
        if merged and not has_identity and not _is_finished(merged[-1]):
            merged[-1].append(block)
        else:
            merged.append([block])
    return tuple(group[0] if len(group) == 1 else merge_docs(group) for group in merged)


class Form1099BParser:
    """Standalone 1099-B (one or several forms per file)."""

    id = "tax_1099_b"
    version = "1"
    form_types: tuple[FormType, ...] = ("1099-b",)

    def detect(self, doc: ExtractedDoc) -> float:
        text = doc.full_text
        if _RETURN_RE.search(text) or not _TITLE_RE.search(text):
            return 0.0
        if not (_PROCEEDS_RE.search(text) and _BASIS_RE.search(text)):
            return 0.0
        return halve_if_other_1099(0.9, doc, "B")

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        year = require_year(doc, tax_year)
        blocks = _merge_running_headings(
            split_blocks(doc, without_continuations(_FORM_START_RE)) or (doc,)
        )
        got = collect(blocks, lambda block: parse_b_section(block, year), kind="1099-B")
        return build_document(got.forms, warnings=got.warnings, unparsed=got.unparsed)


PARSER = Form1099BParser()
