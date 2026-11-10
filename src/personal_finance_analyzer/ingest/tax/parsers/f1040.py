"""Form 1040 parser (software-generated, one visual row per line), tax years 2022-2025.

Parser id ``tax_1040``; role "return" (D4). Emits income lines from the return itself (wages,
interest, dividends, capital gain, business and other income), the liability lines (total tax,
Schedule 2 items), withholding, payments and the refund/amount-owed result, plus the
ordinary-vs-preferential federal tax estimate from `ingest.tax.estimate` (``is_estimate=True``).
Names, addresses and identifiers on the return are never read.

Layout handling
---------------
* Every line is found by a **label regex** (the line number is only a display hint and a fallback),
  because 2022 / 2023+ / 2025 renumber and reword some lines. The per-year tables are
  `F1040_FIELDS[year]`; an unlisted year falls back to the latest table with a warning (form
  layout for tax year N not verified).
* Two-column rows (``2a Tax-exempt interest ... b Taxable interest ...``) are split into separate
  virtual rows before extraction, whole-dollar amounts printed with a trailing period
  (``85,000.``) are normalised, and bare 4-digit numbers that are years or form numbers are
  neutralised so they are never mistaken for amounts (`_prepare`).
* Schedule pages (D, 1, 2, C) are recognised by their heading row; fields are looked up only on
  the pages that own them, so a Schedule D "line 7" is never confused with 1040 line 7.
* An amount is the one after the right-hand repeat of the line number; on a row with inline form
  references (``16 Tax ... Check if any from Form(s): 1 [ ] 8814 2 [ ] 4972 ... 16 11,000.``) the
  form numbers are never amounts (`_row_money`).
* Filing status is the one option with a check mark (``[X] Single``, ``(X)``, a ballot glyph or a
  bare ``X`` before the label, or a bracketed mark after it); the options may be on separate rows or
  share one (``Filing Status [ ] Single [X] Married filing jointly [ ] ...``). None or several
  marked => `None` plus a warning (and no tax estimate).
* ``refund`` is line 35a (refund received), else line 34 less line 36 (applied to next year).

Units and signs: minor units; only capital gains (1040 line 7, Schedule D lines 7 and 15) may be
negative (D6). 1040-SR, 1040-NR and 1040-X are not supported (`detect` returns 0.0, `parse`
raises `UnsupportedLayoutError`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.tax_types import (
    FormType,
    IncomeCategory,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
    TaxCategory,
)
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import ParsedMoney, Row, cluster_rows, parse_money
from personal_finance_analyzer.ingest.tax import estimate
from personal_finance_analyzer.ingest.tax.common import build_document, check_warning, require_year
from personal_finance_analyzer.ingest.tax.fields import (
    LabelHit,
    compile_label,
    row_for_label,
    same_row_candidates,
    signed,
)

#: Tax years whose line layout has been verified against the IRS form.
SUPPORTED_1040_YEARS: Final[tuple[int, ...]] = (2022, 2023, 2024, 2025)

_SOURCE_FORM = "1040"
Scope = Literal["main", "d", "1", "2", "c"]


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Where a value lives: `label` matches the row text (after `_prepare`), `line` is the
    printed line number (display hint and fallback), `scope` the page group that owns it."""

    label: re.Pattern[str]
    line: str
    scope: Scope = "main"
    line_fallback: bool = False


def _f(pattern: str, line: str, scope: Scope = "main", *, line_fallback: bool = False) -> FieldSpec:
    return FieldSpec(compile_label(pattern), line, scope, line_fallback)


# Labels common to all supported years (IRS wording, shortened to the stable part).
_COMMON: Final[dict[str, FieldSpec]] = {
    "taxable_interest": _f(r"Taxable interest", "2b"),
    "qualified_dividends": _f(r"Qualified dividends", "3a"),
    "ordinary_dividends": _f(r"Ordinary dividends", "3b"),
    # 4b/5b/6b share the label "Taxable amount": the printed line reference after it tells them apart
    "ira_taxable": _f(r"Taxable amount(?=[\s.]*4b\b)", "4b"),
    "pension_taxable": _f(r"Taxable amount(?=[\s.]*5b\b)", "5b"),
    "ss_taxable": _f(r"Taxable amount(?=[\s.]*6b\b)", "6b"),
    "capital_gain": _f(r"Capital gain or \(loss\)", "7"),
    "other_income": _f(r"Additional income from Schedule 1, line 10", "8"),
    "total_income": _f(r"This is your total income", "9", line_fallback=True),
    "agi": _f(r"This is your adjusted gross income", "11", line_fallback=True),
    "taxable_income": _f(r"If zero or less, enter -0-", "15", line_fallback=True),
    "tax": _f(r"(?:^|\s)Tax \(see instructions\)", "16"),
    "total_tax": _f(r"This is your total tax", "24", line_fallback=True),
    "withheld_total": _f(r"Add lines 25a through 25c", "25d"),
    # IRS rows read "25 Federal income tax withheld from: a Form(s) W-2 ..."; software often prints
    # "25a Form(s) W-2". Both are accepted ("a" standing alone, so line 1a never matches).
    "withheld_w2": _f(r"(?:^|\s)(?:25a|a)\s+Form\(s\) W-2", "25a"),
    "withheld_1099": _f(r"(?:^|\s)(?:25b|b)\s+Form\(s\) #?1099", "25b"),
    "withheld_other": _f(r"(?:^|\s)(?:25c|c)\s+Other forms", "25c"),
    "estimated_payments": _f(
        r"estimated tax payments and amount applied from (?:the )?\S+ return", "26"
    ),
    "total_payments": _f(r"These are your total payments", "33", line_fallback=True),
    "overpaid": _f(r"amount you overpaid", "34"),
    "refunded": _f(r"you want refunded to you", "35a"),
    "applied": _f(r"you want applied to your \S+ estimated tax", "36"),
    "owed": _f(r"This is the amount you owe", "37"),
    # Schedule D
    "d_short_term": _f(r"Net short-term capital gain or \(loss\)", "7", "d"),
    "d_long_term": _f(r"Net long-term capital gain or \(loss\)", "15", "d"),
    "d_combined": _f(r"Combine lines 7 and 15 and enter the result", "16", "d"),
    "d_gain_28": _f(r"28% Rate Gain Worksheet", "18", "d"),
    "d_gain_1250": _f(r"Unrecaptured Section 1250 Gain Worksheet", "19", "d"),
    # Schedule 1 / C
    "s1_business": _f(r"Business income or \(loss\)", "3", "1"),
    "c_net_profit": _f(r"Net profit or \(loss\)\. Subtract line 30 from line 29", "31", "c"),
    # Schedule 2
    "s2_se_tax": _f(r"Self-employment tax\. Attach Schedule SE", "4", "2"),
    "s2_add_medicare": _f(r"Additional Medicare Tax(?:[.,]?\s*(?:Attach )?Form \S+)?", "11", "2"),
    "s2_niit": _f(r"Net investment income tax(?:[.,]?\s*(?:Attach |from )?Form \S+)?", "12", "2"),
}

_WAGES_2023_PLUS: Final[dict[str, FieldSpec]] = {
    "wages": _f(r"Add lines 1a through 1h", "1z"),
    "wages_1a": _f(r"Total amount from Form\(s\) W-2, box 1", "1a"),
}

_WAGES_2022: Final[dict[str, FieldSpec]] = {
    "wages": _f(r"Wages, salaries, tips, etc\.?\s*Attach Form\(s\) W-2", "1"),
}

#: Per-year field tables (2022 numbers wages as line 1; 2023+ as 1a-1z; 2025 adds line 11a/11b and
#: moves the deductions to 12e/13a/13b, none of which change the labels used here).
F1040_FIELDS: Final[dict[int, dict[str, FieldSpec]]] = {
    2022: {**_COMMON, **_WAGES_2022},
    2023: {**_COMMON, **_WAGES_2023_PLUS},
    2024: {**_COMMON, **_WAGES_2023_PLUS},
    2025: {
        **_COMMON,
        **_WAGES_2023_PLUS,
        "agi": _f(r"This is your adjusted gross income", "11a", line_fallback=True),
    },
}
_LATEST_YEAR: Final[int] = max(F1040_FIELDS)


# --------------------------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------------------------

_TITLE_RE = re.compile(r"U\.S\. Individual Income Tax Return", re.IGNORECASE)
_FORM_RE = re.compile(r"\bForm\s+1040\b(?!\s*-)", re.IGNORECASE)
_FILING_STATUS_RE = re.compile(r"\bFiling Status\b", re.IGNORECASE)
_AGI_RE = re.compile(r"adjusted gross income", re.IGNORECASE)
_UNSUPPORTED_RE = re.compile(
    r"\bForm\s+1040\s*-\s*(?:SR|NR|X)\b|U\.S\. Tax Return for Seniors"
    r"|U\.S\. Nonresident Alien Income Tax Return|\bAmended U\.S\. Individual",
    re.IGNORECASE,
)


def _detect_score(text: str) -> float:
    if _UNSUPPORTED_RE.search(text):
        return 0.0
    has_form = _FORM_RE.search(text) is not None
    has_title = _TITLE_RE.search(text) is not None
    has_status = _FILING_STATUS_RE.search(text) is not None
    if has_form and has_title and has_status:
        return 0.95
    if has_form and has_title:
        return 0.6
    if has_form and has_status and _AGI_RE.search(text):
        return 0.55
    return 0.0


# --------------------------------------------------------------------------------------------
# Document preparation
# --------------------------------------------------------------------------------------------

_TRAILING_PERIOD_MONEY = re.compile(r"^(\(?-?\$?\d{1,3}(?:,\d{3})*|\(?-?\$?\d+)\.(\)?)$")
_YEAR_LIKE = re.compile(r"^(?:19|20)\d{2}$")
_BARE_FOUR = re.compile(r"^\d{4}$")
_FORM_WORDS = frozenset({"form", "forms", "form(s)", "form(s):", "form:", "schedule"})
#: A checkbox glyph (or the closing bracket of ``[ ]``) before a number: "Form(s): 1 ☐ 8814".
_CHECKBOX_WORDS = frozenset({"☐", "☒", "☑", "]", "[ ]", "( )"})
_SUBLINE_MARKER = re.compile(r"^[b-e]$")
_ALPHA_WORD = re.compile(r"^[A-Za-z]")
# The whole token after "SCHEDULE" (hyphen included), so "Schedule 1-A" is not read as Schedule 1.
_SCHEDULE_HEADING = re.compile(r"^\s*SCHEDULE\s+([\w-]+)", re.IGNORECASE)
_LINE_REF = re.compile(r"^\d{1,2}[a-z]?$")
#: Prefix marking a neutralised right-hand line reference (see `_prepare_page`).
REF_PREFIX = "ref:"
_ROW_GAP = 10.0
_GLYPH = 8.0


def _clean_token(prev: str | None, text: str) -> str:
    stripped = _TRAILING_PERIOD_MONEY.sub(r"\1\2", text)
    if _BARE_FOUR.match(stripped) and (
        _YEAR_LIKE.match(stripped)
        or (prev is not None and (prev.lower() in _FORM_WORDS or prev in _CHECKBOX_WORDS))
    ):
        return "#" + stripped  # a year or form number, never an amount
    return stripped


def _split_row(row: Row) -> list[list[Word]]:
    """Split a row at sub-line markers (``... b Taxable interest ...``) into its columns."""
    segments: list[list[Word]] = [[]]
    words = row.words
    for i, word in enumerate(words):
        is_marker = (
            i > 0
            and _SUBLINE_MARKER.match(word.text) is not None
            and i + 1 < len(words)
            and _ALPHA_WORD.match(words[i + 1].text) is not None
        )
        if is_marker:
            segments.append([])
        segments[-1].append(word)
    return [s for s in segments if s]


def _prepare_page(page: PageText) -> PageText:
    words_out: list[Word] = []
    lines: list[str] = []
    top = 0.0
    for row in cluster_rows(page.words):
        for segment in _split_row(row):
            cleaned: list[Word] = []
            prev: str | None = None
            lead = segment[0].text if _LINE_REF.match(segment[0].text) else None
            ref_seen = False
            for i, word in enumerate(segment):
                text = _clean_token(prev, word.text)
                if lead is not None and i > 0 and not ref_seen and word.text == lead:
                    # the right-hand repeat of the line number: never an amount, even if blank
                    text = REF_PREFIX + text
                    ref_seen = True
                cleaned.append(Word(text, word.x0, word.x1, top, top + _GLYPH))
                prev = word.text
            words_out.extend(cleaned)
            lines.append(" ".join(w.text for w in cleaned))
            top += _ROW_GAP
    text = "\n".join(lines)
    return PageText(
        page_number=page.page_number, text=text, words=tuple(words_out), char_count=len(text)
    )


def _prepare(doc: ExtractedDoc) -> ExtractedDoc:
    """Normalised copy of `doc` (see module docstring); coordinates are synthetic but x order and
    row membership are preserved."""
    return ExtractedDoc(
        file_sha256=doc.file_sha256,
        page_count=doc.page_count,
        pages=tuple(_prepare_page(p) for p in doc.pages),
        has_text_layer=doc.has_text_layer,
    )


@dataclass(frozen=True, slots=True)
class _Pages:
    main: tuple[int, ...]
    d: tuple[int, ...]
    s1: tuple[int, ...]
    s2: tuple[int, ...]
    c: tuple[int, ...]

    def for_scope(self, scope: Scope) -> tuple[int, ...]:
        return {"main": self.main, "d": self.d, "1": self.s1, "2": self.s2, "c": self.c}[scope]


def _classify_pages(doc: ExtractedDoc) -> _Pages:
    groups: dict[str, list[int]] = {"d": [], "1": [], "2": [], "c": []}
    main: list[int] = []
    other: list[int] = []
    for page in doc.pages:
        heading = "\n".join(page.text.splitlines()[:2])
        match = _SCHEDULE_HEADING.search(heading)
        if match is not None:
            key = match.group(1).lower()
            if key in groups:
                groups[key].append(page.page_number)
            else:
                other.append(page.page_number)
        elif re.match(r"^\s*Form\s+(?!1040\b)\d{3,5}\b", heading, re.IGNORECASE):
            other.append(page.page_number)  # an attached form (8960, 8959, ...): not ours
        else:
            main.append(page.page_number)
    return _Pages(
        main=tuple(main),
        d=tuple(groups["d"]),
        s1=tuple(groups["1"]),
        s2=tuple(groups["2"]),
        c=tuple(groups["c"]),
    )


# --------------------------------------------------------------------------------------------
# Value access
# --------------------------------------------------------------------------------------------


def _last_money_on_line_row(doc: ExtractedDoc, pages: Sequence[int], line: str) -> int | None:
    """Fallback: the amount printed after the right-hand line reference (``... 11 95,000``) on a
    row that starts with the printed line number. `None` when the reference or value is absent."""
    start = re.compile(rf"^{re.escape(line)}\s+\S")
    for page in doc.pages:
        if page.page_number not in pages:
            continue
        for row in cluster_rows(page.words):
            if start.match(row.text) is None:
                continue
            refs = [i for i, w in enumerate(row.words) if i > 0 and w.text == REF_PREFIX + line]
            if not refs:
                continue
            for word in row.words[refs[-1] + 1 :]:
                money = parse_money(word.text)
                if money is not None:
                    return money.magnitude_minor
    return None


def _inline_form_refs(row_words: Sequence[Word], start: int) -> bool:
    """True when words from `start` on contain a form reference (``Form(s):``, ``Form 8814``)."""
    return any(w.text.lower() in _FORM_WORDS for w in row_words[start:])


def _row_money(row: Row, hit: LabelHit, line: str) -> ParsedMoney | None:
    """The amount of a 1040 line on the row holding its label.

    Rows can carry form references between the label and the amount (``16 Tax (see
    instructions). Check if any from Form(s): 1 [ ] 8814 2 [ ] 4972 3 [ ] . . . 16 11,000.``), whose
    numbers must never be read as the amount. Rules, in order:

    1. the amount after the right-hand repeat of the line number (`REF_PREFIX`), blank if none
       follows it (an empty line prints only its number);
    2. on a row with an inline form reference, the last money token on the row;
    3. otherwise the first money token right of the label (box and line numbers skipped).
    """
    words = row.words
    after = [i for i, w in enumerate(words) if w.x0 >= hit.label_x1 - 0.5]
    refs = [i for i in after if words[i].text == REF_PREFIX + line]
    if refs:
        for word in words[refs[-1] + 1 :]:
            money = parse_money(word.text)
            if money is not None:
                return money
        return None
    candidates = same_row_candidates(row, hit, skip_line_numbers=True)
    if not candidates:
        return None
    if after and _inline_form_refs(words, after[0]):
        return candidates[-1]
    return candidates[0]


class _Values:
    """Reads fields of one year's table from a prepared document."""

    def __init__(self, doc: ExtractedDoc, pages: _Pages, table: dict[str, FieldSpec]) -> None:
        self._doc = doc
        self._pages = pages
        self._table = table

    def has_scope(self, scope: Scope) -> bool:
        return bool(self._pages.for_scope(scope))

    def signed(self, key: str) -> int | None:
        spec = self._table.get(key)
        if spec is None:
            return None
        pages = self._pages.for_scope(spec.scope)
        if not pages:
            return None
        found = row_for_label(self._doc, spec.label, pages=pages)
        if found is not None:
            money = _row_money(found[0], found[1], spec.line)
            if money is not None:
                return signed(money)
        if spec.line_fallback:
            return _last_money_on_line_row(self._doc, pages, spec.line)
        return None

    def amount(self, key: str) -> int | None:
        value = self.signed(key)
        return None if value is None else abs(value)

    def line(self, key: str) -> str:
        return self._table[key].line


# --------------------------------------------------------------------------------------------
# Filing status
# --------------------------------------------------------------------------------------------

_MARKED = "[xX✓✔]"
#: Any check box on a status row, marked or empty: ``[X]``, ``[ ]``, ``(X)``, ``( )``, a glyph.
_BOX = re.compile(r"\[\s*([xX✓✔]?)\s*\]|\(\s*([xX✓✔]?)\s*\)|([☐☒☑✓✔])")
#: A bare ``X`` standing before a label ("X Single"): a mark, never an empty box.
_BARE_X = re.compile(r"(?<![\w])[xX](?=\s+\S)")
_STATUS_LABELS: Final[tuple[tuple[str, str], ...]] = (
    ("single", r"Single\b"),
    ("mfj", r"Married filing jointly"),
    ("mfs", r"Married filing separately"),
    ("hoh", r"Head of household"),
    ("qss", r"Qualifying (?:surviving spouse|widow\(er\))"),
)
_STATUS_REGEXES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (key, re.compile(label, re.IGNORECASE)) for key, label in _STATUS_LABELS
)
_STATUS_ROW_START = re.compile(
    rf"^\s*(?:Filing Status\s+)?(?:{_BOX.pattern}|{_BARE_X.pattern}|"
    + "|".join(label for _, label in _STATUS_LABELS)
    + ")",
    re.IGNORECASE,
)


def _is_marked(match: re.Match[str]) -> bool:
    groups = match.groups()
    return bool(groups[0] or groups[1] or (groups[2] and groups[2] != "☐"))


def _row_statuses(line: str) -> list[str]:
    """Statuses marked on one text row; several statuses may share the row (IRS layout:
    ``Filing Status [ ] Single [X] Married filing jointly [ ] Married filing separately``).

    A mark belongs to the label that follows it when the row's first label has a box (or a bare
    ``X``) in front of it, else to the label that precedes it (``Single [X] Married ... [ ]``)."""
    if _STATUS_ROW_START.match(line) is None:
        return []
    spans: list[tuple[int, int, str]] = []
    for key, pattern in _STATUS_REGEXES:
        match = pattern.search(line)
        if match is not None:
            spans.append((match.start(), match.end(), key))
    spans.sort()
    if not spans:
        return []
    boxes = [(m.start(), m.end(), _is_marked(m)) for m in _BOX.finditer(line)]
    boxes += [(m.start(), m.end(), True) for m in _BARE_X.finditer(line)]
    boxes.sort()

    def box_before(start: int) -> tuple[int, int, bool] | None:
        for box in boxes:
            if box[1] <= start and line[box[1] : start].strip() == "":
                return box
        return None

    found: list[str] = []
    if box_before(spans[0][0]) is not None:  # the first label has a box in front: box-first row
        for start, _, key in spans:
            box = box_before(start)
            if box is not None and box[2]:
                found.append(key)
        return found
    for i, (_, end, key) in enumerate(spans):  # a box after each label
        limit = spans[i + 1][0] if i + 1 < len(spans) else len(line)
        if any(end <= b0 < limit and marked for b0, _, marked in boxes):
            found.append(key)
    return found


def _filing_status(doc: ExtractedDoc, pages: Sequence[int]) -> tuple[str | None, str | None]:
    """(status, problem) where problem is "none" or "multiple" when status is `None`."""
    found: list[str] = []
    for page in doc.pages:
        if page.page_number not in pages:
            continue
        for line in page.text.splitlines():
            for key in _row_statuses(line):
                if key not in found:
                    found.append(key)
    if len(found) == 1:
        return found[0], None
    return None, "multiple" if found else "none"


# --------------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------------


def _income(
    category: IncomeCategory,
    amount: int | None,
    box: str,
    label: str,
    *,
    is_memo: bool = False,
) -> ParsedIncomeItem | None:
    if amount is None or amount == 0:
        return None
    return ParsedIncomeItem(
        category=category,
        amount_minor=amount,
        source_form=_SOURCE_FORM,
        box=box,
        label=label,
        is_memo=is_memo,
    )


def _tax(
    category: TaxCategory,
    amount: int | None,
    box: str,
    label: str,
    *,
    keep_zero: bool = False,
    is_estimate: bool = False,
) -> ParsedTaxItem | None:
    if amount is None or (amount == 0 and not keep_zero):
        return None
    return ParsedTaxItem(
        category=category,
        amount_minor=amount,
        source_form=_SOURCE_FORM,
        box=box,
        label=label,
        is_estimate=is_estimate,
    )


_ONE_DOLLAR = 100


def _refund_received(v: _Values) -> tuple[int | None, str]:
    """(refund actually received, box label): line 35a when printed, else line 34 less line 36
    (the part applied to next year's estimated tax). `None` when neither line 34 nor 35a is read."""
    refunded = v.amount("refunded")
    if refunded is not None:
        return refunded, "line 35a"
    overpaid = v.amount("overpaid")
    if overpaid is None:
        return None, "line 34"
    return max(overpaid - (v.amount("applied") or 0), 0), "line 34"


class F1040Parser:
    """`TaxFormParser` for software-generated Form 1040 (tax years 2022-2025)."""

    id = "tax_1040"
    version = "1"
    form_types: tuple[FormType, ...] = ("1040",)

    def detect(self, doc: ExtractedDoc) -> float:
        """0.95 for "Form 1040" + "U.S. Individual Income Tax Return" + "Filing Status"; lower
        for partial matches; 0.0 for 1040-SR/NR/X and anything else. Never raises."""
        try:
            return _detect_score(doc.full_text)
        except (ValueError, TypeError):  # pragma: no cover - defensive, text is plain str
            return 0.0

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        """Parse a 1040 (plus attached Schedules D, 1, 2, C) into one `ParsedTaxForm`.

        Raises:
            UnsupportedLayoutError: 1040-SR / 1040-NR / 1040-X.
            ParserError: tax year undeterminable, or none of total income, AGI and total tax
                could be read. Messages are generic (no document text).
        """
        if _UNSUPPORTED_RE.search(doc.full_text):
            raise UnsupportedLayoutError(
                "This variant of Form 1040 (1040-SR, 1040-NR or an amended return) is not supported."
            )
        year = require_year(doc, tax_year)
        warnings: list[str] = []
        if year in F1040_FIELDS:
            table = F1040_FIELDS[year]
        else:
            table = F1040_FIELDS[_LATEST_YEAR]
            warnings.append(f"form layout for tax year {year} not verified")

        prepared = _prepare(doc)
        pages = _classify_pages(prepared)
        if not pages.main:
            raise ParserError("The Form 1040 pages could not be located in this file.")
        values = _Values(prepared, pages, table)

        total_income = values.amount("total_income")
        agi = values.amount("agi")
        total_tax = values.amount("total_tax")
        if total_income is None and agi is None and total_tax is None:
            raise ParserError("The key lines of this Form 1040 could not be read.")
        taxable_income = values.amount("taxable_income")
        income_tax = values.amount("tax")

        status, problem = _filing_status(prepared, pages.main)

        income_items = self._income_items(values, warnings, total_income)
        tax_items = self._tax_items(values, total_tax)

        est_items = self._estimate(
            values,
            year=year,
            status=status,
            problem=problem,
            taxable_income=taxable_income,
            income_tax=income_tax,
            warnings=warnings,
        )
        tax_items.extend(est_items)

        self._consistency(values, warnings)

        form = ParsedTaxForm(
            form_type="1040",
            tax_year=year,
            payer_name=None,
            payer_tin_last4=None,
            account_last4=None,
            is_corrected=False,
            filing_status=status,
            agi_minor=agi,
            taxable_income_minor=taxable_income,
            income=tuple(income_items),
            taxes=tuple(tax_items),
            warnings=tuple(check_warning(w) for w in dict.fromkeys(warnings)),
        )
        return build_document([form])

    # ----- income ---------------------------------------------------------------------------

    def _income_items(
        self, v: _Values, warnings: list[str], total_income: int | None
    ) -> list[ParsedIncomeItem]:
        items: list[ParsedIncomeItem | None] = []
        wages = v.amount("wages")
        wages_key = "wages"
        if wages is None:
            wages, wages_key = v.amount("wages_1a"), "wages_1a"
        if wages is not None:
            items.append(
                _income(
                    "salary_wages",
                    wages,
                    f"line {v.line(wages_key)}",
                    "Wages, salaries, tips",
                )
            )
        items.append(
            _income("interest", v.amount("taxable_interest"), "line 2b", "Taxable interest")
        )
        items.append(
            _income(
                "ordinary_dividends",
                v.amount("ordinary_dividends"),
                "line 3b",
                "Ordinary dividends",
            )
        )
        items.append(
            _income(
                "qualified_dividends",
                v.amount("qualified_dividends"),
                "line 3a",
                "Qualified dividends",
                is_memo=True,
            )
        )
        items.extend(self._capital_gain_items(v, warnings))

        business = v.signed("s1_business")
        if business is None:
            business = v.signed("c_net_profit")
        business_income = 0
        if business is not None:
            if business < 0:
                warnings.append("business loss reported; it is not included in income items")
            else:
                business_income = business
                items.append(
                    _income("self_employment", business, "Schedule 1 line 3", "Business income")
                )

        other = (
            (v.amount("ira_taxable") or 0)
            + (v.amount("pension_taxable") or 0)
            + (v.amount("ss_taxable") or 0)
        )
        line8 = v.signed("other_income")
        if line8 is not None:
            other += max(line8 - business_income, 0)
        items.append(_income("other", other, "lines 4b-6b, 8", "Retirement, benefits and other"))

        if total_income is not None:
            reported = sum(
                x
                for x in (
                    wages,
                    v.amount("taxable_interest"),
                    v.amount("ordinary_dividends"),
                    v.amount("ira_taxable"),
                    v.amount("pension_taxable"),
                    v.amount("ss_taxable"),
                    v.signed("capital_gain"),
                    line8,
                )
                if x is not None
            )
            if abs(reported - total_income) > _ONE_DOLLAR:
                warnings.append("sum of income lines does not match the printed total income")
        return [i for i in items if i is not None]

    def _capital_gain_items(self, v: _Values, warnings: list[str]) -> list[ParsedIncomeItem | None]:
        line7 = v.signed("capital_gain")
        short = v.signed("d_short_term")
        long_term = v.signed("d_long_term")
        if v.has_scope("d") and (short is not None or long_term is not None):
            return [
                _income(
                    "short_term_capital_gain",
                    short,
                    "Schedule D line 7",
                    "Net short-term capital gain or loss",
                ),
                _income(
                    "long_term_capital_gain",
                    long_term,
                    "Schedule D line 15",
                    "Net long-term capital gain or loss",
                ),
            ]
        if line7 is None or line7 == 0:
            return []
        if v.has_scope("d"):
            warnings.append(
                "Schedule D lines were not read; the capital gain is shown as long-term"
            )
        return [
            _income("long_term_capital_gain", line7, "line 7", "Capital gain or loss"),
        ]

    # ----- taxes ----------------------------------------------------------------------------

    def _tax_items(self, v: _Values, total_tax: int | None) -> list[ParsedTaxItem]:
        withheld = v.amount("withheld_total")
        if withheld is None:
            parts = [v.amount(k) for k in ("withheld_w2", "withheld_1099", "withheld_other")]
            if any(p is not None for p in parts):
                withheld = sum(p or 0 for p in parts)
        refund, refund_box = _refund_received(v)
        candidates = (
            _tax("total_federal_tax", total_tax, "line 24", "Total tax", keep_zero=True),
            _tax(
                "federal_income_tax_withheld",
                withheld,
                "line 25d",
                "Federal income tax withheld",
            ),
            _tax(
                "estimated_tax_payments",
                v.amount("estimated_payments"),
                "line 26",
                "Estimated tax payments",
            ),
            _tax(
                "total_payments",
                v.amount("total_payments"),
                "line 33",
                "Total payments",
                keep_zero=True,
            ),
            _tax("refund", refund, refund_box, "Refund received"),
            _tax("amount_owed", v.amount("owed"), "line 37", "Amount you owe"),
            _tax(
                "net_investment_income_tax",
                v.amount("s2_niit"),
                "Schedule 2 line 12",
                "Net investment income tax",
                keep_zero=True,
            ),
            _tax(
                "additional_medicare_tax",
                v.amount("s2_add_medicare"),
                "Schedule 2 line 11",
                "Additional Medicare Tax",
                keep_zero=True,
            ),
            _tax(
                "self_employment_tax",
                v.amount("s2_se_tax"),
                "Schedule 2 line 4",
                "Self-employment tax",
                keep_zero=True,
            ),
        )
        return [c for c in candidates if c is not None]

    # ----- estimate -------------------------------------------------------------------------

    def _estimate(
        self,
        v: _Values,
        *,
        year: int,
        status: str | None,
        problem: str | None,
        taxable_income: int | None,
        income_tax: int | None,
        warnings: list[str],
    ) -> list[ParsedTaxItem]:
        if status is None:
            what = (
                "several filing statuses are marked"
                if problem == "multiple"
                else "filing status not found"
            )
            warnings.append(f"{what}; federal tax split not estimated")
            return []
        if not estimate.has_table(year, status):
            warnings.append(
                f"no federal bracket table for tax year {year}; tax split not estimated"
            )
            return []
        if taxable_income is None or income_tax is None:
            warnings.append("taxable income or tax not read; federal tax split not estimated")
            return []

        qualified = v.amount("qualified_dividends") or 0
        line7 = v.signed("capital_gain")
        long_term = v.signed("d_long_term")
        combined = v.signed("d_combined")
        if v.has_scope("d") and long_term is not None:
            gain = long_term if combined is None else min(long_term, combined)
        else:
            gain = line7 or 0
        split = estimate.estimate_tax_split(
            tax_year=year,
            filing_status=status,
            taxable_income_minor=taxable_income,
            qualified_dividends_minor=qualified,
            net_ltcg_minor=max(gain, 0),
            income_tax_minor=income_tax,
        )
        if split is None:
            warnings.append("federal tax split could not be estimated")
            return []
        special = (v.amount("d_gain_28") or 0) + (v.amount("d_gain_1250") or 0)
        if split.approximate or special > 0:
            warnings.append("federal tax split is approximate")
        return [
            ParsedTaxItem(
                category="federal_tax_ordinary_est",
                amount_minor=split.ordinary_minor,
                source_form=_SOURCE_FORM,
                box="line 16",
                label="Tax on ordinary income (estimate)",
                is_estimate=True,
            ),
            ParsedTaxItem(
                category="federal_tax_preferential_est",
                amount_minor=split.preferential_minor,
                source_form=_SOURCE_FORM,
                box="line 16",
                label="Tax on qualified dividends and gains (estimate)",
                is_estimate=True,
            ),
        ]

    # ----- consistency ----------------------------------------------------------------------

    def _consistency(self, v: _Values, warnings: list[str]) -> None:
        overpaid = v.amount("overpaid")
        refunded = v.amount("refunded")
        applied = v.amount("applied")
        if overpaid is None and (refunded is not None or applied is not None):
            overpaid = (refunded or 0) + (applied or 0)  # line 34 unreadable: rebuild it
        owed = v.amount("owed")
        if (overpaid or 0) > 0 and (owed or 0) > 0:
            warnings.append("both a refund and an amount owed are printed")
        if (
            overpaid is not None
            and refunded is not None  # without 35a the refund is derived from 34 less 36
            and abs(overpaid - (refunded or 0) - (applied or 0)) > _ONE_DOLLAR
        ):
            warnings.append(
                "refund and amount applied to next year do not add up to the overpayment"
            )
        payments = v.amount("total_payments")
        total_tax = v.amount("total_tax")
        if payments is not None and total_tax is not None:
            diff = payments - total_tax
            if (
                abs((overpaid or 0) - max(diff, 0)) > _ONE_DOLLAR
                or abs((owed or 0) - max(-diff, 0)) > _ONE_DOLLAR
            ):
                warnings.append(
                    "refund or amount owed does not match total payments minus total tax"
                )


PARSER = F1040Parser()
