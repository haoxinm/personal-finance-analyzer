"""Form 1099-INT (Interest Income) parser, id ``tax_1099_int``.

Reusable section parser (imported by the consolidated-1099 parser; signature is stable)::

    parse_int_section(block: ExtractedDoc, year: int) -> ParsedTaxForm

`block` holds the rows of ONE 1099-INT (a whole single-form PDF, or one section of a consolidated
statement) and works for both the IRS box layout (label above, value beneath) and the same-row
layout (``1 Interest income ....... 123.45``). `year` is the already-determined tax year. The
result has ``form_type="1099-int"``, ``source_form="1099-INT"`` items, payer name / TIN last four
and account last four when they can be read (callers may override them with
`dataclasses.replace`). Box 1 interest is emitted even when 0; box 3 (savings bonds / Treasury
obligations) as a second `interest` item and box 8 tax-exempt interest as `other` with
``is_memo=True`` only when non-zero; box 4 federal and box 17 state withholding (``jurisdiction`` =
state code) only when non-zero. A "CORRECTED" mark adds a warning and sets ``is_corrected``.

Raises `ParserError` (generic message, no document text) when box 1 cannot be found. The year is
NOT detected here; `PARSER.parse` does that (`common.require_year`).

`PARSER.parse` splits a file holding several 1099-INT forms (one per page, or at repeated
"Form 1099-INT" headings) and parses each with `parse_int_section`.
"""

from __future__ import annotations

import re

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import (
    FormType,
    ParsedIncomeItem,
    ParsedTaxDocument,
    ParsedTaxForm,
    ParsedTaxItem,
)
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.common import (
    build_document,
    check_warning,
    collect,
    halve_if_other_1099,
    require_year,
    sanitize_payer_name,
)
from personal_finance_analyzer.ingest.tax.fields import compile_label, corrected_marker, doc_has
from personal_finance_analyzer.ingest.tax.parsers._boxes import (
    CORRECTED_WARNING,
    FED_WITHHELD_LABEL,
    LABEL_CONTINUATION,
    PAYER_NAME_LABEL,
    PAYER_TIN_LABEL,
    RECIPIENT_TIN_LABEL,
    STATE_CODE_WARNING,
    STATE_TAX_WITHHELD_LABEL,
    UNKNOWN_STATE,
    account_last4,
    box_amount,
    split_forms,
    state_tax_lines,
    text_below,
    tin_last4,
)

_FORM: FormType = "1099-int"
_SOURCE = "1099-INT"
_HEADING = re.compile(r"Form\s*1099-INT\b", re.IGNORECASE)

_BOX1 = compile_label(r"(?:(?<![\d.,])1|\bBox\s*1)\s*[-:.]?\s*Interest\s+income")
_BOX3 = compile_label(
    r"Interest\s+on\s+U\.?\s?S\.?\s+Savings\s+Bonds(?:\s+and\s+Treas(?:ury|\.)?\s+obligations)?"
)
_BOX8 = compile_label(r"Tax-?\s?exempt\s+interest")
_EARLY_WITHDRAWAL = compile_label(r"Early\s+withdrawal\s+penalty")

#: Labels that identify the form (the payer/recipient TIN pair counts once).
_DISTINCTIVE = (_BOX1, _BOX3, _EARLY_WITHDRAWAL)
_CONSOLIDATED = re.compile(r"Consolidated\s+(?:Form\s+)?1099|1099\s+Composite", re.IGNORECASE)


def parse_int_section(block: ExtractedDoc, year: int) -> ParsedTaxForm:
    """Parse one 1099-INT (see the module docstring for the contract).

    Raises:
        ParserError: box 1 (interest income) has no value in `block`.
    """
    interest = box_amount(block, _BOX1)
    if interest is None:
        raise ParserError("A 1099-INT form was found but box 1 (interest income) has no value.")
    income = [
        ParsedIncomeItem(
            category="interest",
            amount_minor=interest,
            source_form=_SOURCE,
            box="1",
            label="Interest income",
        )
    ]
    bonds = box_amount(block, _BOX3)
    if bonds:
        income.append(
            ParsedIncomeItem(
                category="interest",
                amount_minor=bonds,
                source_form=_SOURCE,
                box="3",
                label="Interest on U.S. Savings Bonds and Treasury obligations",
            )
        )
    exempt = box_amount(block, _BOX8)
    if exempt:
        income.append(
            ParsedIncomeItem(
                category="other",
                amount_minor=exempt,
                source_form=_SOURCE,
                box="8",
                label="Tax-exempt interest",
                is_memo=True,
            )
        )
    taxes: list[ParsedTaxItem] = []
    warnings: list[str] = []
    withheld = box_amount(block, FED_WITHHELD_LABEL)
    if withheld:
        taxes.append(
            ParsedTaxItem(
                category="federal_income_tax_withheld",
                amount_minor=withheld,
                source_form=_SOURCE,
                box="4",
                label="Federal income tax withheld",
            )
        )
    for code, money in state_tax_lines(block, STATE_TAX_WITHHELD_LABEL):
        if money.magnitude_minor == 0:
            continue
        if code is None:
            warnings.append(STATE_CODE_WARNING)
        taxes.append(
            ParsedTaxItem(
                category="state_income_tax_withheld",
                amount_minor=money.magnitude_minor,
                source_form=_SOURCE,
                box="17",
                label="State tax withheld",
                jurisdiction=code or UNKNOWN_STATE,
            )
        )
    if taxes and interest + (bonds or 0) == 0:
        warnings.append("Tax withheld is reported without any interest income.")
    corrected = corrected_marker(block)
    if corrected:
        warnings.append(CORRECTED_WARNING)
    return ParsedTaxForm(
        form_type=_FORM,
        tax_year=year,
        payer_name=sanitize_payer_name(
            text_below(block, PAYER_NAME_LABEL, skip_re=LABEL_CONTINUATION)
        ),
        payer_tin_last4=tin_last4(block, PAYER_TIN_LABEL),
        account_last4=account_last4(block),
        is_corrected=corrected,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=tuple(income),
        taxes=tuple(taxes),
        warnings=tuple(check_warning(w) for w in dict.fromkeys(warnings)),
    )


def _score(doc: ExtractedDoc) -> float:
    if not doc_has(doc, _HEADING):
        return 0.0
    hits = sum(1 for label in _DISTINCTIVE if doc_has(doc, label))
    if doc_has(doc, PAYER_TIN_LABEL) and doc_has(doc, RECIPIENT_TIN_LABEL):
        hits += 1
    score = 0.9 if hits >= 2 else 0.6 if hits == 1 else 0.3
    if doc_has(doc, _CONSOLIDATED):
        score *= 0.5
    return halve_if_other_1099(score, doc, "INT")


class Form1099IntParser:
    """Parser for Form 1099-INT (id ``tax_1099_int``)."""

    id = "tax_1099_int"
    version = "1"
    form_types: tuple[FormType, ...] = (_FORM,)

    def detect(self, doc: ExtractedDoc) -> float:
        """0.9 for a "Form 1099-INT" heading plus two distinctive box labels (0.6 with one, 0.3
        with none), halved when another 1099 section heading or a consolidated-statement marker
        is present. Never raises."""
        try:
            return _score(doc)
        except (ValueError, IndexError, KeyError):
            return 0.0

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        """Parse every 1099-INT in `doc`.

        Raises:
            ParserError: tax year undeterminable, or a form without a box 1 value.
        """
        year = require_year(doc, tax_year)
        got = collect(
            split_forms(doc, _HEADING),
            lambda block: parse_int_section(block, year),
            kind="1099-INT",
        )
        return build_document(got.forms, warnings=got.warnings, unparsed=got.unparsed)


PARSER = Form1099IntParser()
