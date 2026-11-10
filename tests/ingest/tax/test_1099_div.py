"""Tests for the 1099-DIV parser (`tax_1099_div`) and its reusable `parse_div_section`."""

from __future__ import annotations

import re

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.parsers.f1099_div import PARSER, parse_div_section
from personal_finance_analyzer.ingest.tax.parsers.f1099_int import PARSER as INT
from personal_finance_analyzer.ingest.tax.parsers.f1099_nec_misc import PARSER as NEC_MISC
from personal_finance_analyzer.ingest.tax.parsers.w2 import PARSER as W2
from tests.ingest.tax_support import (
    assert_matches_golden,
    doc_from_lines,
    extract_doc,
    generated_pdf,
    load_golden,
)

_ID = "tax_1099_div"
_VARIANTS = ("normal", "qualified_gt_ordinary", "exempt_interest_memo")
_SSN_SHAPE = re.compile(r"\d{3}-\d{2}-\d{4}")


def _doc(variant: str) -> ExtractedDoc:
    return extract_doc(generated_pdf(_ID, variant))


@pytest.mark.parametrize("variant", _VARIANTS)
def test_matches_golden(variant: str) -> None:
    assert_matches_golden(PARSER.parse(_doc(variant)), load_golden(_ID, variant))


def test_normal_items() -> None:
    (form,) = PARSER.parse(_doc("normal")).forms
    assert form.form_type == "1099-div"
    assert form.payer_name == "Copperfield Securities"
    assert form.payer_tin_last4 == "0004"
    assert form.account_last4 == "6789"
    items = {i.category: i for i in form.income}
    assert items["ordinary_dividends"].amount_minor == 250_000
    assert items["qualified_dividends"].is_memo
    assert items["qualified_dividends"].amount_minor == 180_000
    assert not items["ordinary_dividends"].is_memo
    assert items["long_term_capital_gain"].amount_minor == 64_000
    assert items["long_term_capital_gain"].box == "2a"
    assert [(t.category, t.amount_minor) for t in form.taxes] == [
        ("federal_income_tax_withheld", 25_000)
    ]


def test_qualified_greater_than_ordinary_warns() -> None:
    (form,) = PARSER.parse(_doc("qualified_gt_ordinary")).forms
    assert len(form.warnings) == 1
    assert "box 1b" in form.warnings[0]


def test_exempt_interest_dividends_are_memo_other() -> None:
    (form,) = PARSER.parse(_doc("exempt_interest_memo")).forms
    memo = [i for i in form.income if i.is_memo]
    assert {(i.category, i.box) for i in memo} == {("qualified_dividends", "1b"), ("other", "12")}
    assert form.warnings == ()


def test_parse_div_section_signature() -> None:
    doc = _doc("normal")
    assert parse_div_section(doc, 2024) == PARSER.parse(doc).forms[0]


def test_section_without_box_1a_raises_generic_error() -> None:
    with pytest.raises(ParserError) as info:
        parse_div_section(doc_from_lines(["Form 1099-DIV", "1b Qualified dividends 5.00"]), 2024)
    assert not re.search(r"\d{6,}", str(info.value))


def test_same_row_layout_and_state_withholding() -> None:
    lines = [
        "Form 1099-DIV   Tax Year 2024",
        "1a Total Ordinary Dividends (includes lines 1b, 5, 2e) ....... 1,200.00",
        "1b Qualified Dividends ....... 900.00",
        "2a Total Capital Gain Distributions ....... 100.00",
        "4 Federal Income Tax Withheld ....... 0.00",
        "16 State Tax Withheld CA ....... 30.00",
    ]
    (form,) = PARSER.parse(doc_from_lines(lines)).forms
    assert [(i.category, i.amount_minor) for i in form.income] == [
        ("ordinary_dividends", 120_000),
        ("qualified_dividends", 90_000),
        ("long_term_capital_gain", 10_000),
    ]
    assert [(t.category, t.jurisdiction, t.amount_minor) for t in form.taxes] == [
        ("state_income_tax_withheld", "CA", 3_000)
    ]


def test_zero_ordinary_dividends_kept() -> None:
    lines = ["Form 1099-DIV  2024 ", "Tax Year 2024", "Total ordinary dividends  0.00"]
    (form,) = PARSER.parse(doc_from_lines(lines)).forms
    assert [(i.category, i.amount_minor) for i in form.income] == [("ordinary_dividends", 0)]


@pytest.mark.parametrize("variant", _VARIANTS)
def test_privacy_no_identifiers_leak(variant: str) -> None:
    parsed = PARSER.parse(_doc(variant))
    text = repr(parsed)
    assert not _SSN_SHAPE.search(text)
    assert not re.search(r"\d{9}", text)
    assert "Testperson" not in text


@pytest.mark.parametrize("variant", _VARIANTS)
def test_detection(variant: str) -> None:
    doc = _doc(variant)
    assert PARSER.detect(doc) == 0.9
    for other in (W2, INT, NEC_MISC):
        assert other.detect(doc) <= 0.5


def test_detection_halves_with_other_sections() -> None:
    base = ["Form 1099-DIV", "Total ordinary dividends", "Qualified dividends"]
    assert PARSER.detect(doc_from_lines(base)) == 0.9
    assert PARSER.detect(doc_from_lines([*base, "Form 1099-B"])) == 0.45
    assert PARSER.detect(doc_from_lines(["Consolidated Form 1099", *base])) == 0.45
    assert PARSER.detect(doc_from_lines(["Nothing"])) == 0.0
