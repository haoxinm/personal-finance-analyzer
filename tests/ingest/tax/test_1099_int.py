"""Tests for the 1099-INT parser (`tax_1099_int`) and its reusable `parse_int_section`."""

from __future__ import annotations

import dataclasses
import re

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.parsers.f1099_div import PARSER as DIV
from personal_finance_analyzer.ingest.tax.parsers.f1099_int import PARSER, parse_int_section
from personal_finance_analyzer.ingest.tax.parsers.f1099_nec_misc import PARSER as NEC_MISC
from personal_finance_analyzer.ingest.tax.parsers.w2 import PARSER as W2
from tests.ingest.tax_support import (
    assert_matches_golden,
    doc_from_lines,
    extract_doc,
    generated_pdf,
    load_golden,
)

_ID = "tax_1099_int"
_VARIANTS = ("normal", "with_withholding_and_state", "tax_exempt_memo", "zero_interest")
_SSN_SHAPE = re.compile(r"\d{3}-\d{2}-\d{4}")


def _doc(variant: str) -> ExtractedDoc:
    return extract_doc(generated_pdf(_ID, variant))


@pytest.mark.parametrize("variant", _VARIANTS)
def test_matches_golden(variant: str) -> None:
    assert_matches_golden(PARSER.parse(_doc(variant)), load_golden(_ID, variant))


def test_normal_has_two_interest_items() -> None:
    (form,) = PARSER.parse(_doc("normal")).forms
    assert form.form_type == "1099-int"
    assert form.payer_name == "Harbor Mill Savings"
    assert form.payer_tin_last4 == "0003"
    assert form.account_last4 == "6789"
    assert [(i.category, i.box, i.amount_minor) for i in form.income] == [
        ("interest", "1", 123_456),
        ("interest", "3", 24_580),
    ]
    assert not any(i.is_memo for i in form.income)
    assert form.taxes == ()


def test_withholding_is_positive_and_state_has_jurisdiction() -> None:
    (form,) = PARSER.parse(_doc("with_withholding_and_state")).forms
    taxes = {(t.category, t.jurisdiction): t.amount_minor for t in form.taxes}
    assert taxes == {
        ("federal_income_tax_withheld", None): 87_000,
        ("state_income_tax_withheld", "CA"): 12_000,
    }


def test_tax_exempt_interest_is_memo_other() -> None:
    (form,) = PARSER.parse(_doc("tax_exempt_memo")).forms
    memo = [i for i in form.income if i.is_memo]
    assert [(i.category, i.box, i.amount_minor) for i in memo] == [("other", "8", 64_000)]


def test_zero_interest_keeps_box1_and_drops_zero_boxes() -> None:
    (form,) = PARSER.parse(_doc("zero_interest")).forms
    assert [(i.category, i.amount_minor) for i in form.income] == [("interest", 0)]
    assert form.taxes == ()
    assert form.warnings == ()


def test_parse_int_section_signature_and_result() -> None:
    doc = _doc("with_withholding_and_state")
    form = parse_int_section(doc, 2024)
    assert form.tax_year == 2024
    assert form == PARSER.parse(doc).forms[0]
    assert dataclasses.replace(form, account_last4="1111").account_last4 == "1111"


def test_section_without_box1_raises_generic_error() -> None:
    with pytest.raises(ParserError) as info:
        parse_int_section(doc_from_lines(["Form 1099-INT", "nothing useful here"]), 2024)
    assert not re.search(r"\d{6,}", str(info.value))


def test_missing_year_needs_override() -> None:
    lines = ["Form 1099-INT", "PAYER'S TIN", "99-0000003", "1 Interest income", "10.00"]
    with pytest.raises(ParserError):
        PARSER.parse(doc_from_lines(lines))
    assert PARSER.parse(doc_from_lines(lines), tax_year=2024).tax_year == 2024


def test_two_forms_in_one_file_are_split_by_page() -> None:
    first, second = _doc("normal"), _doc("with_withholding_and_state")
    pages = (first.pages[0], dataclasses.replace(second.pages[0], page_number=2))
    combined = dataclasses.replace(first, page_count=2, pages=pages)
    parsed = PARSER.parse(combined)
    assert [f.income[0].amount_minor for f in parsed.forms] == [123_456, 348_000]


def test_same_row_layout_with_leaders_and_parenthetical() -> None:
    lines = [
        "Form 1099-INT   Tax Year 2024",
        "Payer's TIN: 99-0000003",
        "Account number: SYN-000123456789",
        "1 Interest Income .......................... $ 1,000.50",
        "3 Interest on U.S. Savings Bonds and Treasury Obligations ...... 20.00",
        "4 Federal Income Tax Withheld ............... 150.00",
        "8 Tax-Exempt Interest ...................... 75.25",
    ]
    (form,) = PARSER.parse(doc_from_lines(lines)).forms
    assert form.payer_tin_last4 == "0003"
    assert form.account_last4 == "6789"
    assert [(i.box, i.amount_minor, i.is_memo) for i in form.income] == [
        ("1", 100_050, False),
        ("3", 2_000, False),
        ("8", 7_525, True),
    ]
    assert [t.amount_minor for t in form.taxes] == [15_000]


def test_corrected_box_and_withholding_without_income_warn() -> None:
    lines = [
        "[X] CORRECTED (if checked)",
        "Form 1099-INT   Tax Year 2024",
        "1 Interest income .......... 0.00",
        "4 Federal income tax withheld .......... 5.00",
    ]
    (form,) = PARSER.parse(doc_from_lines(lines)).forms
    assert form.is_corrected
    assert len(form.warnings) == 2


@pytest.mark.parametrize("variant", _VARIANTS)
def test_privacy_no_identifiers_leak(variant: str) -> None:
    parsed = PARSER.parse(_doc(variant))
    text = repr(parsed)
    assert not _SSN_SHAPE.search(text)
    assert not re.search(r"\d{9}", text)
    assert "Testperson" not in text
    assert "SYN-0001" not in text


@pytest.mark.parametrize("variant", _VARIANTS)
def test_detection(variant: str) -> None:
    doc = _doc(variant)
    assert PARSER.detect(doc) == 0.9
    for other in (W2, DIV, NEC_MISC):
        assert other.detect(doc) <= 0.5


def test_detection_halves_with_other_sections_or_consolidated_marker() -> None:
    base = ["Form 1099-INT", "PAYER'S TIN", "RECIPIENT'S TIN", "1 Interest income"]
    assert PARSER.detect(doc_from_lines(base)) == 0.9
    assert PARSER.detect(doc_from_lines([*base, "Form 1099-DIV"])) == 0.45
    assert PARSER.detect(doc_from_lines(["Consolidated Form 1099", *base])) == 0.45
    assert PARSER.detect(doc_from_lines(["Hello"])) == 0.0
    assert PARSER.detect(doc_from_lines(["Form 1099-INT"])) == 0.3
    # A 1040 that merely mentions the form is not a 1099-INT.
    mention = ["Form 1040 (2024)", "Tax-exempt interest", "see Form 1099-INT", "Interest income"]
    assert PARSER.detect(doc_from_lines(mention)) <= 0.5
