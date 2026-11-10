"""Page selection: `ExtractedDoc.select_pages`, `parse_page_spec`, `validate_pages`."""

from __future__ import annotations

import pytest

from personal_finance_analyzer.core.errors import PfaError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText
from personal_finance_analyzer.ingest.pages import (
    dump_pages,
    load_pages,
    parse_page_spec,
    validate_pages,
)


def _doc(char_counts: list[int]) -> ExtractedDoc:
    pages = tuple(
        PageText(page_number=i, text="x" * n, words=(), char_count=n)
        for i, n in enumerate(char_counts, start=1)
    )
    return ExtractedDoc(file_sha256="s", page_count=len(pages), pages=pages, has_text_layer=True)


def test_select_pages_keeps_original_numbers_and_recounts() -> None:
    doc = _doc([100, 100, 100, 100])
    sel = doc.select_pages([4, 2])
    assert [p.page_number for p in sel.pages] == [2, 4]
    assert sel.page_count == 2
    assert sel.has_text_layer is True


def test_select_pages_ignores_out_of_range_and_empty() -> None:
    doc = _doc([100, 100])
    assert [p.page_number for p in doc.select_pages([2, 9]).pages] == [2]
    assert doc.select_pages(None) is doc
    assert doc.select_pages([]) is doc
    assert doc.select_pages([7, 8]) is doc


def test_select_pages_recomputes_text_layer() -> None:
    doc = _doc([500, 0, 0])
    assert doc.select_pages([2, 3]).has_text_layer is False
    assert doc.select_pages([1]).has_text_layer is True


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("1-3,5", [1, 2, 3, 5]), ("5,1,1", [1, 5]), (" 2 - 4 ", [2, 3, 4]), ("7", [7])],
)
def test_parse_page_spec(spec: str, expected: list[int]) -> None:
    assert parse_page_spec(spec) == expected


@pytest.mark.parametrize("spec", ["", "a", "1-", "3-1", "0", "1,,2", "-2", "1-2-3"])
def test_parse_page_spec_rejects_bad_input(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_page_spec(spec)


def test_validate_pages() -> None:
    assert validate_pages([3, 1, 3], 3) == [1, 3]
    with pytest.raises(PfaError):
        validate_pages([], 3)
    with pytest.raises(PfaError):
        validate_pages([4], 3)
    with pytest.raises(PfaError):
        validate_pages([0], 3)


def test_dump_load_roundtrip() -> None:
    assert load_pages(dump_pages([1, 2])) == [1, 2]
    assert load_pages(None) is None
    assert load_pages("garbage") is None
    assert dump_pages(None) is None


def test_parse_page_spec_bounds_ranges_before_expanding() -> None:
    assert parse_page_spec("1-3", max_page=3) == [1, 2, 3]
    with pytest.raises(ValueError, match="out of range"):
        parse_page_spec("1-999999999999", max_page=5)
    with pytest.raises(ValueError, match="out of range"):
        parse_page_spec("1-999999999999")  # the global ceiling applies without a page count
