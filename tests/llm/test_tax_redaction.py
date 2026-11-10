"""Golden redaction tests for the tax LLM fallback: structure-based identity removal, address
shapes anywhere, the unlabeled mailing block, preparer / spouse rows, the page-1 context header.
Synthetic data only."""

from __future__ import annotations

import pytest

from personal_finance_analyzer.core.tax_types import UnparsedRegion
from personal_finance_analyzer.llm import tax_parser as tp
from tests.ingest.tax_support import doc_from_lines, doc_from_pages

_FORM = [
    "Form W-2 Wage and Tax Statement 2024",
    "a Employee's social security number",
    "912-34-5678",
    "e Employee's first name and initial     Last name",
    "JORDAN Q                                TESTPERSON",
    "100 State Street Apt 4",
    "SPRINGFIELD IL 62701",
    "1 Wages, tips, other compensation   50,000.00",
    "2 Federal income tax withheld   5,000.00",
]


def _redact(lines: list[str]) -> str:
    return tp.redact_page(doc_from_lines(lines).pages[0])


def test_value_rows_with_stop_words_below_a_label_are_still_dropped() -> None:
    out = _redact(_FORM)
    for leaked in ("State Street", "Apt 4", "SPRINGFIELD", "62701", "JORDAN", "TESTPERSON"):
        assert leaked not in out, leaked
    assert "50,000.00" in out and "5,000.00" in out


@pytest.mark.parametrize(
    "line",
    [
        "100 Federal St",
        "42 State Street Apt 4",
        "7 N. Account Avenue Suite 200",
        "55 Code Road #12",
        "PO Box 1234",
        "P.O. Box 77",
        "Testville ZZ 00000-1234",
        "SPRINGFIELD IL 62701",
        "12345-6789",
    ],
)
def test_address_shaped_lines_are_dropped_wherever_they_appear(line: str) -> None:
    out = _redact(["Form 1099-INT Interest Income Tax Year 2024", line, "1 Interest income  5.00"])
    assert out.splitlines() == [
        "Form 1099-INT Interest Income Tax Year 2024",
        "1 Interest income 5.00",
    ]


def test_address_next_to_an_amount_only_loses_the_address_words() -> None:
    out = _redact(
        ["Form 1099-INT", "9 Example Lane Testville ZZ 00000       1 Interest income  5.00"]
    )
    assert "Example" not in out and "Testville" not in out and "5.00" in out


def test_unlabeled_mailing_block_at_the_top_of_page_one_is_dropped() -> None:
    lines = [
        "JORDAN TESTPERSON",
        "123 EXAMPLE LANE",
        "SPRINGFIELD IL 62701",
        "",
        "Consolidated Form 1099 Tax Statement 2024",
        "Granite Brokerage LLC",
        "1a Total ordinary dividends   12.00",
    ]
    out = _redact(lines)
    assert "JORDAN" not in out and "TESTPERSON" not in out and "EXAMPLE" not in out
    assert "Granite Brokerage LLC" in out and "12.00" in out


def test_leading_block_keeps_payer_labelled_and_company_lines() -> None:
    lines = [
        "Granite Fund Trust",
        "PAYER'S name: Granite Fund Trust",
        "JORDAN TESTPERSON",
        "Form 1099-INT 2024",
        "1 Interest income  5.00",
    ]
    out = _redact(lines)
    assert out.count("Granite Fund Trust") == 2 and "JORDAN" not in out


def test_only_page_one_loses_its_leading_block() -> None:
    doc = doc_from_pages(
        [["Form 1099-INT", "1 Interest income  1.00"], ["Continued notes", "2.00"]]
    )
    assert "Continued notes" in tp.redact_page(doc.pages[1])


@pytest.mark.parametrize(
    "lines",
    [
        ["Paid Preparer's name  Pat Preparer", "Firm's name  Acme Tax Prep LLC", "PTIN P01234567"],
        ["Preparer's name", "Pat Preparer"],
        ["Spouse's name  Sam Testperson"],
        ["Your spouse's first name", "Sam"],
        ["Dependents", "Casey Testperson"],
    ],
)
def test_preparer_spouse_and_dependent_rows_are_removed(lines: list[str]) -> None:
    out = _redact(["Form 1099-INT 2024", *lines, "1 Interest income  5.00"])
    for leaked in ("Pat", "Preparer LLC", "Acme", "Sam", "Casey", "Testperson", "P01234567"):
        assert leaked not in out.replace("Preparer's", ""), leaked
    assert "5.00" in out


def test_walk_stops_at_a_blank_row_and_at_the_next_label() -> None:
    lines = [
        "Form 1099-INT 2024",
        "RECIPIENT'S name",
        "Jordan Testperson",
        "",
        "Granite Notice Line",
        "1 Interest income  5.00",
    ]
    out = _redact(lines)
    assert "Jordan" not in out and "Granite Notice Line" in out


def test_text_without_word_boxes_uses_the_same_rules() -> None:
    text = (
        "RECIPIENT'S name\nJordan Testperson\n100 State Street Apt 4\nTestville ZZ 00000\n"
        "\nPO Box 12\nInterest 5.00"
    )
    out = tp.redact_tax_text(text)
    for leaked in ("Jordan", "State Street", "Testville", "PO Box"):
        assert leaked not in out
    assert "5.00" in out


# --- the page-1 context header ---------------------------------------------------------------


def _three_pages(first: list[str]) -> tuple[list[tp.Request], bool]:
    doc = doc_from_pages([first, ["Form 1099-INT", "1 Interest  3.00"], ["x", "Interest  4.00"]])
    return tp.build_requests(doc, (UnparsedRegion("1099-INT", (1, 2, 3), "unreadable"),))


def test_header_context_needs_a_payer_label() -> None:
    requests, _ = _three_pages(
        ["JORDAN TESTPERSON", "123 EXAMPLE LANE", "SPRINGFIELD IL 62701", "Year-End Summary"]
    )
    assert all("context only" not in r.text for r in requests)
    assert all("TESTPERSON" not in r.text and "EXAMPLE" not in r.text for r in requests)


def test_header_context_is_redacted_like_the_pages() -> None:
    requests, _ = _three_pages(
        [
            "JORDAN TESTPERSON",
            "123 EXAMPLE LANE",
            "SPRINGFIELD IL 62701",
            "Year-End Summary",
            "PAYER'S name: Granite Fund Trust",
            "RECIPIENT'S name: Jordan Testperson",
        ]
    )
    request = requests[1]  # page 3's chunk carries the page-1 header as context
    assert "context only" in request.text and "Granite Fund Trust" in request.text
    for leaked in ("TESTPERSON", "Testperson", "EXAMPLE", "SPRINGFIELD", "62701"):
        assert leaked not in request.text, leaked


def test_header_context_is_never_added_when_page_one_is_not_sent() -> None:
    doc = doc_from_pages(
        [
            ["PAYER'S name: Granite Fund Trust", "Account summary"],
            ["Form 1099-INT", "1 Interest  3.00"],
            ["x", "Interest  4.00"],
        ]
    )
    requests, _ = tp.build_requests(doc, (UnparsedRegion("1099-INT", (2, 3), "unreadable"),))
    wire = "\n".join(r.text for r in requests)
    assert "context only" not in wire and "Granite" not in wire
    assert sorted(p for r in requests for p in r.pages) == [2, 3]
