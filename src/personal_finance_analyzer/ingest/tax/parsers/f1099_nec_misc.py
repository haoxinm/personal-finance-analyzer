"""Form 1099-NEC and 1099-MISC parser, id ``tax_1099_nec_misc`` (one parser, two form types).

The form type of each form follows its heading ("Form 1099-NEC" / "Form 1099-MISC").

* 1099-NEC: box 1 nonemployee compensation -> `self_employment` (emitted even when 0); box 4
  federal and box 5 state tax withheld (``jurisdiction`` = state code), non-zero only.
* 1099-MISC: box 1 rents, box 2 royalties, box 3 other income -> three `other` items (non-zero
  only); a pre-2020 layout's box 7 nonemployee compensation -> `self_employment`; box 4 federal and
  box 16 state tax withheld. At least one of boxes 1, 2, 3 or 7 must be present.

A file with several forms (several headings) yields one `ParsedTaxForm` each.

Raises `ParserError` (generic message) when the tax year cannot be determined (and none was
given), when a form has no readable primary box, or when no heading is found.
"""

from __future__ import annotations

import re

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import (
    FormType,
    IncomeCategory,
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

_HEADING = re.compile(r"Form\s*1099-(NEC|MISC)\b", re.IGNORECASE)
_CONSOLIDATED = re.compile(r"Consolidated\s+(?:Form\s+)?1099|1099\s+Composite", re.IGNORECASE)

_NEC_BOX1 = compile_label(r"(?<![\d.,])1\s*[-:.]?\s*Nonemployee\s+compensation")
_MISC_BOX7 = compile_label(r"(?<![\d.,])7\s*[-:.]?\s*Nonemployee\s+compensation")
_RENTS = compile_label(r"(?<![\d.,])1\s*[-:.]?\s*Rents\b")
_ROYALTIES = compile_label(r"(?<![\d.,])2\s*[-:.]?\s*Royalties\b")
_OTHER_INCOME = compile_label(r"(?<![\d.,])3\s*[-:.]?\s*Other\s+income\b")

_DISTINCTIVE = (_NEC_BOX1, _RENTS, _ROYALTIES, _OTHER_INCOME)


def _item(
    category: IncomeCategory, amount: int, form: str, box: str, label: str
) -> ParsedIncomeItem:
    return ParsedIncomeItem(
        category=category, amount_minor=amount, source_form=form, box=box, label=label
    )


def _parse_form(block: ExtractedDoc, year: int) -> ParsedTaxForm:
    heading = _HEADING.search(block.full_text)
    if heading is None:
        raise ParserError("A 1099-NEC or 1099-MISC form could not be identified.")
    is_nec = heading.group(1).upper() == "NEC"
    form_type: FormType = "1099-nec" if is_nec else "1099-misc"
    source = "1099-NEC" if is_nec else "1099-MISC"

    income: list[ParsedIncomeItem] = []
    if is_nec:
        compensation = box_amount(block, _NEC_BOX1)
        if compensation is None:
            raise ParserError("A 1099-NEC form was found but box 1 has no value.")
        income.append(
            _item("self_employment", compensation, source, "1", "Nonemployee compensation")
        )
        state_label, state_box = STATE_TAX_WITHHELD_LABEL, "5"
    else:
        found = False
        for label_re, box, label in (
            (_RENTS, "1", "Rents"),
            (_ROYALTIES, "2", "Royalties"),
            (_OTHER_INCOME, "3", "Other income"),
        ):
            amount = box_amount(block, label_re)
            if amount is not None:
                found = True
                if amount:
                    income.append(_item("other", amount, source, box, label))
        legacy = box_amount(block, _MISC_BOX7)
        if legacy is not None:
            found = True
            if legacy:
                income.append(
                    _item("self_employment", legacy, source, "7", "Nonemployee compensation")
                )
        if not found:
            raise ParserError(
                "A 1099-MISC form was found but none of boxes 1, 2, 3 or 7 has a value."
            )
        state_label, state_box = STATE_TAX_WITHHELD_LABEL, "16"

    taxes: list[ParsedTaxItem] = []
    warnings: list[str] = []
    withheld = box_amount(block, FED_WITHHELD_LABEL)
    if withheld:
        taxes.append(
            ParsedTaxItem(
                category="federal_income_tax_withheld",
                amount_minor=withheld,
                source_form=source,
                box="4",
                label="Federal income tax withheld",
            )
        )
    for code, money in state_tax_lines(block, state_label):
        if money.magnitude_minor == 0:
            continue
        if code is None:
            warnings.append(STATE_CODE_WARNING)
        taxes.append(
            ParsedTaxItem(
                category="state_income_tax_withheld",
                amount_minor=money.magnitude_minor,
                source_form=source,
                box=state_box,
                label="State tax withheld",
                jurisdiction=code or UNKNOWN_STATE,
            )
        )
    corrected = corrected_marker(block)
    if corrected:
        warnings.append(CORRECTED_WARNING)
    return ParsedTaxForm(
        form_type=form_type,
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
    return halve_if_other_1099(score, doc, ("NEC", "MISC"))


class Form1099NecMiscParser:
    """Parser for Form 1099-NEC and 1099-MISC (id ``tax_1099_nec_misc``)."""

    id = "tax_1099_nec_misc"
    version = "1"
    form_types: tuple[FormType, ...] = ("1099-nec", "1099-misc")

    def detect(self, doc: ExtractedDoc) -> float:
        """0.9 for a "Form 1099-NEC/MISC" heading plus two distinctive box labels (0.6 with one,
        0.3 with none), halved when a 1099 section other than NEC/MISC is present or the file is
        a consolidated statement. Never raises."""
        try:
            return _score(doc)
        except (ValueError, IndexError, KeyError):
            return 0.0

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        """Parse every 1099-NEC / 1099-MISC in `doc`.

        Raises:
            ParserError: tax year undeterminable, no heading, or a form without a primary box.
        """
        year = require_year(doc, tax_year)
        got = collect(
            split_forms(doc, _HEADING), lambda block: _parse_form(block, year), kind="1099-NEC/MISC"
        )
        return build_document(got.forms, warnings=got.warnings, unparsed=got.unparsed)


PARSER = Form1099NecMiscParser()
