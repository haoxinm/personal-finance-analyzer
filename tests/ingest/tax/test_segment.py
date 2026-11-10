"""Unit tests for `ingest.tax.segment`: anchors, column cuts (2-up, 2x2, headingless tables),
family cuts, continuation and the untouched single-form path."""

from __future__ import annotations

from personal_finance_analyzer.core.types import ExtractedDoc
from personal_finance_analyzer.ingest.layout import cluster_rows
from personal_finance_analyzer.ingest.tax.segment import (
    build_segments,
    find_anchors,
    layout_regions,
)
from tests.ingest.tax_support import doc_from_lines, doc_from_pages

_RIGHT_COL = 62  # character column of the right-hand form on a 6 pt grid (372 pt)


def _two(left: str, right: str) -> str:
    return left.ljust(_RIGHT_COL) + right


def _texts(doc: ExtractedDoc) -> list[list[str]]:
    return [[r.text for r in cluster_rows(p.words)] for p in doc.pages]


def _flat(doc: ExtractedDoc) -> str:
    return "\n".join(row for page in _texts(doc) for row in page)


def test_anchors_are_headings_not_prose() -> None:
    doc = doc_from_lines(
        [
            "Form 1099-DIV 2024 Dividends and Distributions",
            "The amounts shown on Forms 1099-INT and 1099-DIV are reported as described in the",
            "Form 1099-INT (continued)",
            "Form W-2 is mentioned here in passing",
        ]
    )
    rows = cluster_rows(doc.pages[0].words)
    assert [a.family for a in find_anchors(rows, 1)] == ["1099-DIV"]


def test_two_headings_on_one_row_are_both_found() -> None:
    doc = doc_from_lines([_two("Form 1099-DIV Dividends", "Form 1099-INT Interest")])
    rows = cluster_rows(doc.pages[0].words)
    anchors = find_anchors(rows, 1)
    assert [a.family for a in anchors] == ["1099-DIV", "1099-INT"]
    assert anchors[0].x0 < anchors[1].x0


def test_w2_anchor_is_the_ssn_label_and_falls_back_to_the_wages_label() -> None:
    with_ssn = doc_from_lines(
        ["a Employee's social security number", "1 Wages, tips, other compensation"]
    )
    assert len(find_anchors(cluster_rows(with_ssn.pages[0].words), 1)) == 1
    no_ssn = doc_from_lines(["1 Wages, tips, other compensation   2 Federal income tax withheld"])
    assert [a.family for a in find_anchors(cluster_rows(no_ssn.pages[0].words), 1)] == ["w2"]


def test_single_form_keeps_the_original_pages() -> None:
    doc = doc_from_pages(
        [
            ["Form 1099-INT Interest Income", "1 Interest income   25.00"],
            ["Form 1099-INT (continued)", "State tax withheld   2.00"],
        ]
    )
    (segment,) = build_segments(doc)
    assert segment.doc.pages == doc.pages
    assert segment.page_map == {1: 1, 2: 2}


def test_two_up_page_is_cut_into_columns_without_mixing_rows() -> None:
    lines = [
        "Payment statement",
        _two("Form 1099-INT Interest Income", "Form 1099-INT Interest Income"),
        _two("Payer's name Alpha Bank", "Payer's name Beta Credit"),
        _two("1 Interest income   100.00", "1 Interest income   200.00"),
        _two("4 Federal tax withheld   1.00", "4 Federal tax withheld   2.00"),
    ]
    (segment,) = build_segments(doc_from_lines(lines))
    pages = _texts(segment.doc)
    assert len(pages) == 3  # header, left form, right form
    assert any("Alpha Bank" in row for row in pages[1]) and not any("Beta" in r for r in pages[1])
    assert any("Beta Credit" in row for row in pages[2]) and not any("Alpha" in r for r in pages[2])
    assert segment.page_map == {1: 1, 2: 1, 3: 1}


def test_left_form_with_wide_amounts_is_not_cut_in_half() -> None:
    lines = [
        _two("Form 1099-DIV Dividends and Distributions", "Form 1099-INT Interest Income"),
        _two("1a Total Ordinary Dividends        12,345.67", "1 Interest income   200.00"),
        _two("1b Qualified Dividends              9,000.00", "3 Savings bonds   5.00"),
    ]
    div, interest = build_segments(doc_from_lines(lines))
    assert (div.family, interest.family) == ("1099-DIV", "1099-INT")
    left = _flat(div.doc)
    assert "12,345.67" in left and "9,000.00" in left
    assert "200.00" not in left
    assert "200.00" in _flat(interest.doc)


def test_four_up_grid_reads_top_left_top_right_bottom_left_bottom_right() -> None:
    top = [
        _two("Form 1099-INT Interest A", "Form 1099-INT Interest B"),
        _two("1 Interest income   1.00", "1 Interest income   2.00"),
    ]
    bottom = [
        _two("Form 1099-INT Interest C", "Form 1099-INT Interest D"),
        _two("1 Interest income   3.00", "1 Interest income   4.00"),
    ]
    (segment,) = build_segments(doc_from_lines([*top, "", "", *bottom]))
    amounts = [row.split()[-1] for page in _texts(segment.doc) for row in page if "income" in row]
    assert amounts == ["1.00", "2.00", "3.00", "4.00"]


def test_stacked_forms_of_different_types_are_separate_segments() -> None:
    lines = [
        "a Employee's social security number",
        "1 Wages, tips, other compensation",
        "  85,000.00",
        "",
        "",
        "Form 1099-INT Interest Income",
        "1 Interest income   25.00",
    ]
    segments = build_segments(doc_from_lines(lines))
    assert [s.family for s in segments] == ["w2", "1099-INT"]
    assert "25.00" not in _flat(segments[0].doc)
    assert "85,000.00" not in _flat(segments[1].doc)


def test_pages_of_different_forms_in_one_file_are_separate_segments() -> None:
    doc = doc_from_pages(
        [
            ["a Employee's social security number", "1 Wages, tips, other compensation"],
            ["Form 1099-INT Interest Income", "1 Interest income   25.00"],
            ["Form 1099-DIV Dividends", "1a Total ordinary dividends   9.00"],
        ]
    )
    segments = build_segments(doc)
    assert [s.family for s in segments] == ["w2", "1099-INT", "1099-DIV"]
    assert [s.original_pages for s in segments] == [(1,), (2,), (3,)]


def test_page_without_heading_continues_the_previous_form() -> None:
    doc = doc_from_pages(
        [
            ["Form 1099-INT Interest Income", "1 Interest income   25.00"],
            ["Form 1099-INT (continued)", "17 State tax withheld   2.00"],
            ["Notes", "no heading here"],
            ["Form 1099-DIV Dividends", "1a Total ordinary dividends   9.00"],
        ]
    )
    segments = build_segments(doc)
    assert [s.family for s in segments] == ["1099-INT", "1099-DIV"]
    assert segments[0].original_pages == (1, 2, 3)


def test_headingless_side_by_side_tables_are_split_at_the_amount_label_gap() -> None:
    rows = [
        f"Label {i} ............ {i}.00".ljust(_RIGHT_COL) + f"Other {i} ........ {i}.50"
        for i in range(1, 8)
    ]
    regions = layout_regions(doc_from_lines(rows))
    assert len(regions) == 2
    assert all("Other" not in r.text for r in regions[0].rows)
    assert all("Label" not in r.text for r in regions[1].rows)


def test_single_column_table_is_not_split() -> None:
    rows = [f"Label {i} ............ {i}.00" for i in range(1, 12)]
    assert len(layout_regions(doc_from_lines(rows))) == 1


def test_document_without_headings_is_one_anonymous_segment() -> None:
    (segment,) = build_segments(doc_from_pages([["nothing to see"], []]))
    assert segment.family is None and segment.anchors == ()
