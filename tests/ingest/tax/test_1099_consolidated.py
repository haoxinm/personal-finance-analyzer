"""Tests for the consolidated 1099 parser (`tax_1099_consolidated`)."""

from __future__ import annotations

import json
import re

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import ParsedTaxDocument
from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.tax.common import parsing_with_pii_terms
from personal_finance_analyzer.ingest.tax.parsers import f1099_b
from personal_finance_analyzer.ingest.tax.parsers.f1099_consolidated import PARSER
from tests.ingest.tax_support import (
    assert_matches_golden,
    doc_from_lines,
    extract_doc,
    generated_pdf,
    load_golden,
    parsed_to_dict,
)

LAYOUT = "tax_1099_consolidated"
VARIANTS = ("int_div_b", "int_div_b_misc", "div_only", "multi_account")


def _parse(variant: str) -> ParsedTaxDocument:
    return PARSER.parse(extract_doc(generated_pdf(LAYOUT, variant)))


IDENTITY_VARIANTS = ("alt_labels", "two_payers_one_file", "noid_a", "noid_b", "noid_anon")

_HEADER = [
    "Consolidated Form 1099 Tax Information",
    "Tax Year 2024",
    "Payer name: Copperfield Securities",
    "Form 1099 Summary",
    "Account number: SYN-000123456789",
]


@pytest.mark.parametrize("variant", VARIANTS)
def test_matches_golden(variant: str) -> None:
    assert_matches_golden(_parse(variant), load_golden(LAYOUT, variant))


@pytest.mark.parametrize("variant", IDENTITY_VARIANTS)
def test_identity_variants_match_golden(variant: str) -> None:
    assert_matches_golden(_parse(variant), load_golden(LAYOUT, variant))


def test_alt_labels_amounts_and_identity_are_read() -> None:
    """`Payer:` / `Payer EIN:` / `Account No.` and `Box n` / `1a Total ...` wording."""
    (form,) = _parse("alt_labels").forms
    assert (form.payer_name, form.payer_tin_last4, form.account_last4) == (
        "Copperfield Securities",
        "0004",
        "6789",
    )
    assert [(i.box, i.amount_minor) for i in form.income] == [
        ("1", 123_456),
        ("1a", 245_075),
        ("2a", 31_040),
    ]


def test_two_payers_one_file_reads_payer_and_tin_per_account() -> None:
    first, second = _parse("two_payers_one_file").forms
    assert (first.payer_name, first.payer_tin_last4, first.account_last4) == (
        "Alder Brokerage Group",
        "0031",
        "2222",
    )
    assert (second.payer_name, second.payer_tin_last4, second.account_last4) == (
        "Birchwood Securities",
        "0032",
        "4444",
    )


def test_unknown_amount_wording_emits_no_empty_form() -> None:
    with pytest.raises(ParserError) as err:
        _parse("no_amounts")
    assert not re.search(r"\d{6}", str(err.value))


def test_account_without_amounts_is_not_emitted() -> None:
    lines = [
        *_HEADER,
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "Account number: SYN-000555555555",
        "1099-DIV  Dividends and Distributions",
        "Dividends received this year   9.00",
    ]
    parsed = PARSER.parse(doc_from_lines(lines))
    assert [f.account_last4 for f in parsed.forms] == ["6789"]


def test_payer_inferred_from_page_header_with_warning() -> None:
    lines = [
        "Maple Street Brokers",
        "Consolidated Form 1099 Tax Information",
        "Tax Year 2024",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
    ]
    form = PARSER.parse(doc_from_lines(lines)).forms[0]
    assert form.payer_name == "Maple Street Brokers"
    assert "Payer name was inferred from the page header." in form.warnings


@pytest.mark.parametrize("variant", VARIANTS)
def test_detect_and_privacy(variant: str) -> None:
    doc = extract_doc(generated_pdf(LAYOUT, variant))
    assert PARSER.detect(doc) == pytest.approx(0.95)
    assert f1099_b.PARSER.detect(doc) <= 0.5
    parsed = PARSER.parse(doc)
    blob = repr(parsed) + json.dumps(parsed_to_dict(parsed))
    assert not re.search(r"\d{3}-\d{2}-\d{4}|\d{9}", blob)
    assert "Testperson" not in blob
    for form in parsed.forms:
        assert form.form_type == "1099-consolidated"
        assert form.payer_tin_last4 == "0004"
        assert form.account_last4 is not None and len(form.account_last4) == 4


def test_standalone_1099_b_is_not_consolidated() -> None:
    doc = extract_doc(generated_pdf("tax_1099_b", "normal_summary"))
    assert PARSER.detect(doc) == 0.0


def test_int_div_b_items_keep_their_source_form() -> None:
    form = _parse("int_div_b").forms[0]
    assert [(i.source_form, i.category, i.is_memo) for i in form.income] == [
        ("1099-INT", "interest", False),
        ("1099-INT", "interest", False),
        ("1099-INT", "other", True),
        ("1099-DIV", "ordinary_dividends", False),
        ("1099-DIV", "qualified_dividends", True),
        ("1099-DIV", "long_term_capital_gain", False),
        ("1099-B", "short_term_capital_gain", False),
        ("1099-B", "long_term_capital_gain", False),
    ]
    assert [(t.source_form, t.amount_minor) for t in form.taxes] == [("1099-DIV", 2_500)]


def test_misc_section_and_state_withholding() -> None:
    form = _parse("int_div_b_misc").forms[0]
    misc = [i for i in form.income if i.source_form == "1099-MISC"]
    assert [(i.category, i.amount_minor) for i in misc] == [
        ("other", 120_000),
        ("other", 34_000),
        ("other", 7_550),
    ]
    taxes = {(t.source_form, t.category, t.jurisdiction): t.amount_minor for t in form.taxes}
    assert taxes[("1099-MISC", "federal_income_tax_withheld", None)] == 1_000
    assert taxes[("1099-DIV", "state_income_tax_withheld", "CA")] == 1_200


def test_div_only_single_section() -> None:
    form = _parse("div_only").forms[0]
    assert {i.source_form for i in form.income} == {"1099-DIV"}


def test_multi_account_emits_one_form_per_account() -> None:
    parsed = _parse("multi_account")
    assert [f.account_last4 for f in parsed.forms] == ["6789", "4321"]
    first, second = parsed.forms
    assert {i.source_form for i in first.income} == {"1099-DIV", "1099-INT"}
    assert {i.source_form for i in second.income} == {"1099-B", "1099-DIV"}
    ltcg = [i.amount_minor for i in second.income if i.source_form == "1099-B"]
    assert ltcg == [-250_000, -60_000]  # net losses stay negative (D6)
    assert [t.amount_minor for t in second.taxes] == [4_000]


def test_qualified_over_ordinary_warns() -> None:
    lines = [
        *_HEADER,
        "1099-DIV  Dividends and Distributions",
        "Ordinary dividends (Box 1a)           100.00",
        "Qualified dividends (Box 1b)          150.00",
    ]
    parsed = PARSER.parse(doc_from_lines(lines))
    assert parsed.forms[0].warnings == ("1099-DIV qualified dividends exceed ordinary dividends.",)


def test_unsupported_and_empty_sections_warn() -> None:
    lines = [
        *_HEADER,
        "1099-R  Distributions From Pensions",
        "Gross distribution (Box 1)   10.00",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "1099-DIV  Dividends and Distributions",
        "1099-B  Proceeds From Broker",
    ]
    form = PARSER.parse(doc_from_lines(lines)).forms[0]
    assert [(i.category, i.amount_minor) for i in form.income] == [("interest", 2_500)]
    assert set(form.warnings) == {
        "A 1099-R section is not supported and was skipped.",
        "The 1099-DIV section contained no recognizable amounts.",
        "The 1099-B section contained no readable totals.",
    }


def test_without_account_rows_the_whole_document_is_one_account() -> None:
    lines = [
        "Consolidated Form 1099 Tax Information",
        "Tax Year 2024",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
    ]
    form = PARSER.parse(doc_from_lines(lines)).forms[0]
    assert form.account_last4 is None and form.payer_name is None


def test_no_sections_is_a_parser_error() -> None:
    with pytest.raises(ParserError) as err:
        PARSER.parse(doc_from_lines([*_HEADER]))
    assert not re.search(r"\d{6}", str(err.value))


def test_year_override_and_missing_year() -> None:
    lines = [h for h in _HEADER if "Tax Year" not in h]
    lines += ["1099-INT  Interest Income", "Interest income (Box 1)   25.00"]
    doc = doc_from_lines(lines)
    with pytest.raises(ParserError):
        PARSER.parse(doc)
    assert PARSER.parse(doc, tax_year=2023).tax_year == 2023


def test_detect_variants() -> None:
    two = ["1099-INT  Interest Income", "1099-DIV  Dividends"]
    assert PARSER.detect(doc_from_lines(two)) == pytest.approx(0.6)
    assert PARSER.detect(doc_from_lines([*_HEADER, *two])) == pytest.approx(0.95)
    assert PARSER.detect(doc_from_lines(["1099-INT  Interest Income"])) == 0.0
    assert PARSER.detect(doc_from_lines(["Form 1099 Summary"])) == 0.0
    assert PARSER.detect(doc_from_lines(["Wage and Tax Statement 2024"])) == 0.0
    assert (
        PARSER.detect(
            doc_from_lines(["Form 1040  U.S. Individual Income Tax Return", *_HEADER, *two])
        )
        == 0.0
    )


_STMT_HEADER = ["Consolidated Form 1099 Tax Information", "Tax Year 2024"]


def test_all_zero_oid_section_is_recognised_without_a_warning() -> None:
    lines = [
        *_STMT_HEADER,
        "Summary of 2024 Original Issue Discount Items",
        "Original Issue Discount ........ 0.00",
        "OID on U.S. Treasury Obligations ........ 0.00",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
    ]
    form = PARSER.parse(doc_from_lines(lines)).forms[0]
    oid = [(i.box, i.amount_minor) for i in form.income if i.source_form == "1099-OID"]
    assert oid == [("1", 0), ("8", 0)]
    assert not any("OID" in w for w in form.warnings)


def test_state_code_on_its_own_row_labels_the_withholding() -> None:
    lines = [
        *_STMT_HEADER,
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "15 State   CA",
        "17 State Tax Withheld   3.50",
    ]
    (tax,) = PARSER.parse(doc_from_lines(lines)).forms[0].taxes
    assert (tax.category, tax.amount_minor, tax.jurisdiction) == (
        "state_income_tax_withheld",
        350,
        "CA",
    )


def test_savings_bond_abbreviation_and_box_prefixes() -> None:
    lines = [
        *_STMT_HEADER,
        "1099-INT  Interest Income",
        "1 Interest Income   25.00",
        "3 Int. on U.S. Savings Bonds & Treas.   4.00",
    ]
    form = PARSER.parse(doc_from_lines(lines)).forms[0]
    assert [(i.box, i.amount_minor) for i in form.income] == [("1", 2_500), ("3", 400)]


def test_repeated_section_heading_is_merged_before_parsing() -> None:
    lines = [
        *_STMT_HEADER,
        "1099-DIV  Dividends and Distributions",
        "Ordinary dividends (Box 1a)   100.00",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "1099-DIV  Dividends and Distributions (continued)",
        "Exempt-interest dividends (Box 12)   7.00",
    ]
    form = PARSER.parse(doc_from_lines(lines)).forms[0]
    assert [(i.source_form, i.box, i.amount_minor) for i in form.income] == [
        ("1099-DIV", "1a", 10_000),
        ("1099-DIV", "12", 700),
        ("1099-INT", "1", 2_500),
    ]


def test_account_number_printed_beneath_its_label() -> None:
    lines = [
        *_STMT_HEADER,
        "Account No.            Taxpayer No.",
        "XQ-4455-7731           XXX-XX-1234",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
    ]
    assert PARSER.parse(doc_from_lines(lines)).forms[0].account_last4 == "7731"


def test_section_without_amounts_is_reported_as_an_unparsed_region() -> None:
    lines = [
        *_STMT_HEADER,
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "1099-DIV  Dividends and Distributions",
        "Dividends paid to you   9.00",
    ]
    parsed = PARSER.parse(doc_from_lines(lines))
    assert [(r.kind, r.reason) for r in parsed.unparsed] == [("1099-DIV", "no amounts")]


def _header_doc(first_line: str) -> ExtractedDoc:
    return doc_from_lines(
        [
            first_line,
            "Consolidated Form 1099 Tax Information",
            "Tax Year 2024",
            "1099-INT  Interest Income",
            "Interest income (Box 1)   25.00",
        ]
    )


def test_header_line_without_company_evidence_is_not_a_payer() -> None:
    form = PARSER.parse(_header_doc("Jane Q Sample")).forms[0]
    assert form.payer_name is None
    assert "Payer name was inferred from the page header." not in form.warnings


def test_header_company_matching_a_pii_term_is_not_a_payer() -> None:
    doc = _header_doc("Sample Family Trust")
    assert PARSER.parse(doc).forms[0].payer_name == "Sample Family Trust"
    with parsing_with_pii_terms(["Sample Family"]):
        assert PARSER.parse(doc).forms[0].payer_name is None


def test_payer_printed_after_a_later_accounts_row_is_that_accounts_payer() -> None:
    lines = [
        "Consolidated Form 1099 Tax Information",
        "Tax Year 2024",
        "Payer: Alder Brokerage Group",
        "Account number: 1111",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "Account number: 2222",
        "Payer: Birchwood Securities",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   30.00",
    ]
    forms = PARSER.parse(doc_from_lines(lines)).forms
    assert [(f.payer_name, f.account_last4) for f in forms] == [
        ("Alder Brokerage Group", "1111"),
        ("Birchwood Securities", "2222"),
    ]


def test_one_payer_heading_several_accounts_is_kept_for_each() -> None:
    lines = [
        "Consolidated Form 1099 Tax Information",
        "Tax Year 2024",
        "Payer: Alder Brokerage Group",
        "Account number: 1111",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   25.00",
        "Account number: 2222",
        "1099-INT  Interest Income",
        "Interest income (Box 1)   30.00",
    ]
    forms = PARSER.parse(doc_from_lines(lines)).forms
    assert [f.payer_name for f in forms] == ["Alder Brokerage Group"] * 2
