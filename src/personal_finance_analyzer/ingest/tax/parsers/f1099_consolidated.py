"""Consolidated 1099 (broker "Tax Information" document) parser, id ``tax_1099_consolidated``.

One document carries several sections (``1099-INT``, ``1099-DIV``, ``1099-B``, ``1099-MISC``,
``1099-OID``) behind a ``Form 1099 Summary``; software layout, label and value on one row (dot
leaders, ``Box n`` / ``1a Total`` prefixes and a state code on its own row are understood). A
section that repeats (a running ``Form 1099-B`` heading on each page of a transaction table) is
merged before it is parsed, so its subtotals are counted once. Each
account (a row starting ``Account number``) yields ONE `ParsedTaxForm` with
``form_type="1099-consolidated"``, ``account_last4`` set, and the union of its sections' items,
each keeping its own ``source_form``. Payer name and TIN (last four) are read per account: the
payer block printed since the previous account row (else the first payer row inside the account's own block, else the payer carried over), so a
file holding several payers' statements keeps each identity. An account none of whose sections
yielded an amount is not emitted (an empty form has no identity and no value). Section headings are matched without the word "Form" (``1099-DIV  Dividends ...``) so
the single-form parsers, which key on ``Form 1099-XXX``, stay quiet. Unsupported sections
(for example ``1099-R``) are skipped with a warning and reported as an `UnparsedRegion`; so is a
section from which no amount could be read (a section whose amounts are all zero IS recognised).

Detection (plan 2.4): at least two distinct 1099 section headings plus a consolidated/summary
marker scores 0.95; a single section behind that marker also 0.95; two sections without a marker
0.6; otherwise 0.0.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.pii import compile_redactor
from personal_finance_analyzer.core.tax_types import (
    FormType,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
    UnparsedRegion,
)
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.layout import Row, cluster_rows
from personal_finance_analyzer.ingest.tax.common import (
    build_document,
    current_pii_terms,
    distinct_1099_sections,
    require_year,
    sanitize_payer_name,
)
from personal_finance_analyzer.ingest.tax.fields import (
    compile_label,
    corrected_marker,
    merge_docs,
    split_blocks,
    text_for_label,
)
from personal_finance_analyzer.ingest.tax.parsers._boxes import (
    PAYER_NAME_LABELS,
    PAYER_TIN_LABELS,
    payer_from_row,
)
from personal_finance_analyzer.ingest.tax.parsers._consolidated_sections import (
    SectionResult,
    parse_consolidated_div,
    parse_consolidated_int,
    parse_consolidated_misc,
    parse_consolidated_oid,
)
from personal_finance_analyzer.ingest.tax.parsers.f1099_b import parse_b_section
from personal_finance_analyzer.ingest.tax.tin import mask_tin, scrub

_MARKER_RE = re.compile(
    r"Consolidated\s+Form\s+1099|Form\s+1099\s+Summary|1099\s+Composite|Tax\s+Information",
    re.IGNORECASE,
)
_SECTION_START = re.compile(
    r"^\s*(?:(?i:Form\s+)?1099\s*-\s*([A-Z]{1,4})\b"
    r"|(?i:Summary\s+of\s+(?:\d{4}\s+)?(Original\s+Issue\s+Discount)\b))"
)
_ACCOUNT_START = compile_label(r"^Account\s+(?:number\b|no\b\.?|#)")
_ACCOUNT_RE = compile_label(r"^Account\s+(?:number\b|no\b\.?|#)\s*:?")
_NOT_A_NAME = re.compile(
    r"\d|\bForm\b|\b1099\b|\bTax(?:payer)?\b|\bSummary\b|\bInformation\b|\bStatement\b"
    r"|\bYear\b|\bAccount\b|\bPage\b|\bNo\b|\bNumber\b|\bDate\b|:",
    re.IGNORECASE,
)
#: Evidence that a header line names a company (a recipient's own name has none of these).
_COMPANY_WORD = re.compile(
    r"\b(?:inc|incorporated|llc|l\.l\.c|lp|l\.p|llp|ltd|limited|corp|corporation|co|company|"
    r"bank|n\.?a|securities|brokers?|brokerage|investments?|investor|financial|trust|fund|funds|capital|"
    r"group|services|markets|advisors?|wealth|partners|holdings|clearing|assets?|management|"
    r"insurance|credit\s+union|savings)\b\.?",
    re.IGNORECASE,
)
#: A bare ``Federal Identification Number`` label counts as the payer's only inside a payer block.
_FED_ID_LABEL = compile_label(r"^Federal\s+(?:Identification|ID)\s+(?:Number|No\.?)\s*:?")
_PAYER_BLOCK_ROWS = 10
_RUN_GAP = 14.0
_HEADER_INFERRED_WARNING = "Payer name was inferred from the page header."
_NO_AMOUNTS_MESSAGE = "No amounts could be read from the 1099 sections in this file."
_RETURN_RE = re.compile(r"U\.S\.\s+Individual\s+Income\s+Tax\s+Return", re.IGNORECASE)


def _section_kind(block: ExtractedDoc) -> str | None:
    for page in block.pages:
        for row in cluster_rows(page.words):
            match = _SECTION_START.match(row.text)
            if match is None:
                return None
            return "OID" if match.group(2) else match.group(1).upper()
    return None


def _row_runs(row: Row) -> list[list[int]]:
    """Indices of the words of each text run (cell) of `row`."""
    runs: list[list[int]] = []
    for index, word in enumerate(row.words):
        if runs and word.x0 - row.words[runs[-1][-1]].x1 <= _RUN_GAP:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def _payer_in_runs(
    rows: tuple[Row, ...], row_index: int, *, in_payer_block: bool
) -> tuple[str | None, str | None]:
    """Payer name / TIN last four from a label that starts a text run in the middle of a row
    (several blocks share one visual row on a statement's first page): ``Payer:`` with the name
    beneath it, or on the same run; ``Federal Identification Number`` inside the payer block."""
    row = rows[row_index]
    name: str | None = None
    tin: str | None = None
    for run in _row_runs(row):
        words = [row.words[i] for i in run]
        text = " ".join(w.text for w in words)
        match = PAYER_NAME_LABELS.search(text)
        if match is not None and name is None:
            rest = scrub(text[match.end() :]).strip(" :\t")
            if not rest:
                for below in rows[row_index + 1 : row_index + 3]:
                    cell = [w for w in below.words if abs(w.x0 - words[0].x0) <= 6]
                    if cell:
                        start = below.words.index(cell[0])
                        parts = [below.words[start].text]
                        for prev, cur in zip(
                            below.words[start:], below.words[start + 1 :], strict=False
                        ):
                            if cur.x0 - prev.x1 > 8:
                                break
                            parts.append(cur.text)
                        rest = scrub(" ".join(parts)).strip(" :\t")
                        break
            name = rest or None
            continue
        tin_match = PAYER_TIN_LABELS.search(text)
        if tin_match is None and in_payer_block:
            tin_match = _FED_ID_LABEL.search(text)
        if tin_match is not None and tin is None:
            tin = mask_tin(text[tin_match.end() :])
    return name, tin


def _payers_per_account(doc: ExtractedDoc, n_accounts: int) -> list[tuple[str | None, str | None]]:
    """``(payer name, payer TIN last4)`` for each account block, in order (one entry when the
    document has no account rows). An account takes the payer rows printed since the previous
    account row (a payer block above its own account); a payer that was already used by an earlier
    account is replaced by the first payer rows inside this account's own block (after its
    account row), and kept when the block has none (one payer heading several accounts)."""
    # per account row: the payer carried into it, and whether it is new since the previous
    # account row (a payer printed above its own account) rather than the one that account
    # inherits from an earlier block.
    snapshots: list[tuple[str | None, str | None, bool, bool]] = []
    inside: list[list[str | None]] = []  # first name / tin seen after each account row
    name: str | None = None
    tin: str | None = None
    fresh_name = fresh_tin = False
    first: list[str | None] = [None, None]  # first name / tin in the whole document
    for page in doc.pages:
        rows = cluster_rows(page.words)
        last_payer_row = -_PAYER_BLOCK_ROWS - 1
        for index, row in enumerate(rows):
            if n_accounts and _ACCOUNT_START.search(row.text):
                snapshots.append((name, tin, fresh_name, fresh_tin))
                fresh_name = fresh_tin = False
                inside.append([None, None])
                continue
            row_name, row_tin = payer_from_row(row.text)
            if row_name is None and row_tin is None:
                row_name, row_tin = _payer_in_runs(
                    rows, index, in_payer_block=index - last_payer_row <= _PAYER_BLOCK_ROWS
                )
            if row_name is not None:
                last_payer_row = index
                name = sanitize_payer_name(row_name)
                fresh_name = True
                if first[0] is None:
                    first[0] = name
                if inside and inside[-1][0] is None:
                    inside[-1][0] = name
            if row_tin is not None:
                tin = row_tin
                fresh_tin = True
                if first[1] is None:
                    first[1] = tin
                if inside and inside[-1][1] is None:
                    inside[-1][1] = tin
    if not n_accounts:
        return [(first[0], first[1])]
    return [
        (
            snap_name if fresh_n else (after[0] or snap_name),
            snap_tin if fresh_t else (after[1] or snap_tin),
        )
        for (snap_name, snap_tin, fresh_n, fresh_t), after in zip(snapshots, inside, strict=True)
    ]


def _header_payer_name(doc: ExtractedDoc, pii_terms: Sequence[str] = ()) -> str | None:
    """The first non-empty line of page 1 when it looks like a company name: letters, no digits,
    not a form title or a label, carrying a company word (Inc, LLC, Bank, Securities ...) and not
    matching any of the user's personal-info terms (the recipient's own name often heads page 1).
    `None` otherwise."""
    if not doc.pages:
        return None
    for row in cluster_rows(doc.pages[0].words):
        text = row.text.strip()
        if not text:
            continue
        if _NOT_A_NAME.search(text) or _MARKER_RE.search(text) or not re.search(r"[A-Za-z]", text):
            return None
        if _COMPANY_WORD.search(text) is None:
            return None
        if pii_terms and compile_redactor(pii_terms).redact(text)[1]:
            return None
        return sanitize_payer_name(text)
    return None


def _account_last4(account: ExtractedDoc) -> str | None:
    """Last four digits of the account number: after the label on its row, else the cell beneath
    it (statements print ``Account No.`` as a column heading with the value on the next row)."""
    same = mask_tin(text_for_label(account, _ACCOUNT_RE, mode="same_row"))
    if same is not None:
        return same
    return mask_tin(text_for_label(account, _ACCOUNT_RE, mode="below"))


def _merge_same_kind(sections: tuple[ExtractedDoc, ...]) -> list[tuple[str | None, ExtractedDoc]]:
    """One block per section kind, in order of first appearance. A section split by a repeated
    running heading (every page of a transaction table) is put back together."""
    order: list[str | None] = []
    groups: dict[str | None, list[ExtractedDoc]] = {}
    for block in sections:
        kind = _section_kind(block)
        if kind not in groups:
            order.append(kind)
            groups[kind] = []
        groups[kind].append(block)
    return [
        (kind, groups[kind][0] if len(groups[kind]) == 1 else merge_docs(groups[kind]))
        for kind in order
    ]


class Form1099ConsolidatedParser:
    """Consolidated broker 1099 (INT / DIV / B / MISC / OID sections)."""

    id = "tax_1099_consolidated"
    version = "1"
    form_types: tuple[FormType, ...] = ("1099-consolidated",)

    def detect(self, doc: ExtractedDoc) -> float:
        text = doc.full_text
        if _RETURN_RE.search(text):
            return 0.0
        sections = distinct_1099_sections(doc)
        marker = _MARKER_RE.search(text) is not None
        if sections and marker:
            return 0.95
        if len(sections) >= 2:
            return 0.6
        return 0.0

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        year = require_year(doc, tax_year)
        corrected = corrected_marker(doc)
        accounts = split_blocks(doc, _ACCOUNT_START)
        payers = _payers_per_account(doc, len(accounts))
        header_name = _header_payer_name(doc, current_pii_terms())
        forms: list[ParsedTaxForm] = []
        unparsed: list[UnparsedRegion] = []
        found_sections = False
        for account, (name, tin) in zip(accounts or (doc,), payers, strict=True):
            form, had_sections, regions = self._parse_account(
                account, year, name, tin, corrected, header_name
            )
            found_sections = found_sections or had_sections
            unparsed.extend(regions)
            if form is not None:
                forms.append(form)
        if not forms:
            if found_sections:
                raise ParserError(_NO_AMOUNTS_MESSAGE)
            raise ParserError("No 1099 sections were found in this file.")
        return build_document(forms, unparsed=unparsed)

    def _parse_account(
        self,
        account: ExtractedDoc,
        year: int,
        payer: str | None,
        payer_tin: str | None,
        corrected: bool,
        header_name: str | None = None,
    ) -> tuple[ParsedTaxForm | None, bool, list[UnparsedRegion]]:
        """The account's form (None when no section yielded an amount), whether the account had
        any section at all, and the sections that yielded nothing."""
        income: list[ParsedIncomeItem] = []
        taxes: list[ParsedTaxItem] = []
        warnings: list[str] = []
        unparsed: list[UnparsedRegion] = []
        sections = split_blocks(account, _SECTION_START)
        if not sections:
            return None, False, unparsed
        for kind, block in _merge_same_kind(sections):
            pages = tuple(p.page_number for p in block.pages)
            result: SectionResult
            if kind == "INT":
                result = parse_consolidated_int(block)
            elif kind == "DIV":
                result = parse_consolidated_div(block)
            elif kind == "MISC":
                result = parse_consolidated_misc(block)
            elif kind == "OID":
                result = parse_consolidated_oid(block)
            elif kind == "B":
                try:
                    form = parse_b_section(block, year)
                except ParserError:
                    warnings.append("The 1099-B section contained no readable totals.")
                    unparsed.append(UnparsedRegion("1099-B", pages, "no amounts"))
                    continue
                result = SectionResult(form.income, form.taxes, form.warnings)
            else:
                warnings.append(f"A 1099-{kind} section is not supported and was skipped.")
                unparsed.append(UnparsedRegion(f"1099-{kind}", pages, "unsupported"))
                continue
            if not result.found_any:
                warnings.append(f"The 1099-{kind} section contained no recognizable amounts.")
                unparsed.append(UnparsedRegion(f"1099-{kind}", pages, "no amounts"))
            income.extend(result.income)
            taxes.extend(result.taxes)
            warnings.extend(result.warnings)
        if not income and not taxes:
            # no amounts anywhere: an empty form has no identity or value
            return None, True, unparsed
        if payer is None and header_name is not None:
            payer = header_name
            warnings.append(_HEADER_INFERRED_WARNING)
        form_type: FormType = "1099-consolidated"
        form = ParsedTaxForm(
            form_type=form_type,
            tax_year=year,
            payer_name=payer,
            payer_tin_last4=payer_tin,
            account_last4=_account_last4(account),
            is_corrected=corrected,
            filing_status=None,
            agi_minor=None,
            taxable_income_minor=None,
            income=tuple(income),
            taxes=tuple(taxes),
            warnings=tuple(dict.fromkeys(warnings)),
        )
        return form, True, unparsed


PARSER = Form1099ConsolidatedParser()

__all__ = ["PARSER", "Form1099ConsolidatedParser"]
