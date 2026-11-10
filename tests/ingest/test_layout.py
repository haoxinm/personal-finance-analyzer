from __future__ import annotations

from datetime import date
from pathlib import Path

import pdfplumber
import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import (
    Band,
    Row,
    assign_to_bands,
    cluster_rows,
    infer_column_bands,
    infer_year,
    locate_table_bands,
    parse_date,
    parse_money,
)
from tests.fixtures.gen.base import format_row, render_lines_pdf

# --------------------------------------------------------------------------------------------
# parse_money
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_minor", "expected_negative"),
    [
        ("12.34", 1234, False),
        ("0.00", 0, False),
        ("1,234.56", 123456, False),
        ("-12.34", 1234, True),
        ("(12.34)", 1234, True),
        ("12.34 CR", 1234, True),
        ("12.34-", 1234, True),
        ("$12.34", 1234, False),
        ("$1,234.56", 123456, False),
        ("  12.34  ", 1234, False),
        ("1,234,567.89", 123456789, False),
    ],
)
def test_parse_money_shapes(text: str, expected_minor: int, expected_negative: bool) -> None:
    parsed = parse_money(text)
    assert parsed is not None
    assert parsed.magnitude_minor == expected_minor
    assert parsed.printed_negative is expected_negative


@pytest.mark.parametrize("text", ["", "abc", "REF#12345", "12/05/2026", "(12.34", "12.34)"])
def test_parse_money_rejects_non_money(text: str) -> None:
    assert parse_money(text) is None


# --------------------------------------------------------------------------------------------
# parse_date / infer_year
# --------------------------------------------------------------------------------------------


def test_parse_date_with_and_without_year() -> None:
    with_year = parse_date("01/05/2026", ("%m/%d/%Y", "%m/%d"))
    assert with_year is not None
    assert with_year.year == 2026
    assert with_year.month == 1
    assert with_year.day == 5

    without_year = parse_date("01/05", ("%m/%d/%Y", "%m/%d"))
    assert without_year is not None
    assert without_year.year is None
    assert without_year.month == 1
    assert without_year.day == 5


def test_parse_date_returns_none_when_no_format_matches() -> None:
    assert parse_date("not a date", ("%m/%d/%Y",)) is None


def test_infer_year_december_on_january_statement() -> None:
    # A statement period crossing the year boundary (e.g. Dec 28 - Jan 27), as real monthly
    # statements do; a December-dated row must resolve to the prior year.
    partial = parse_date("12/29", ("%m/%d",))
    assert partial is not None
    resolved = infer_year(partial, date(2025, 12, 28), date(2026, 1, 27))
    assert resolved == date(2025, 12, 29)


def test_infer_year_january_on_december_statement() -> None:
    # Same boundary-crossing period; a January-dated row must resolve to the later year.
    partial = parse_date("01/05", ("%m/%d",))
    assert partial is not None
    resolved = infer_year(partial, date(2025, 12, 28), date(2026, 1, 27))
    assert resolved == date(2026, 1, 5)


def test_infer_year_raises_when_no_candidate_fits() -> None:
    partial = parse_date("06/15", ("%m/%d",))
    assert partial is not None
    with pytest.raises(ParserError):
        infer_year(partial, date(2026, 1, 1), date(2026, 1, 31))


def test_infer_year_returns_as_is_when_year_present() -> None:
    partial = parse_date("06/15/2026", ("%m/%d/%Y",))
    assert partial is not None
    resolved = infer_year(partial, date(2026, 1, 1), date(2026, 1, 31))
    assert resolved == date(2026, 6, 15)


# --------------------------------------------------------------------------------------------
# cluster_rows / infer_column_bands / assign_to_bands
# --------------------------------------------------------------------------------------------


def _word(text: str, x0: float, top: float, width: float = 20.0, height: float = 10.0) -> Word:
    return Word(text=text, x0=x0, x1=x0 + width, top=top, bottom=top + height)


def test_cluster_rows_groups_by_y() -> None:
    words = [
        _word("01/05/2026", x0=50, top=100),
        _word("COFFEE", x0=120, top=101),
        _word("SHOP", x0=170, top=100),
        _word("12.34", x0=400, top=100.5),
        _word("01/06/2026", x0=50, top=120),
        _word("GAS", x0=120, top=120),
    ]
    rows = cluster_rows(words)
    assert len(rows) == 2
    assert rows[0].text == "01/05/2026 COFFEE SHOP 12.34"
    assert rows[1].text == "01/06/2026 GAS"


def test_cluster_rows_empty_input() -> None:
    assert cluster_rows(()) == ()


def test_infer_column_bands_and_assign() -> None:
    rows = [
        Row(
            words=(_word("01/05/2026", x0=50, top=100), _word("12.34", x0=400, top=100)),
            top=100,
            bottom=110,
            text="01/05/2026 12.34",
        ),
        Row(
            words=(_word("01/06/2026", x0=50, top=120), _word("56.78", x0=400, top=120)),
            top=120,
            bottom=130,
            text="01/06/2026 56.78",
        ),
        Row(
            words=(_word("01/07/2026", x0=50, top=140), _word("9.00", x0=400, top=140)),
            top=140,
            bottom=150,
            text="01/07/2026 9.00",
        ),
    ]
    bands = infer_column_bands(rows)
    assert len(bands) == 2
    assigned = assign_to_bands(rows[0], bands)
    assert assigned[bands[0].name] == "01/05/2026"
    assert assigned[bands[1].name] == "12.34"


def test_band_dataclass_bounds() -> None:
    band = Band(name="col_0", x0=0.0, x1=100.0)
    assert band.x0 < band.x1


# --------------------------------------------------------------------------------------------
# locate_table_bands (§2e.2 Stage 1)
# --------------------------------------------------------------------------------------------


def _extract_doc(pdf_path: Path) -> ExtractedDoc:
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = tuple(
                Word(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"])
                for w in page.extract_words()
            )
            text = page.extract_text() or ""
            pages.append(PageText(page_number=i, text=text, words=words, char_count=len(text)))
    doc_pages = tuple(pages)
    median_chars = sorted(p.char_count for p in doc_pages)[len(doc_pages) // 2] if doc_pages else 0
    return ExtractedDoc(
        file_sha256="test-sha",
        page_count=len(doc_pages),
        pages=doc_pages,
        has_text_layer=median_chars >= 50,
    )


_COLUMN_WIDTHS = (12, 30, 10)


def test_locate_table_bands_finds_table(tmp_path: Path) -> None:
    header = format_row(["Date", "Description", "Amount"], _COLUMN_WIDTHS)
    rows = [
        format_row(["01/05/2026", "COFFEE SHOP SEATTLE WA", "12.34"], _COLUMN_WIDTHS),
        format_row(["01/06/2026", "GROCERY STORE ANYTOWN", "56.78"], _COLUMN_WIDTHS),
        format_row(["01/07/2026", "GAS STATION HIGHWAY 1", "40.00"], _COLUMN_WIDTHS),
        format_row(["01/08/2026", "ONLINE STORE PURCHASE", "9.99"], _COLUMN_WIDTHS),
    ]
    pdf_path = tmp_path / "table.pdf"
    render_lines_pdf(pdf_path, [[header, *rows]])

    doc = _extract_doc(pdf_path)
    bands = locate_table_bands(doc)

    assert len(bands) == 1
    band = bands[0]
    assert len(band.rows) == 4
    assert band.header_row_index is not None
    assert all(kind == "data" for kind in band.row_kinds)


def test_locate_table_bands_includes_trailing_continuation_at_page_end(tmp_path: Path) -> None:
    # A wrapped description line with no date and no amount, as the literal last line of the
    # table on the page, must still be pulled into the band (not silently dropped).
    header = format_row(["Date", "Description", "Amount"], _COLUMN_WIDTHS)
    data_rows = [
        format_row(["01/05/2026", "COFFEE SHOP SEATTLE WA", "12.34"], _COLUMN_WIDTHS),
        format_row(["01/06/2026", "GROCERY STORE ANYTOWN", "56.78"], _COLUMN_WIDTHS),
        format_row(["01/07/2026", "GAS STATION HIGHWAY 1", "40.00"], _COLUMN_WIDTHS),
        format_row(["01/08/2026", "Foreign purchase EUR", "9.99"], _COLUMN_WIDTHS),
    ]
    # Indented into the description column, like a wrapped continuation of the row above.
    continuation = " " * 14 + "Fx rate 1.0842, orig amt 9.21 EUR"
    pdf_path = tmp_path / "trailing_continuation.pdf"
    render_lines_pdf(pdf_path, [[header, *data_rows, continuation]])

    doc = _extract_doc(pdf_path)
    bands = locate_table_bands(doc)

    assert len(bands) == 1
    band = bands[0]
    assert len(band.rows) == 5
    assert band.rows[-1].text.strip().startswith("Fx rate")
    # Not a fresh transaction row: it carries no date, so downstream parsers fold it into the
    # previous transaction's description regardless of the exact row kind assigned.
    assert band.row_kinds[-1] != "total"
    assert band.row_kinds[-1] != "header"


def test_locate_table_bands_includes_continuation_but_not_trailing_footer(tmp_path: Path) -> None:
    # Same as above, but the continuation line is itself followed by a non-table footer line.
    # The continuation belongs to the table; the footer does not.
    header = format_row(["Date", "Description", "Amount"], _COLUMN_WIDTHS)
    data_rows = [
        format_row(["01/05/2026", "COFFEE SHOP SEATTLE WA", "12.34"], _COLUMN_WIDTHS),
        format_row(["01/06/2026", "GROCERY STORE ANYTOWN", "56.78"], _COLUMN_WIDTHS),
        format_row(["01/07/2026", "GAS STATION HIGHWAY 1", "40.00"], _COLUMN_WIDTHS),
        format_row(["01/08/2026", "Foreign purchase EUR", "9.99"], _COLUMN_WIDTHS),
    ]
    continuation = " " * 14 + "Fx rate 1.0842, orig amt 9.21 EUR"
    footer = "Page 1 of 1"  # flush with the date column: page furniture, not a continuation
    pdf_path = tmp_path / "trailing_continuation_and_footer.pdf"
    render_lines_pdf(pdf_path, [[header, *data_rows, continuation, footer]])

    doc = _extract_doc(pdf_path)
    bands = locate_table_bands(doc)

    assert len(bands) == 1
    band = bands[0]
    assert len(band.rows) == 5
    assert band.rows[-1].text.strip().startswith("Fx rate")
    assert all("Page 1 of 1" not in row.text for row in band.rows)


def test_locate_table_bands_returns_empty_for_prose_page(tmp_path: Path) -> None:
    lines = [
        "Thank you for being a valued customer.",
        "Please review the information below carefully.",
        "Contact support if you have any questions about your account.",
        "This is not a transaction table at all, just some prose text.",
    ]
    pdf_path = tmp_path / "prose.pdf"
    render_lines_pdf(pdf_path, [lines])

    doc = _extract_doc(pdf_path)
    bands = locate_table_bands(doc)

    assert bands == ()
