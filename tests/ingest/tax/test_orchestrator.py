"""Tests for `ingest.tax.orchestrator.parse_document`: every golden fixture, mixed packages,
side-by-side / stacked / multi-page forms, derived 1099-B totals and partial success."""

from __future__ import annotations

import json
import re

import pytest

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.tax_types import ParsedTaxDocument
from personal_finance_analyzer.ingest.tax.orchestrator import parse_document
from tests.ingest.tax_support import (
    GOLDEN_DIR,
    assert_matches_golden,
    doc_from_lines,
    doc_from_pages,
    extract_doc,
    generated_pdf,
    load_golden,
)

_CASES = sorted(
    (path.parent.name, path.stem.removeprefix(f"{path.parent.name}_"))
    for path in GOLDEN_DIR.glob("*/*.json")
    if "expect_error" not in json.loads(path.read_text())
)


def _parse(layout: str, variant: str) -> ParsedTaxDocument:
    return parse_document(extract_doc(generated_pdf(layout, variant))).document


@pytest.mark.parametrize(("layout", "variant"), _CASES, ids=[f"{a}/{b}" for a, b in _CASES])
def test_every_fixture_matches_its_golden_through_the_orchestrator(
    layout: str, variant: str
) -> None:
    """Single-form files take the unchanged path; geometry and package fixtures are segmented."""
    assert_matches_golden(_parse(layout, variant), load_golden(layout, variant))


def test_there_are_goldens_for_the_segmentation_fixtures() -> None:
    names = {f"{a}/{b}" for a, b in _CASES}
    assert {
        "tax_w2/side_by_side_2up",
        "tax_w2/four_up",
        "tax_1099_int/two_payers_stacked",
        "tax_1099_div/two_payers_side_by_side",
        "tax_1099_broker/two_up",
        "tax_mixed_package/w2_int_div",
    } <= names


def test_single_form_file_reports_its_parser() -> None:
    outcome = parse_document(extract_doc(generated_pdf("tax_w2", "normal")))
    assert (outcome.parser_ids, outcome.parser_id) == (("tax_w2",), "tax_w2")


def test_mixed_package_uses_one_parser_per_form_and_reports_multi() -> None:
    outcome = parse_document(extract_doc(generated_pdf("tax_mixed_package", "w2_int_div")))
    assert [f.form_type for f in outcome.document.forms] == ["w2", "1099-int", "1099-div"]
    assert outcome.parser_id == "multi"
    assert set(outcome.parser_ids) == {"tax_w2", "tax_1099_int", "tax_1099_div"}


def test_instruction_page_naming_every_form_creates_no_form() -> None:
    parsed = _parse("tax_mixed_package", "with_instruction_page")
    assert [f.form_type for f in parsed.forms] == ["w2", "1099-int", "1099-div"]
    assert parsed.unparsed == () and parsed.warnings == ()


@pytest.mark.parametrize(
    ("layout", "variant", "count"),
    [
        ("tax_w2", "side_by_side_2up", 2),
        ("tax_w2", "four_up", 2),
        ("tax_w2", "stacked_two_per_page", 2),
        ("tax_w2", "multipage_one_form", 1),
        ("tax_w2", "copies_on_separate_pages", 1),
        ("tax_1099_int", "two_payers_side_by_side", 2),
        ("tax_1099_int", "two_payers_stacked", 2),
        ("tax_1099_int", "continued_page", 1),
        ("tax_1099_div", "two_payers_side_by_side", 2),
    ],
)
def test_form_counts_for_geometry_variants(layout: str, variant: str, count: int) -> None:
    parsed = _parse(layout, variant)
    assert len(parsed.forms) == count
    assert parsed.unparsed == ()


def test_side_by_side_forms_keep_each_payer_with_its_own_amounts() -> None:
    first, second = _parse("tax_1099_int", "two_payers_side_by_side").forms
    assert (first.payer_name, first.income[0].amount_minor) == ("Harbor Mill Savings", 123_456)
    assert (second.payer_name, second.income[0].amount_minor) == (
        "Willow Bend Credit Union",
        64_200,
    )


def test_multipage_w2_keeps_every_state_line() -> None:
    (form,) = _parse("tax_w2", "multipage_one_form").forms
    states = sorted(
        t.jurisdiction or "" for t in form.taxes if t.category == "state_income_tax_withheld"
    )
    assert states == ["CA", "OR"]


# --------------------------------------------------------------------------------------------
# Broker statement
# --------------------------------------------------------------------------------------------


def test_statement_reads_both_columns_and_every_section() -> None:
    (form,) = _parse("tax_1099_broker", "two_up").forms
    sources = {item.source_form for item in form.income}
    assert sources == {"1099-DIV", "1099-INT", "1099-OID", "1099-MISC", "1099-B"}
    assert (form.payer_name, form.payer_tin_last4, form.account_last4) == (
        "Copperfield Securities",
        "0004",
        "7731",
    )


@pytest.mark.parametrize("variant", ["two_up", "b_category_subtotals", "b_detail_only"])
def test_1099_b_totals_are_derived_once_whatever_is_printed(variant: str) -> None:
    """Net lines, covered/noncovered subtotals or bare trades all give the same gain: security
    subtotals and statement totals are never added on top of the trades."""
    (form,) = _parse("tax_1099_broker", variant).forms
    gains = {i.box: i.amount_minor for i in form.income if i.source_form == "1099-B"}
    assert gains == {"ST": -150_000, "LT": 380_000}


def test_instruction_text_and_unheaded_table_add_nothing() -> None:
    (form,) = _parse("tax_1099_broker", "two_up").forms
    misc = [i for i in form.income if i.source_form == "1099-MISC"]
    assert [i.amount_minor for i in misc] == [0, 0]  # the neighbouring table's 125.00 is not read
    assert not any(t.source_form == "1099-MISC" for t in form.taxes)


def test_all_zero_sections_are_recognised_not_unrecognised() -> None:
    parsed = _parse("tax_1099_broker", "zero_sections")
    (form,) = parsed.forms
    assert {i.source_form for i in form.income} == {
        "1099-DIV",
        "1099-INT",
        "1099-OID",
        "1099-MISC",
    }
    assert all(i.amount_minor == 0 for i in form.income)
    assert form.taxes == () and parsed.warnings == () and form.warnings == ()
    assert parsed.unparsed == ()


def test_section_continued_on_the_next_page_is_merged() -> None:
    (form,) = _parse("tax_1099_broker", "section_continued").forms
    box12 = [i for i in form.income if i.box == "12"]
    assert [i.amount_minor for i in box12] == [4_500]


# --------------------------------------------------------------------------------------------
# Partial success
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layout", "form_type"), [("tax_1099_int", "1099-int"), ("tax_1099_div", "1099-div")]
)
def test_one_unreadable_form_does_not_discard_the_others(layout: str, form_type: str) -> None:
    parsed = _parse(layout, "second_form_blank_box1")
    assert [f.form_type for f in parsed.forms] == [form_type]
    assert len(parsed.warnings) == 1 and "skipped" in parsed.warnings[0]
    (region,) = parsed.unparsed
    assert (region.kind, region.pages) == (
        layout.removeprefix("tax_").upper().replace("_", "-"),
        (1,),
    )
    assert not re.search(r"\d{6}", parsed.warnings[0])


def test_every_form_unreadable_is_still_an_error() -> None:
    with pytest.raises(ParserError):
        parse_document(extract_doc(generated_pdf("tax_w2", "no_box1")))


def test_unrecognised_file_is_unsupported() -> None:
    with pytest.raises(UnsupportedLayoutError):
        parse_document(doc_from_lines(["Grocery receipt", "Milk 3.49", "Bread 2.99"]))


def test_unrecognised_section_with_amounts_is_reported_not_fatal() -> None:
    doc = doc_from_pages(
        [
            ["Tax Year 2024", "Form 1099-INT Interest Income", "1 Interest income   25.00"],
            [
                "Tax Year 2024",
                "Form 1099-R Distributions From Pensions",
                "1 Gross distribution   1,234.00",
            ],
        ]
    )
    outcome = parse_document(doc)
    assert [f.form_type for f in outcome.document.forms] == ["1099-int"]
    (region,) = outcome.document.unparsed
    assert (region.kind, region.pages, region.reason) == ("1099-R", (2,), "unrecognized")
    assert any("could not be recognized" in w for w in outcome.document.warnings)


def test_forms_for_another_year_are_dropped_with_a_warning() -> None:
    """The file's year is the most common one (ties: the first form's); other forms are
    reported rather than silently mixed in or failing the whole file."""
    doc = doc_from_pages(
        [
            ["Tax Year 2024", "Form 1099-INT Interest Income", "1 Interest income   25.00"],
            [
                "Tax Year 2023",
                "Form 1099-DIV Dividends and Distributions",
                "1a Total ordinary dividends   9.00",
            ],
        ]
    )
    document = parse_document(doc).document
    assert document.tax_year == 2024
    assert [f.form_type for f in document.forms] == ["1099-int"]
    assert "A form for a different tax year was skipped." in document.warnings


def test_dollar_signs_spaces_leaders_and_parenthesised_losses() -> None:
    doc = doc_from_lines(
        [
            "Consolidated Form 1099 Tax Information",
            "Tax Year 2024",
            "Form 1 0 9 9 - D I V 2024 Dividends",
            "1a Total Ordinary Dividends ........ $ 2,000.00",
            "1b Qualified Dividends ........ $ 1,500.00",
            "Form 1099-B Proceeds",
            "SHORT TERM CAPITAL GAINS AND LOSSES",
            "NET SHORT TERM CAPITAL GAINS AND LOSSES 1,000.00 1,500.00 0.00 0.00 ( 500.00 )",
        ]
    )
    (form,) = parse_document(doc).document.forms
    assert [(i.box, i.amount_minor) for i in form.income] == [
        ("1a", 200_000),
        ("1b", 150_000),
        ("ST", -50_000),
    ]
