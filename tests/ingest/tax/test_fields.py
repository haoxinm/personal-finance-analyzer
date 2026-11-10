from __future__ import annotations

from pathlib import Path

import pytest

from personal_finance_analyzer.ingest.tax import fields
from personal_finance_analyzer.ingest.tax.fields import (
    amount_for_label,
    checkbox_checked,
    compile_label,
    corrected_marker,
    doc_has,
    find_label,
    first_block,
    money_for_label,
    signed,
    signed_amount_for_label,
    split_blocks,
    text_for_label,
    whole_dollar_or_cents,
)
from tests.fixtures.gen import tax_common
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.ingest.tax_support import doc_from_lines, doc_from_pages, extract_doc

L = compile_label

SAME_ROW = [
    "Form 1040 U.S. Individual Income Tax Return 2024",
    "1a Wages, salaries, tips, etc. Attach Form(s) W-2 . . . . . . 1a        85,000",
    "2b Taxable interest . . . . . . . . . . . . . . . . . . 2b             1,234",
    "7  Capital gain or (loss). Attach Schedule D . . . . . . 7            (3,000)",
    "9  Add lines 1z, 2b, 3b. This is your total income . . . 9            90,000.50",
    "Short-term totals                              1,000.00     2,500.00    (1,500.00)",
    "Interest 5.00",
    "Interest 7.00",
]


def test_same_row_basic_and_whole_dollars() -> None:
    doc = doc_from_lines(SAME_ROW)
    assert amount_for_label(doc, L(r"Wages, salaries, tips"), mode="same_row") == 8_500_000
    assert amount_for_label(doc, L(r"Taxable interest"), mode="same_row") == 123_400
    assert amount_for_label(doc, L(r"total income"), mode="same_row") == 9_000_050


def test_same_row_skips_line_number_before_value() -> None:
    doc = doc_from_lines(["Total income 9   90,000", "Tax 11  5"])
    assert amount_for_label(doc, L(r"Total income")) == 9_000_000
    assert amount_for_label(doc, L(r"Total income"), skip_line_numbers=False) == 900
    assert amount_for_label(doc, L(r"^Tax"), skip_line_numbers=False) == 1100


def test_same_row_box_number_of_next_cell_is_not_a_value() -> None:
    doc = doc_from_lines(["1 Wages, tips        2 Federal income tax withheld"])
    assert amount_for_label(doc, L(r"Wages, tips"), mode="same_row") is None


def test_parenthesized_negative_and_signing() -> None:
    doc = doc_from_lines(SAME_ROW)
    label = L(r"Capital gain or \(loss\)")
    assert amount_for_label(doc, label, mode="same_row") == 300_000
    assert signed_amount_for_label(doc, label, mode="same_row") == -300_000
    money = money_for_label(doc, label, mode="same_row")
    assert money is not None and money.printed_negative
    assert signed(money) == -300_000
    assert signed(None) is None


def test_nth_column_and_occurrence() -> None:
    doc = doc_from_lines(SAME_ROW)
    label = L(r"Short-term totals")
    assert amount_for_label(doc, label, nth=0) == 100_000
    assert amount_for_label(doc, label, nth=1) == 250_000
    assert signed_amount_for_label(doc, label, nth=2) == -150_000
    assert amount_for_label(doc, label, nth=3) is None
    assert amount_for_label(doc, L(r"^Interest"), occurrence=1) == 700
    assert amount_for_label(doc, L(r"^Interest"), occurrence=2) is None
    assert amount_for_label(doc, L(r"^Interest"), occurrence=-1) is None


def test_missing_label_and_missing_value() -> None:
    doc = doc_from_lines(["Nothing here", "Label only"])
    assert amount_for_label(doc, L(r"absent")) is None
    assert amount_for_label(doc, L(r"Label only")) is None
    assert text_for_label(doc, L(r"absent")) is None


BELOW = [
    "1 Wages, tips, other comp.   2 Federal income tax withheld",
    "  52,000.00                    6,500.00",
    "",
    "3 Social security wages      4 Social security tax withheld",
    "  40,000.00",
    "",
    "5 Medicare wages and tips    6 Medicare tax withheld",
    "",
    "7 Social security tips       8 Allocated tips",
    "  11.00",
]


def test_below_mode_picks_value_in_label_x_range() -> None:
    doc = doc_from_lines(BELOW)
    assert amount_for_label(doc, L(r"Wages, tips, other"), mode="below") == 5_200_000
    assert amount_for_label(doc, L(r"Federal income tax withheld"), mode="below") == 650_000
    assert amount_for_label(doc, L(r"Social security wages"), mode="below") == 4_000_000


def test_below_mode_rejects_wrong_column() -> None:
    doc = doc_from_lines(BELOW)
    # box 4's cell is blank: the 40,000.00 under box 3 is in a different column
    assert amount_for_label(doc, L(r"Social security tax withheld"), mode="below") is None


def test_below_mode_blank_box_does_not_steal_next_cells_value() -> None:
    doc = doc_from_lines(BELOW)
    assert amount_for_label(doc, L(r"Medicare wages"), mode="below") is None
    assert amount_for_label(doc, L(r"Medicare tax withheld"), mode="below") is None
    assert amount_for_label(doc, L(r"Social security tips"), mode="below") == 1_100


def test_below_mode_skips_wrapped_label_continuation() -> None:
    doc = doc_from_lines(
        ["Wages, tips, other", "compensation", "  52,000.00", "", "3 Social security wages"]
    )
    assert amount_for_label(doc, L(r"Wages, tips, other"), mode="below") == 5_200_000
    assert amount_for_label(doc, L(r"Wages, tips, other"), mode="below", max_rows_below=1) is None


def test_auto_prefers_same_row_then_below() -> None:
    doc = doc_from_lines(["Total  123.00", "Other label", "  9.00"])
    assert amount_for_label(doc, L(r"Total")) == 12_300
    assert amount_for_label(doc, L(r"Other label")) == 900
    assert amount_for_label(doc, L(r"Other label"), mode="same_row") is None
    assert amount_for_label(doc, L(r"Total"), mode="below", max_rows_below=1) is None


def test_pages_filter_and_find_label_positions() -> None:
    doc = doc_from_pages([["Total 1.00"], ["Total 2.00"]])
    hits = find_label(doc, L(r"Total"))
    assert [(h.page_number, h.row_index) for h in hits] == [(1, 0), (2, 0)]
    assert hits[0].label_x0 == 0.0 and hits[0].label_x1 == 30.0
    assert amount_for_label(doc, L(r"Total"), pages=[2]) == 200
    assert find_label(doc, L(r"Total"), pages=[3]) == []
    assert amount_for_label(doc, L(r"Total"), occurrence=1) == 200


def test_whole_dollar_or_cents() -> None:
    assert whole_dollar_or_cents("85,000") == 8_500_000
    assert whole_dollar_or_cents("85000") == 8_500_000
    assert whole_dollar_or_cents("$1,234.56") == 123_456
    assert whole_dollar_or_cents("(5)") == 500
    assert whole_dollar_or_cents("abc") is None
    assert whole_dollar_or_cents("") is None


def test_text_for_label_same_row_below_and_scrub() -> None:
    doc = doc_from_lines(
        [
            "Payer's name: Harbor Mill Savings        Phone",
            "",
            "Employer's name, address",
            "Pinecrest Logistics LLC",
            "200 Sample Road",
            "",
            "Recipient 912-34-5678 99-0000001",
        ]
    )
    assert text_for_label(doc, L(r"Payer's name:"), mode="same_row") == "Harbor Mill Savings"
    assert text_for_label(doc, L(r"Employer's name"), mode="below") == "Pinecrest Logistics LLC"
    assert (
        text_for_label(doc, L(r"Employer's name"), mode="below", lines=2)
        == "Pinecrest Logistics LLC\n200 Sample Road"
    )
    assert text_for_label(doc, L(r"^Recipient"), mode="same_row") == "***-**-5678 **-***0001"


def test_split_blocks_and_first_block() -> None:
    doc = doc_from_pages(
        [
            ["Tax Year 2024", "Form W-2 Copy B", "Wages 1.00", "Form W-2 Copy C", "Wages 1.00"],
            ["Form W-2 Copy 2", "Wages 2.00", "Footer END"],
        ]
    )
    start = L(r"Form W-2")
    blocks = split_blocks(doc, start)
    assert len(blocks) == 3
    assert [b.page_count for b in blocks] == [1, 1, 1]
    assert "Copy B" in blocks[0].full_text and "Copy C" not in blocks[0].full_text
    assert amount_for_label(blocks[2], L(r"Wages")) == 200
    assert blocks[2].pages[0].page_number == 2
    assert first_block(doc, start) == blocks[0]
    assert fields.first_page_block is first_block
    with_pre = split_blocks(doc, start, keep_preamble=True)
    assert len(with_pre) == 3 and "Tax Year 2024" in with_pre[0].full_text
    ended = split_blocks(doc, start, end_re=L(r"END"))
    assert "Footer" not in ended[2].full_text
    assert split_blocks(doc, L(r"nope")) == ()
    assert first_block(doc, L(r"nope")) is doc


def test_block_spanning_pages() -> None:
    doc = doc_from_pages([["Form 1099-INT A", "x 1.00"], ["continued 2.00"], ["Form 1099-INT B"]])
    blocks = split_blocks(doc, L(r"Form 1099-INT"))
    assert [b.page_count for b in blocks] == [2, 1]
    assert amount_for_label(blocks[0], L(r"continued")) == 200


def test_doc_has() -> None:
    doc = doc_from_pages([["Alpha beta"], ["Gamma 12345"]])
    assert doc_has(doc, r"gamma \d+")
    assert not doc_has(doc, r"gamma", pages=[1])
    assert doc_has(doc, L(r"alpha"), pages=[1])
    assert not doc_has(doc, "delta")


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("[X] CORRECTED", True),
        ("CORRECTED [x]", True),
        ("(X) CORRECTED (if checked)", True),
        ("[ ] CORRECTED", False),
        ("CORRECTED (if checked)", False),
        ("*** CORRECTED ***", True),
        ("CORRECTED", True),
        ("Form 1099-INT", False),
        ("VOID [ ] CORRECTED [ ]", False),
    ],
)
def test_corrected_marker(line: str, expected: bool) -> None:
    assert corrected_marker(doc_from_lines([line])) is expected


def test_checkbox_checked() -> None:
    doc = doc_from_lines(["[X] Statutory employee   [ ] Retirement plan"])
    assert checkbox_checked(doc, L(r"Statutory employee"))
    assert not checkbox_checked(doc, L(r"Retirement plan"))


# --- real pdfplumber extraction -------------------------------------------------------------


def test_below_mode_with_real_pdf_boxes(tmp_path: Path) -> None:
    items = tax_common.grid(
        40,
        60,
        [
            [("1 Wages, tips, other comp.", "52,000.00"), ("2 Federal income tax", "6,500.00")],
            [("3 Social security wages", "40,000.00"), ("4 Social security tax", "")],
            [("5 Medicare wages and tips", ""), ("6 Medicare tax withheld", "580.00")],
        ],
        col_width=170,
    )
    pdf = tmp_path / "boxes.pdf"
    tax_common.render_boxes_pdf(pdf, [items])
    doc = extract_doc(pdf)
    assert amount_for_label(doc, L(r"Wages, tips, other"), mode="below") == 5_200_000
    assert amount_for_label(doc, L(r"Federal income tax"), mode="below") == 650_000
    assert amount_for_label(doc, L(r"Social security wages"), mode="below") == 4_000_000
    assert amount_for_label(doc, L(r"Social security tax"), mode="below") is None
    assert amount_for_label(doc, L(r"Medicare wages"), mode="below") is None
    assert amount_for_label(doc, L(r"Medicare tax withheld"), mode="below") == 58_000
    assert amount_for_label(doc, L(r"Wages, tips, other")) == 5_200_000  # auto


def test_render_boxes_pdf_is_deterministic(tmp_path: Path) -> None:
    items = tax_common.cell(40, 60, "1 Wages", "1.00")
    tax_common.render_boxes_pdf(tmp_path / "a.pdf", [items])
    tax_common.render_boxes_pdf(tmp_path / "b.pdf", [items])
    assert (tmp_path / "a.pdf").read_bytes() == (tmp_path / "b.pdf").read_bytes()


def test_same_row_with_real_render_lines_pdf(tmp_path: Path) -> None:
    widths = [44, 12]
    lines = [
        "Tax Year 2024",
        format_row(["Wages, salaries, tips", "85,000"], widths),
        format_row(["Short-term capital loss", "(3,000.00)"], widths),
        format_row(["Taxable interest", "1,234"], widths),
    ]
    pdf = tmp_path / "rows.pdf"
    render_lines_pdf(pdf, [lines])
    doc = extract_doc(pdf)
    assert amount_for_label(doc, L(r"Wages, salaries"), mode="same_row") == 8_500_000
    assert signed_amount_for_label(doc, L(r"capital loss"), mode="same_row") == -300_000
    assert amount_for_label(doc, L(r"Taxable interest"), mode="same_row") == 123_400
    assert amount_for_label(doc, L(r"Taxable interest"), mode="below") is None


def test_cell_and_grid_reject_overflow() -> None:
    with pytest.raises(ValueError):
        tax_common.cell(0, 0, "x" * 50, "1.00", width=100)
    with pytest.raises(ValueError):
        tax_common.grid(0, 0, [[("label", "1.00")]], col_width=50, row_height=12)
    with pytest.raises(ValueError):
        tax_common.grid(0, 0, [[("a very long label indeed", "1")]], col_width=40)
    assert tax_common.money("1,234.56") == 123_456
