"""Unit tests for `ingest.tax.normalize`: glyph junk, dot leaders, letter-spacing, signs, running
headers and instruction text."""

from __future__ import annotations

import pytest

from personal_finance_analyzer.ingest.layout import cluster_rows
from personal_finance_analyzer.ingest.tax.normalize import normalize_doc
from tests.ingest.tax_support import doc_from_lines, doc_from_pages


def _rows(doc) -> list[str]:  # type: ignore[no-untyped-def]
    return [row.text for page in doc.pages for row in cluster_rows(page.words)]


def test_clean_document_is_returned_unchanged() -> None:
    doc = doc_from_lines(["Form 1099-INT Interest Income", "1 Interest income   25.00"])
    assert normalize_doc(doc) is doc


def test_cid_placeholders_are_removed() -> None:
    doc = doc_from_lines(["(cid:9)(cid:9)(cid:9) Total (cid:12) 1,234.56 (cid:99)"])
    assert _rows(normalize_doc(doc)) == ["Total 1,234.56"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "1a Total Ordinary Dividends ........................ 1,234.56",
            "1a Total Ordinary Dividends 1,234.56",
        ),
        ("Interest Income .................... 99.00", "Interest Income 99.00"),
        ("Tax withheld ____________ 5.00", "Tax withheld 5.00"),
        ("Total Dividends.........1,234.56", "Total Dividends 1,234.56"),
    ],
)
def test_dot_leaders_are_stripped(line: str, expected: str) -> None:
    assert _rows(normalize_doc(doc_from_lines([line]))) == [expected]


def test_letter_spaced_heading_is_rejoined() -> None:
    doc = doc_from_lines(["Form 1 0 9 9 - D I V 2024", "D i v i d e n d s"])
    assert _rows(normalize_doc(doc)) == ["Form 1099-DIV 2024", "Dividends"]


def test_short_normal_words_are_not_merged() -> None:
    doc = doc_from_lines(["a b c d e", "No. 1 of 2", "W - 2 Wage"])
    assert _rows(normalize_doc(doc)) == ["a b c d e", "No. 1 of 2", "W - 2 Wage"]


def test_detached_parentheses_and_minus_keep_the_sign() -> None:
    doc = doc_from_lines(["Net loss ( 1,234.56 )", "Other  - 50.00"])
    assert _rows(normalize_doc(doc)) == ["Net loss (1,234.56)", "Other -50.00"]


def test_form_suffix_split_from_the_number_is_glued() -> None:
    doc = doc_from_lines(["Form 1099 -INT 2024"])
    assert _rows(normalize_doc(doc)) == ["Form 1099-INT 2024"]


def _page(n: int, total: int, body: str, account: str = "ACCT-0001") -> list[str]:
    return [
        "Account No.     Taxpayer No.      Page",
        f"{account}   XXX-XX-9999   {n} of {total}",
        "",
        body,
    ]


def test_repeated_page_header_is_kept_once() -> None:
    doc = doc_from_pages([_page(1, 3, "alpha"), _page(2, 3, "beta"), _page(3, 3, "gamma")])
    rows = _rows(normalize_doc(doc))
    assert rows.count("Account No. Taxpayer No. Page") == 1
    assert [r for r in rows if r in ("alpha", "beta", "gamma")] == ["alpha", "beta", "gamma"]


def test_changed_header_block_is_kept_so_a_new_account_is_visible() -> None:
    doc = doc_from_pages(
        [_page(1, 2, "alpha"), _page(2, 2, "beta", account="ACCT-0002")],
    )
    rows = _rows(normalize_doc(doc))
    assert rows.count("Account No. Taxpayer No. Page") == 2


def test_instruction_text_is_dropped_but_figures_above_it_stay() -> None:
    doc = doc_from_pages(
        [
            [
                "Form 1099-INT Interest Income",
                "1 Interest income   25.00",
                "INSTRUCTIONS FOR RECIPIENT OF FORMS 1099",
                "The amounts on Forms 1099-DIV, 1099-INT and 1099-B are as follows.",
                "1099-DIV - DIVIDENDS AND DISTRIBUTIONS (OMB No. 1545-0110)",
            ],
            ["More legal text about Form 1099-B and Form W-2 with no amounts at all."],
        ]
    )
    text = "\n".join(_rows(normalize_doc(doc)))
    assert "25.00" in text
    assert "1099-DIV" not in text and "Form W-2" not in text


def test_page_after_instructions_with_amounts_is_kept() -> None:
    doc = doc_from_pages(
        [
            ["INSTRUCTIONS FOR RECIPIENT", "text"],
            ["Form 1099-INT", "1 Interest income   25.00"],
        ]
    )
    assert "25.00" in "\n".join(_rows(normalize_doc(doc)))


def test_number_intact_but_suffix_letter_spaced() -> None:
    doc = doc_from_lines(["Form 1099 - I N T 2024", "Form 1099 - D I V 2024"])
    assert _rows(normalize_doc(doc)) == ["Form 1099-INT 2024", "Form 1099-DIV 2024"]


def test_inline_mention_of_instructions_does_not_cut_figures_below() -> None:
    doc = doc_from_lines(
        [
            "For details see the Instructions for Recipient on the back of this page.",
            "1 Interest income   25.00",
        ]
    )
    assert "25.00" in "\n".join(_rows(normalize_doc(doc)))


def test_form_title_row_with_notice_phrase_does_not_cut() -> None:
    doc = doc_from_lines(
        [
            "Form 1099-INT Interest Income Notice to Recipients",
            "1 Interest income   25.00",
        ]
    )
    assert normalize_doc(doc) is doc


def test_amount_rows_below_a_heading_on_the_same_page_survive() -> None:
    doc = doc_from_lines(
        [
            "INSTRUCTIONS FOR RECIPIENT",
            "Report the amounts shown on your return.",
            "1 Interest income   25.00",
        ]
    )
    text = "\n".join(_rows(normalize_doc(doc)))
    assert "25.00" in text and "Report the amounts" not in text
