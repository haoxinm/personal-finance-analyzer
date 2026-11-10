"""Tests for the 1099-NEC / 1099-MISC parser (`tax_1099_nec_misc`)."""

from __future__ import annotations

import dataclasses
import re

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.parsers.f1099_div import PARSER as DIV
from personal_finance_analyzer.ingest.tax.parsers.f1099_int import PARSER as INT
from personal_finance_analyzer.ingest.tax.parsers.f1099_nec_misc import PARSER
from personal_finance_analyzer.ingest.tax.parsers.w2 import PARSER as W2
from tests.ingest.tax_support import (
    assert_matches_golden,
    doc_from_lines,
    extract_doc,
    generated_pdf,
    load_golden,
)

_ID = "tax_1099_nec_misc"
_VARIANTS = ("nec_normal", "misc_rents_royalties", "nec_with_withholding_state")
_SSN_SHAPE = re.compile(r"\d{3}-\d{2}-\d{4}")


def _doc(variant: str) -> ExtractedDoc:
    return extract_doc(generated_pdf(_ID, variant))


@pytest.mark.parametrize("variant", _VARIANTS)
def test_matches_golden(variant: str) -> None:
    assert_matches_golden(PARSER.parse(_doc(variant)), load_golden(_ID, variant))


def test_nec_box1_is_self_employment() -> None:
    (form,) = PARSER.parse(_doc("nec_normal")).forms
    assert form.form_type == "1099-nec"
    assert form.payer_name == "Alder Ridge Brewing Co"
    assert form.payer_tin_last4 == "0002"
    assert form.account_last4 is None
    assert [(i.category, i.source_form, i.amount_minor) for i in form.income] == [
        ("self_employment", "1099-NEC", 925_000)
    ]
    assert form.taxes == ()


def test_nec_withholding_and_state() -> None:
    (form,) = PARSER.parse(_doc("nec_with_withholding_state")).forms
    assert {(t.category, t.jurisdiction): t.amount_minor for t in form.taxes} == {
        ("federal_income_tax_withheld", None): 370_000,
        ("state_income_tax_withheld", "CA"): 92_500,
    }
    assert [i.amount_minor for i in form.income] == [1_850_000]  # box 7 state income is not income


def test_misc_rents_royalties_other_income_are_other_items() -> None:
    (form,) = PARSER.parse(_doc("misc_rents_royalties")).forms
    assert form.form_type == "1099-misc"
    assert form.account_last4 == "6789"
    assert [(i.category, i.box, i.amount_minor) for i in form.income] == [
        ("other", "1", 1_200_000),
        ("other", "2", 345_075),
        ("other", "3", 80_000),
    ]


def test_pre_2020_misc_box7_is_self_employment() -> None:
    lines = [
        "Form 1099-MISC",
        "Tax Year 2018",
        "PAYER'S TIN",
        "RECIPIENT'S TIN",
        "1 Rents ....... 0.00",
        "7 Nonemployee compensation ....... 4,000.00",
    ]
    (form,) = PARSER.parse(doc_from_lines(lines)).forms
    assert form.form_type == "1099-misc"
    assert [(i.category, i.box, i.amount_minor) for i in form.income] == [
        ("self_employment", "7", 400_000)
    ]


def test_misc_without_any_primary_box_raises() -> None:
    with pytest.raises(ParserError) as info:
        PARSER.parse(
            doc_from_lines(
                ["Form 1099-MISC", "Tax Year 2024", "4 Federal income tax withheld 5.00"]
            )
        )
    assert not re.search(r"\d{6,}", str(info.value))


def test_nec_without_box1_raises() -> None:
    with pytest.raises(ParserError):
        PARSER.parse(doc_from_lines(["Form 1099-NEC", "Tax Year 2024"]))


def test_year_override_and_missing_year() -> None:
    lines = ["Form 1099-NEC", "1 Nonemployee compensation ....... 10.00"]
    with pytest.raises(ParserError):
        PARSER.parse(doc_from_lines(lines))
    assert PARSER.parse(doc_from_lines(lines), tax_year=2023).tax_year == 2023


def test_nec_and_misc_pages_in_one_file() -> None:
    nec, misc = _doc("nec_normal"), _doc("misc_rents_royalties")
    pages = (nec.pages[0], dataclasses.replace(misc.pages[0], page_number=2))
    parsed = PARSER.parse(dataclasses.replace(nec, page_count=2, pages=pages))
    assert [f.form_type for f in parsed.forms] == ["1099-nec", "1099-misc"]


@pytest.mark.parametrize("variant", _VARIANTS)
def test_privacy_no_identifiers_leak(variant: str) -> None:
    text = repr(PARSER.parse(_doc(variant)))
    assert not _SSN_SHAPE.search(text)
    assert not re.search(r"\d{9}", text)
    assert "Testperson" not in text


@pytest.mark.parametrize("variant", _VARIANTS)
def test_detection(variant: str) -> None:
    doc = _doc(variant)
    assert PARSER.detect(doc) == 0.9
    for other in (W2, INT, DIV):
        assert other.detect(doc) <= 0.5


def test_detection_halves_with_other_sections() -> None:
    base = ["Form 1099-NEC", "PAYER'S TIN", "RECIPIENT'S TIN", "1 Nonemployee compensation"]
    assert PARSER.detect(doc_from_lines(base)) == 0.9
    assert PARSER.detect(doc_from_lines([*base, "Form 1099-MISC"])) == 0.9  # same parser
    assert PARSER.detect(doc_from_lines([*base, "Form 1099-INT"])) == 0.45
    assert PARSER.detect(doc_from_lines(["Hello"])) == 0.0
