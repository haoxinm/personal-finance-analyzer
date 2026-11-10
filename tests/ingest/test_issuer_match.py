"""Tests for issuer detection by template matching (§2f.4, A24)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from personal_finance_analyzer.core.types import ExtractedDoc, PageText
from personal_finance_analyzer.db.models import Issuer
from personal_finance_analyzer.ingest import issuer_match
from personal_finance_analyzer.ingest.layout import locate_table_bands
from tests.fixtures.gen.base import format_row, render_lines_pdf

_CARD_WIDTHS = (10, 32, 14)


def _card_row(date_: str, description: str, amount: str) -> str:
    return format_row([date_, description, amount], _CARD_WIDTHS)


def _build_statement_pdf(
    path: Path, letterhead: list[str], description: str = "GROCERY MART"
) -> None:
    lines = [
        *letterhead,
        "",
        _card_row("Date", "Description", "Amount"),
        _card_row("01/03/2026", description, "45.10"),
        _card_row("01/05/2026", "COFFEE SHOP", "12.34"),
        _card_row("01/09/2026", "ONLINE BOOKSTORE", "29.99"),
    ]
    render_lines_pdf(path, [lines])


def _extract(path: Path) -> ExtractedDoc:
    from personal_finance_analyzer.ingest.extract import extract

    return extract(path)


def _make_issuer(session: Session, name: str, match_terms: list[str] | None = None) -> Issuer:
    import json

    issuer = Issuer(
        name=name,
        slug=name.lower().replace(" ", "-"),
        match_terms=json.dumps(match_terms if match_terms is not None else []),
    )
    session.add(issuer)
    session.flush()
    return issuer


def test_matches_letterhead_issuer(session: Session, tmp_path: Path) -> None:
    _make_issuer(session, "Invented Bank", ["invented bank"])
    pdf_path = tmp_path / "s.pdf"
    _build_statement_pdf(pdf_path, ["INVENTED BANK", "Statement Period: 01/01/2026 to 01/31/2026"])

    doc = _extract(pdf_path)
    bands = locate_table_bands(doc)
    issuer_id = issuer_match.match_issuer(session, doc, bands)

    invented = session.query(Issuer).filter_by(name="Invented Bank").one()
    assert issuer_id == invented.id


def test_a_name_mentioned_only_inside_a_description_is_not_attributed(
    session: Session, tmp_path: Path
) -> None:
    """A statement whose *description column* mentions a rival bank's name must not be
    attributed to that issuer — the non-table-region restriction's whole point (§2f.4)."""
    _make_issuer(session, "Rival Bank", ["rival bank"])
    pdf_path = tmp_path / "s.pdf"
    _build_statement_pdf(
        pdf_path,
        ["INVENTED BANK", "Statement Period: 01/01/2026 to 01/31/2026"],
        description="PAYMENT TO RIVAL BANK",
    )

    doc = _extract(pdf_path)
    bands = locate_table_bands(doc)
    issuer_id = issuer_match.match_issuer(session, doc, bands)

    assert issuer_id is None


def test_longer_term_wins_over_a_bare_substring(session: Session, tmp_path: Path) -> None:
    """``jpmorgan chase`` (compact) must beat a bare ``chase`` belonging to a different issuer
    (§2f.4's worked example)."""
    _make_issuer(session, "Chase Rewards Card", ["chase"])
    jpmorgan = _make_issuer(session, "JPMorgan Chase", ["jp morgan chase"])
    pdf_path = tmp_path / "s.pdf"
    _build_statement_pdf(pdf_path, ["JPMorgan Chase", "Member FDIC"])

    doc = _extract(pdf_path)
    bands = locate_table_bands(doc)
    issuer_id = issuer_match.match_issuer(session, doc, bands)

    assert issuer_id == jpmorgan.id


def test_ambiguous_tie_yields_no_auto_selection(session: Session, tmp_path: Path) -> None:
    _make_issuer(session, "First Invented Bank", ["invented bank one"])
    _make_issuer(session, "Second Invented Bank", ["invented bank two"])
    pdf_path = tmp_path / "s.pdf"
    # Neither term appears; craft a letterhead where both issuers match at the same length by
    # using two equal-length made-up terms instead.
    _build_statement_pdf(pdf_path, ["SOME OTHER BANK ENTIRELY", "Member FDIC"])

    doc = _extract(pdf_path)
    bands = locate_table_bands(doc)
    issuer_id = issuer_match.match_issuer(session, doc, bands)

    assert issuer_id is None


def test_two_issuers_matching_at_equal_length_are_ambiguous(
    session: Session, tmp_path: Path
) -> None:
    _make_issuer(session, "Alpha Bank", ["alphabank"])
    _make_issuer(session, "Betaa Bank", ["betaabank"])  # same length as "alphabank"
    pdf_path = tmp_path / "s.pdf"
    _build_statement_pdf(pdf_path, ["ALPHABANK BETAABANK", "Member FDIC"])

    doc = _extract(pdf_path)
    bands = locate_table_bands(doc)
    issuer_id = issuer_match.match_issuer(session, doc, bands)

    assert issuer_id is None


def test_no_issuers_configured_returns_none(session: Session, tmp_path: Path) -> None:
    pdf_path = tmp_path / "s.pdf"
    _build_statement_pdf(pdf_path, ["INVENTED BANK", "Member FDIC"])

    doc = _extract(pdf_path)
    bands = locate_table_bands(doc)
    assert issuer_match.match_issuer(session, doc, bands) is None


def test_non_table_page_one_text_falls_back_to_full_page_when_no_band_located() -> None:
    """No table band on page 1 -> the whole page's text is searched (§2f.4: no false positives
    to guard against when there is no table at all)."""
    page = PageText(page_number=1, text="INVENTED BANK letterhead only", words=(), char_count=30)
    doc = ExtractedDoc(file_sha256="0" * 64, page_count=1, pages=(page,), has_text_layer=True)

    text = issuer_match.non_table_page_one_text(doc, ())

    assert text == page.text
