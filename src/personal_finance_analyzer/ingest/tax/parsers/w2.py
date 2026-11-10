"""Form W-2 (Wage and Tax Statement) parser, id ``tax_w2``.

Emits per employer form: box 1 as `salary_wages`; boxes 2, 4, 6 as federal income tax, Social
Security tax and Medicare tax withheld; box 17 (one item per state line, ``jurisdiction`` = the
state code printed in box 15) and box 19 (``jurisdiction`` = the box 20 locality name). Box 12
lines (12a-12d) with code D, E, F, G or S become memo items `retirement_deferral` (box ``12-D``...)
and code W a memo item `hsa_employer` (``12-W``): both are already excluded from box 1 (never
subtract them again) and are shown only for reference and the deferral-limit warning; other codes
are ignored. Boxes 3, 5 and 14 are not stored. Zero withholding amounts are not emitted; box 1 is emitted even when 0.

One file may hold several forms (two employers) or the same form repeated (Copy B / Copy C /
Copy 2 on one page). A form starts at each "Employee's social security number" label; identical
parsed forms are collapsed into one, but only when the employer EIN was read or a "Copy B/C/2"
marker is present in the file; otherwise identical forms are kept (with a warning) because nothing
shows they are copies of one form. The recipient's name, address and SSN are never read.

Raises `ParserError` (generic message) when no tax year can be determined (and none was given) or
when a form has the box 1 label but no value (or no form with box 1 exists at all).
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
    require_year,
    sanitize_payer_name,
)
from personal_finance_analyzer.ingest.tax.fields import (
    compile_label,
    corrected_marker,
    doc_has,
    split_blocks,
    text_for_label,
)
from personal_finance_analyzer.ingest.tax.parsers._boxes import (
    CORRECTED_WARNING,
    LABEL_CONTINUATION,
    MAX_JURISDICTION_LEN,
    STATE_CODE_WARNING,
    UNKNOWN_STATE,
    box_amount,
    clean_jurisdiction,
    code_amount_in_cell,
    label_present,
    money_rows_below,
    state_code_in,
    text_below,
    tin_last4,
)

_FORM: FormType = "w2"
_SOURCE = "W-2"

_TITLE = re.compile(r"Wage\s+and\s+Tax\s+Statement", re.IGNORECASE)
_FORM_W2 = re.compile(r"(?:\bForm\s+W-?2\b|\bW-2\s+Wage\b)", re.IGNORECASE)
_COPY_MARKER = re.compile(r"\bCopy\s+(?:A|B|C|D|1|2)\b", re.IGNORECASE)
_IDENTICAL_WARNING = "Two identical W-2 forms were found; if one is a duplicate copy, delete it."
_START = compile_label(r"Employee'?s\s+social\s+security\s+number")

_EIN_LABEL = compile_label(r"Employer\s+identification\s+number")
_EMPLOYER_NAME = compile_label(r"Employer'?s\s+name")
_WAGES = compile_label(r"Wages,?\s+tips,?\s+other\s+comp")
_FED = compile_label(r"Federal\s+income\s+tax\s+withheld")
_SS_TAX = compile_label(r"Social\s+security\s+tax\s+withheld")
_MEDICARE_TAX = compile_label(r"Medicare\s+tax\s+withheld")
_STATE_TAX = compile_label(r"State\s+income\s+tax")
_LOCAL_TAX = compile_label(r"Local\s+income\s+tax")
_LOCALITY = compile_label(r"Locality\s+name")


#: Box 12 codes that are memo items: elective deferrals (D 401(k), E 403(b), F 408(k)(6) SEP,
#: G 457(b), S SIMPLE 401(k)) and W (employer HSA contributions, incl. cafeteria plan).
_DEFERRAL_CODES = frozenset({"D", "E", "F", "G", "S"})
_BOX12_CODES = _DEFERRAL_CODES | {"W"}
_BOX12_SLOTS = tuple(compile_label(rf"(?<![\d.])12{slot}\b(?!\d)") for slot in ("a", "b", "c", "d"))


def _score(doc: ExtractedDoc) -> float:
    strong = doc_has(doc, _TITLE)
    weak = doc_has(doc, _FORM_W2)
    ein = doc_has(doc, _EIN_LABEL)
    wages = doc_has(doc, _WAGES)
    if (strong or weak) and ein and wages:
        return 0.95
    if strong and (ein or wages):
        return 0.6
    return 0.0


def _state_items(block: ExtractedDoc) -> tuple[list[ParsedTaxItem], list[str]]:
    items: list[ParsedTaxItem] = []
    warnings: list[str] = []
    for row, money in money_rows_below(block, _STATE_TAX):
        if money.magnitude_minor == 0:
            continue
        code = state_code_in(row)
        if code is None:
            code = UNKNOWN_STATE
            warnings.append(STATE_CODE_WARNING)
        items.append(
            ParsedTaxItem(
                category="state_income_tax_withheld",
                amount_minor=money.magnitude_minor,
                source_form=_SOURCE,
                box="17",
                label="State income tax",
                jurisdiction=code,
            )
        )
    return items, warnings


def _local_items(block: ExtractedDoc) -> list[ParsedTaxItem]:
    items: list[ParsedTaxItem] = []
    names = text_for_label(block, _LOCALITY, mode="below", lines=3)
    name_lines = names.split("\n") if names else []
    for index, (_, money) in enumerate(money_rows_below(block, _LOCAL_TAX)):
        if money.magnitude_minor == 0:
            continue
        raw = name_lines[index] if index < len(name_lines) else None
        jurisdiction = clean_jurisdiction(raw) or "local"
        items.append(
            ParsedTaxItem(
                category="local_income_tax_withheld",
                amount_minor=money.magnitude_minor,
                source_form=_SOURCE,
                box="19",
                label="Local income tax",
                jurisdiction=jurisdiction[:MAX_JURISDICTION_LEN],
            )
        )
    return items


def _parse_block(block: ExtractedDoc, year: int) -> ParsedTaxForm | None:
    """One form from one block, or `None` when the block is not a W-2 (no box 1 label).

    Raises:
        ParserError: the box 1 label is present but has no value.
    """
    if not label_present(block, _WAGES):
        return None
    wages = box_amount(block, _WAGES)
    if wages is None:
        raise ParserError("A W-2 form was found but box 1 (wages) has no value.")
    income: tuple[ParsedIncomeItem, ...] = (
        ParsedIncomeItem(
            category="salary_wages",
            amount_minor=wages,
            source_form=_SOURCE,
            box="1",
            label="Wages, tips, other compensation",
        ),
    )
    memos = [
        ParsedIncomeItem(
            category="hsa_employer" if code == "W" else "retirement_deferral",
            amount_minor=amount,
            source_form=_SOURCE,
            box=f"12-{code}",
            label=f"Box 12 code {code}",
            is_memo=True,
        )
        for slot in _BOX12_SLOTS
        if (found := code_amount_in_cell(block, slot, _BOX12_CODES)) is not None
        for code, amount in (found,)
        if amount
    ]
    income += tuple(memos)
    taxes: list[ParsedTaxItem] = []
    for category, label_re, box, label in (
        ("federal_income_tax_withheld", _FED, "2", "Federal income tax withheld"),
        ("social_security_tax", _SS_TAX, "4", "Social security tax withheld"),
        ("medicare_tax", _MEDICARE_TAX, "6", "Medicare tax withheld"),
    ):
        amount = box_amount(block, label_re)
        if amount:
            taxes.append(
                ParsedTaxItem(
                    category=category,  # type: ignore[arg-type]
                    amount_minor=amount,
                    source_form=_SOURCE,
                    box=box,
                    label=label,
                )
            )
    state_items, warnings = _state_items(block)
    taxes += state_items
    taxes += _local_items(block)

    corrected = corrected_marker(block)
    if corrected:
        warnings.append(CORRECTED_WARNING)
    employer = sanitize_payer_name(
        text_below(block, _EMPLOYER_NAME, skip_re=LABEL_CONTINUATION, max_rows=3)
    )
    return ParsedTaxForm(
        form_type=_FORM,
        tax_year=year,
        payer_name=employer,
        payer_tin_last4=tin_last4(block, _EIN_LABEL),
        account_last4=None,
        is_corrected=corrected,
        filing_status=None,
        agi_minor=None,
        taxable_income_minor=None,
        income=income,
        taxes=tuple(taxes),
        warnings=tuple(check_warning(w) for w in dict.fromkeys(warnings)),
    )


class W2Parser:
    """Parser for Form W-2 (id ``tax_w2``)."""

    id = "tax_w2"
    version = "1"
    form_types: tuple[FormType, ...] = (_FORM,)

    def detect(self, doc: ExtractedDoc) -> float:
        """0.95 with a W-2 title, the employer identification number label and the box 1 label;
        0.6 with the full title and one of them; else 0.0. Never raises."""
        try:
            return _score(doc)
        except (ValueError, IndexError, KeyError):
            return 0.0

    def parse(self, doc: ExtractedDoc, *, tax_year: int | None = None) -> ParsedTaxDocument:
        """Parse every distinct W-2 in `doc` (see module docstring).

        Raises:
            ParserError: tax year undeterminable, box 1 blank, or no W-2 found.
        """
        year = require_year(doc, tax_year)
        blocks = split_blocks(doc, _START, keep_preamble=True) or (doc,)
        has_copy_marker = doc_has(doc, _COPY_MARKER)
        got = collect(blocks, lambda block: _parse_block(block, year), kind="W-2")
        forms: list[ParsedTaxForm] = []
        warnings: list[str] = list(got.warnings)
        for form in got.forms:
            if form in forms:
                if form.payer_tin_last4 is not None or has_copy_marker:
                    continue  # a Copy B/C/2 of a form already read
                warnings.append(_IDENTICAL_WARNING)
            forms.append(form)
        if not forms:
            raise ParserError("No W-2 form with a box 1 amount was found in this file.")
        return build_document(forms, warnings=warnings, unparsed=got.unparsed)


PARSER = W2Parser()
