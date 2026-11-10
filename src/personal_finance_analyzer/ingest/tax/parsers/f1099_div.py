"""Form 1099-DIV (Dividends and Distributions) parser, id ``tax_1099_div``.

Reusable section parser (imported by the consolidated-1099 parser; signature is stable)::

    parse_div_section(block: ExtractedDoc, year: int) -> ParsedTaxForm

`block` holds the rows of ONE 1099-DIV (a whole single-form PDF or one section of a consolidated
statement); IRS box layout and same-row layout are both understood. `year` is the already
determined tax year. The result has ``form_type="1099-div"`` and ``source_form="1099-DIV"``:
box 1a `ordinary_dividends` (emitted even when 0), box 1b `qualified_dividends` (``is_memo=True``,
only when non-zero), box 2a `long_term_capital_gain` (capital-gain distributions are long term,
only when non-zero), box 12 exempt-interest dividends as `other` with ``is_memo=True`` (only when
non-zero), box 4 federal and the state box (``jurisdiction`` = state code) withholding when
non-zero. Warnings: qualified > ordinary, withholding without income, corrected mark. Payer name,
payer TIN last four and account last four are filled when readable (override with
`dataclasses.replace`).

Raises `ParserError` (generic message) when box 1a cannot be found. The year is NOT detected here.

`PARSER.parse` splits a file with several 1099-DIV forms and parses each with
`parse_div_section`.
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

_FORM: FormType = "1099-div"
_SOURCE = "1099-DIV"
_HEADING = re.compile(r"Form\s*1099-DIV\b", re.IGNORECASE)

_BOX_1A = compile_label(
    r"Total\s+ordinary\s+dividends|(?<![\d.,])1a\s*[-:.]?\s*Ordinary\s+dividends"
)
_BOX_1B = compile_label(r"Qualified\s+dividends")
_BOX_2A = compile_label(r"Total\s+capital\s+gain\s+distr")
_BOX_12 = compile_label(r"Exempt-?\s?interest\s+dividends")

_DISTINCTIVE = (_BOX_1A, _BOX_1B, _BOX_2A)
_CONSOLIDATED = re.compile(r"Consolidated\s+(?:Form\s+)?1099|1099\s+Composite", re.IGNORECASE)


def parse_div_section(block: ExtractedDoc, year: int) -> ParsedTaxForm:
    """Parse one 1099-DIV (see the module docstring for the contract).

    Raises:
        ParserError: box 1a (total ordinary dividends) has no value in `block`.
    """
    ordinary = box_amount(block, _BOX_1A)
    if ordinary is None:
        raise ParserError("A 1099-DIV form was found but box 1a (ordinary dividends) has no value.")
    income = [
        ParsedIncomeItem(
            category="ordinary_dividends",
            amount_minor=ordinary,
            source_form=_SOURCE,
            box="1a",
            label="Total ordinary dividends",
        )
    ]
    warnings: list[str] = []
    qualified = box_amount(block, _BOX_1B)
    if qualified:
        income.append(
            ParsedIncomeItem(
                category="qualified_dividends",
                amount_minor=qualified,
                source_form=_SOURCE,
                box="1b",
                label="Qualified dividends",
                is_memo=True,
            )
        )
        if qualified > ordinary:
            warnings.append(
                "Qualified dividends (box 1b) exceed total ordinary dividends (box 1a)."
            )
    distributions = box_amount(block, _BOX_2A)
    if distributions:
        income.append(
            ParsedIncomeItem(
                category="long_term_capital_gain",
                amount_minor=distributions,
                source_form=_SOURCE,
                box="2a",
                label="Total capital gain distributions",
            )
        )
    exempt = box_amount(block, _BOX_12)
    if exempt:
        income.append(
            ParsedIncomeItem(
                category="other",
                amount_minor=exempt,
                source_form=_SOURCE,
                box="12",
                label="Exempt-interest dividends",
                is_memo=True,
            )
        )
    taxes: list[ParsedTaxItem] = []
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
                box="16",
                label="State tax withheld",
                jurisdiction=code or UNKNOWN_STATE,
            )
        )
    if taxes and ordinary + (distributions or 0) == 0:
        warnings.append("Tax withheld is reported without any dividend income.")
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
    return halve_if_other_1099(score, doc, "DIV")


class Form1099DivParser:
    """Parser for Form 1099-DIV (id ``tax_1099_div``)."""

    id = "tax_1099_div"
    version = "1"
    form_types: tuple[FormType, ...] = (_FORM,)

    def detect(self, doc: ExtractedDoc) -> float:
        """0.9 for a "Form 1099-DIV" heading plus two distinctive box labels (0.6 with one, 0.3
        with none), halved when another 1099 section heading or a consolidated-statement marker
        is present. Never raises."""
        try:
            return _score(doc)
        except (ValueError, IndexError, KeyError):
            return 0.0

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        """Parse every 1099-DIV in `doc`.

        Raises:
            ParserError: tax year undeterminable, or a form without a box 1a value.
        """
        year = require_year(doc, tax_year)
        got = collect(
            split_forms(doc, _HEADING),
            lambda block: parse_div_section(block, year),
            kind="1099-DIV",
        )
        return build_document(got.forms, warnings=got.warnings, unparsed=got.unparsed)


PARSER = Form1099DivParser()
