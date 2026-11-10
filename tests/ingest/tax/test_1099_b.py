"""Tests for the Form 1099-B parser (`tax_1099_b`) against the generated fixtures plus inline docs."""

from __future__ import annotations

import json
import re

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.tax_types import ParsedTaxDocument
from personal_finance_analyzer.ingest.tax.parsers.f1099_b import PARSER, parse_b_section
from tests.fixtures.gen.base import format_row
from tests.fixtures.gen.tax_1099_b import Lot, section_lines
from tests.ingest.tax_support import (
    assert_matches_golden,
    doc_from_lines,
    doc_from_pages,
    extract_doc,
    generated_pdf,
    load_golden,
    parsed_to_dict,
)

LAYOUT = "tax_1099_b"
VARIANTS = ("normal_summary", "transactions_only", "loss_year", "wash_sale", "mismatch")


def _parse(variant: str) -> ParsedTaxDocument:
    return PARSER.parse(extract_doc(generated_pdf(LAYOUT, variant)))


def _form_lines(lots: list[Lot], *, summary: bool, detail: bool) -> list[str]:
    return [
        "Form 1099-B  Proceeds From Broker",
        "Tax Year 2024",
        *section_lines(lots, heading="x", summary=summary, detail=detail)[2:],
    ]


_ST = Lot("short", True, "10 sh AAA", "01/02/2024", "02/03/2024", 100_000, 80_000)
_LT = Lot("long", True, "10 sh BBB", "01/02/2021", "02/03/2024", 90_000, 100_000)


@pytest.mark.parametrize("variant", VARIANTS)
def test_matches_golden(variant: str) -> None:
    assert_matches_golden(_parse(variant), load_golden(LAYOUT, variant))


@pytest.mark.parametrize("variant", VARIANTS)
def test_detect_and_privacy(variant: str) -> None:
    doc = extract_doc(generated_pdf(LAYOUT, variant))
    assert PARSER.detect(doc) > 0.5
    parsed = PARSER.parse(doc)
    blob = repr(parsed) + json.dumps(parsed_to_dict(parsed))
    assert not re.search(r"\d{3}-\d{2}-\d{4}|\d{9}", blob)
    assert "Testperson" not in blob
    form = parsed.forms[0]
    assert form.form_type == "1099-b"
    assert (form.payer_tin_last4, form.account_last4) == ("0004", "6789")
    assert all(not has for has in (form.is_corrected,))


def test_normal_summary_signs_and_categories() -> None:
    form = _parse("normal_summary").forms[0]
    by_cat = {i.category: i.amount_minor for i in form.income}
    assert by_cat == {"short_term_capital_gain": 60_000, "long_term_capital_gain": 370_000}
    assert {(t.category, t.jurisdiction): t.amount_minor for t in form.taxes} == {
        ("federal_income_tax_withheld", None): 15_000,
        ("state_income_tax_withheld", "CA"): 4_500,
    }
    assert all(t.amount_minor > 0 for t in form.taxes)


def test_transactions_only_sums_rows_to_the_same_totals() -> None:
    summary = {i.category: i.amount_minor for i in _parse("normal_summary").forms[0].income}
    rows = {i.category: i.amount_minor for i in _parse("transactions_only").forms[0].income}
    assert rows == summary


def test_loss_year_short_term_is_negative() -> None:
    form = _parse("loss_year").forms[0]
    short = next(i for i in form.income if i.category == "short_term_capital_gain")
    assert short.amount_minor == -320_050
    assert form.taxes == ()


def test_wash_sale_reconciles_without_warnings() -> None:
    parsed = _parse("wash_sale")
    assert parsed.forms[0].warnings == ()
    short = next(i for i in parsed.forms[0].income if i.category == "short_term_capital_gain")
    assert short.amount_minor == -20_000  # (2,000 - 3,000 + 400) + (4,500 - 4,100)


def test_mismatch_warns_once_and_keeps_printed_net() -> None:
    form = _parse("mismatch").forms[0]
    assert len(form.warnings) == 1 and "reconcile" in form.warnings[0]
    assert "short-term" in form.warnings[0]
    short = next(i for i in form.income if i.category == "short_term_capital_gain")
    assert short.amount_minor == 55_000


def test_parse_b_section_on_a_section_block() -> None:
    doc = doc_from_lines(
        section_lines([_ST, _LT], heading="1099-B  Sales", summary=True, detail=False)
    )
    form = parse_b_section(doc, 2024)
    assert form.tax_year == 2024
    assert [(i.category, i.amount_minor) for i in form.income] == [
        ("short_term_capital_gain", 20_000),
        ("long_term_capital_gain", -10_000),
    ]
    assert form.payer_name is None and form.account_last4 is None


def test_detail_rows_not_matching_summary_warn() -> None:
    lines = section_lines(
        [_ST], heading="1099-B", summary=True, detail=True, printed_net={("short", True): 25_000}
    )
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert any("do not add up" in w for w in form.warnings)
    assert any("reconcile" in w for w in form.warnings)


def test_ordinary_term_becomes_other_with_warning() -> None:
    lines = [
        "Summary of Sales by Term",
        format_row(
            ["Ordinary/undetermined", "500.00", "100.00", "0.00", "400.00"], (26, 12, 12, 12, 12)
        ),
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.category, i.amount_minor) for i in form.income] == [("other", 40_000)]
    assert any("other income" in w for w in form.warnings)


def test_zero_withholding_is_not_emitted() -> None:
    form = parse_b_section(
        doc_from_lines(section_lines([_ST], heading="1099-B", summary=True, detail=False)), 2024
    )
    assert form.taxes == ()


def test_no_totals_is_a_parser_error_without_digits() -> None:
    with pytest.raises(ParserError) as err:
        parse_b_section(doc_from_lines(["Form 1099-B", "Tax Year 2024"]), 2024)
    assert not re.search(r"\d{6}", str(err.value))


def test_missing_year_needs_override() -> None:
    lines = [
        "Form 1099-B  Proceeds",
        *section_lines([_ST], heading="x", summary=True, detail=False)[2:],
    ]
    doc = doc_from_lines(lines)
    with pytest.raises(ParserError):
        PARSER.parse(doc)
    assert PARSER.parse(doc, tax_year=2023).tax_year == 2023


def test_two_forms_in_one_file() -> None:
    one = _form_lines([_ST], summary=True, detail=False)
    parsed = PARSER.parse(doc_from_lines([*one, "", *one]))
    assert len(parsed.forms) == 2


def test_detect_halves_when_another_1099_section_is_present() -> None:
    base = _form_lines([_ST], summary=True, detail=False)
    assert PARSER.detect(doc_from_lines(base)) == pytest.approx(0.9)
    assert PARSER.detect(doc_from_lines([*base, "1099-INT  Interest Income"])) == pytest.approx(
        0.45
    )


def test_detect_rejects_unrelated_documents() -> None:
    assert PARSER.detect(doc_from_lines(["Wage and Tax Statement 2024"])) == 0.0
    assert PARSER.detect(doc_from_lines(["Form 1099-B"])) == 0.0
    assert (
        PARSER.detect(
            doc_from_lines(
                ["Form 1040  U.S. Individual Income Tax Return", "Form 1099-B proceeds cost basis"]
            )
        )
        == 0.0
    )


# --------------------------------------------------------------------------------------------
# Totals derived from term nets / category subtotals / trades (broker statements)
# --------------------------------------------------------------------------------------------

_TRADES = [
    "Form 1099-B Proceeds From Broker",
    "SHORT TERM CAPITAL GAINS AND LOSSES",
    "COVERED TRANSACTIONS",
    "ACME CORP CUSIP Number 99999X999",
    "100.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
    "100.0000 Sale 02/02/24 03/05/24 500.00 900.00 0.00 0.00 (400.00)",
    "Security Subtotal 1,500.00 1,700.00 0.00 0.00 (200.00)",
]


def _gains(lines: list[str]) -> dict[str, int]:
    form = parse_b_section(doc_from_lines(lines), 2024)
    return {i.box or "?": i.amount_minor for i in form.income}


def test_trades_alone_are_summed_and_the_security_subtotal_is_not_added() -> None:
    assert _gains(_TRADES) == {"ST": -20_000}


def test_category_subtotals_replace_the_trades() -> None:
    lines = [
        *_TRADES,
        "Covered Short Term Capital Gains and Losses Subtotal 1,500.00 1,700.00 0.00 0.00 (200.00)",
        "NONCOVERED TRANSACTIONS",
        "Noncovered Short Term Capital Gains and Losses Subtotal 300.00 100.00 0.00 0.00 200.00",
    ]
    assert _gains(lines) == {"ST": 0}


def test_term_net_line_wins_over_subtotals_and_trades() -> None:
    lines = [
        *_TRADES,
        "Covered Short Term Capital Gains and Losses Subtotal 1,500.00 1,700.00 0.00 0.00 (200.00)",
        "NET SHORT TERM CAPITAL GAINS AND LOSSES 1,500.00 1,700.00 0.00 0.00 (200.00)",
        "SALES PROCEEDS AND NET GAINS AND LOSSES* 1,500.00 1,700.00 0.00 0.00 (200.00)",
    ]
    assert _gains(lines) == {"ST": -20_000}


def test_footnote_markers_and_market_discount_column_are_tolerated() -> None:
    lines = [
        "Form 1099-B Proceeds From Broker",
        "LONG TERM CAPITAL GAINS AND LOSSES",
        "10.0000 Sale 01/02/20 03/04/24 1,000.00 1,200.00 (A) 0.00 150.00 (W) (50.00)",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("LT", -5_000)]
    assert form.warnings == ()


def test_trade_without_a_computed_gain_is_skipped_with_a_warning() -> None:
    lines = [
        "Form 1099-B Proceeds From Broker",
        "SHORT TERM CAPITAL GAINS AND LOSSES",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
        "OTHER TRANSACTIONS",
        "5.0000 Sale 01/02/20 03/04/24 900.00 0.00 0.00 0.00 N/C",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 20_000)]
    assert "Some 1099-B transactions have no computed gain or loss and are not included." in (
        form.warnings
    )


def test_dividend_and_interest_rows_in_the_same_block_are_not_trades() -> None:
    lines = [
        *_TRADES,
        "ACME CORP 100 03/04/24 Dividend 12.00 12.00 0.00 0.00",
        "99/99/99 Bank Interest 0.00",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", -20_000)]
    assert form.warnings == ()


def test_running_heading_on_each_page_does_not_split_the_table() -> None:
    pages = [
        ["Form 1099-B Proceeds From Broker", "SHORT TERM CAPITAL GAINS AND LOSSES", *_TRADES[3:6]],
        [
            "Form 1099-B Proceeds From Broker",
            "100.0000 Sale 03/02/24 03/05/24 700.00 600.00 0.00 0.00 100.00",
            "NET SHORT TERM CAPITAL GAINS AND LOSSES 2,200.00 2,300.00 0.00 0.00 (100.00)",
            "SALES PROCEEDS AND NET GAINS AND LOSSES* 2,200.00 2,300.00 0.00 0.00 (100.00)",
        ],
    ]
    parsed = PARSER.parse(doc_from_pages(pages), tax_year=2024)
    (form,) = parsed.forms
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", -10_000)]


def test_trade_with_a_trailing_code_letter_is_kept() -> None:
    lines = [
        "Form 1099-B Proceeds From Broker",
        "SHORT TERM CAPITAL GAINS AND LOSSES",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 200.00 W",
        "10.0000 Sale 01/02/24 03/05/24 500.00 400.00 0.00 100.00*",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 30_000)]
    assert form.warnings == ()


def test_trade_with_an_unreadable_tail_is_counted_in_the_warning() -> None:
    lines = [
        "Form 1099-B Proceeds From Broker",
        "SHORT TERM CAPITAL GAINS AND LOSSES",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 200.00 see note",
        "10.0000 Sale 01/02/24 03/05/24 500.00 400.00 0.00 100.00",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert "Some 1099-B transaction rows could not be read." in form.warnings


_TRADE_HEAD = ["Form 1099-B Proceeds From Broker", "SHORT TERM CAPITAL GAINS AND LOSSES"]


@pytest.mark.parametrize(
    "row",
    [
        # code between the amounts, bare and parenthesized
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 (W) 0.00 200.00",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 W 0.00 200.00",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 (D) 0.00 0.00 200.00",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00 (X)",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00 X",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 e 0.00 0.00 200.00",
        # markers and footnotes
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00 *",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00 \u2020",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00 \u00b9",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00\u00b2 800.00 0.00 0.00 200.00",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00[1] 800.00 0.00 0.00 200.00 [2]",
    ],
)
def test_codes_markers_and_footnotes_anywhere_in_the_tail_are_ignored(row: str) -> None:
    form = parse_b_section(doc_from_lines([*_TRADE_HEAD, row]), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 20_000)]
    assert form.warnings == ()


@pytest.mark.parametrize(
    "gain",
    ["(50.00)", "(50.00) (W)", "( 50.00 )", "(50.00 )", "-50.00"],
)
def test_parenthesized_negative_gain_is_read_however_it_is_split(gain: str) -> None:
    row = f"10.0000 Sale 01/02/24 03/04/24 1,000.00 1,050.00 0.00 0.00 {gain}"
    form = parse_b_section(doc_from_lines([*_TRADE_HEAD, row]), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", -5_000)]
    assert form.warnings == ()


def test_split_parenthesized_amount_in_the_middle_of_the_row() -> None:
    row = "10.0000 Sale 01/02/24 03/04/24 1,000.00 (50.00 ) (W) 0.00 1,050.00"
    form = parse_b_section(doc_from_lines([*_TRADE_HEAD, row]), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 105_000)]
    assert form.warnings == ()


@pytest.mark.parametrize("marker", ["N/C", "NC", "(N/C)"])
def test_not_computed_rows_warn_separately_from_unreadable(marker: str) -> None:
    lines = [
        *_TRADE_HEAD,
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
        f"5.0000 Sale 01/02/20 03/04/24 900.00 0.00 0.00 0.00 {marker}",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 20_000)]
    assert form.warnings == (
        "Some 1099-B transactions have no computed gain or loss and are not included.",
    )


@pytest.mark.parametrize(
    "row",
    [
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 200.00 see note",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 200.00 (12.3x)",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00",
    ],
)
def test_garbled_rows_are_still_unreadable(row: str) -> None:
    lines = [*_TRADE_HEAD, row, "10.0000 Sale 01/02/24 03/05/24 500.00 400.00 0.00 0.00 100.00"]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert "Some 1099-B transaction rows could not be read." in form.warnings
    assert "no computed gain" not in " ".join(form.warnings)


def test_supplementary_amortization_table_after_the_totals_is_not_read_as_trades() -> None:
    lines = [
        *_TRADE_HEAD,
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
        "NET SHORT TERM CAPITAL GAINS AND LOSSES 1,000.00 800.00 0.00 0.00 200.00",
        "REALIZED COST BASIS INFORMATION FOR SECURITIES SUBJECT TO AMORTIZATION/ACCRETION",
        "Security Description CUSIP Quantity Acquired Cost Basis Year-To-Date Life-To-Date Sales",
        "WIDGET BOND 123456AB9 500.0000 01/02/23 4,000.00 (12.34) (12.34 ) 4,100.00",
        "OTHER BOND 654321CD0 100.0000 02/03/22 900.00 (1.50 ) (3.00) 950.00",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 20_000)]
    assert form.warnings == ()


def test_supplementary_table_heading_stops_trades_without_a_total_line() -> None:
    lines = [
        *_TRADE_HEAD,
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
        "Cost Basis Information Year-To-Date Life-To-Date",
        "WIDGET BOND 500.0000 01/02/23 4,000.00 (12.34) (12.34 ) 4,100.00",
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 20_000)]
    assert form.warnings == ()


def test_single_date_row_inside_the_trade_table_is_not_a_trade() -> None:
    lines = [
        *_TRADE_HEAD,
        "10.0000 Sale 01/02/24 03/04/24 1,000.00 800.00 0.00 0.00 200.00",
        "WIDGET BOND 500.0000 01/02/23 4,000.00 (12.34) (12.34 ) 4,100.00",
        "Inherited 03/04/24 500.00 400.00 0.00 0.00 100.00",
    ]
    form = parse_b_section(doc_from_lines(lines), 2024)
    assert [(i.box, i.amount_minor) for i in form.income] == [("ST", 30_000)]
    assert form.warnings == ()


def test_net_line_printed_twice_is_not_doubled() -> None:
    net = "NET SHORT TERM CAPITAL GAINS AND LOSSES 1,500.00 1,700.00 0.00 0.00 (200.00)"
    lines = ["Form 1099-B Proceeds From Broker", net, *_TRADES[1:], net]
    assert _gains(lines) == {"ST": -20_000}


def test_subtotal_printed_twice_is_not_doubled_but_categories_still_add() -> None:
    cov = (
        "Covered Short Term Capital Gains and Losses Subtotal 1,500.00 1,700.00 0.00 0.00 (200.00)"
    )
    non = "Noncovered Short Term Capital Gains and Losses Subtotal 300.00 100.00 0.00 0.00 200.00"
    assert _gains(["Form 1099-B Proceeds", cov, non, cov, non]) == {"ST": 0}
    assert _gains(["Form 1099-B Proceeds", cov, cov]) == {"ST": -20_000}
