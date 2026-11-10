"""Tests for Layout B — ``layout_b_credit`` (P1-B2, §2c).

Fixtures are generated once by `tests/generate_fixtures.py` into
``tests/fixtures/generated/layout_b_credit/`` and their goldens are mirrored, checked in, at
``tests/ingest/golden/layout_b_credit/``. These tests parse the generated PDFs and compare
against the checked-in goldens, so a regenerate-and-diff always shows exactly what changed.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest import registry
from personal_finance_analyzer.ingest.parsers.layout_b_credit import LayoutBCreditParser
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.fixtures.gen.layout_b import layout_b

_GENERATED_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "generated" / "layout_b_credit"
)
_GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "layout_b_credit"

_GOLDEN_VARIANTS = ("normal", "multiline", "fx", "refund_and_payment")
_ERROR_VARIANTS = ("malformed", "no_summary")


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


def _statement_to_dict(statement: Any) -> dict[str, Any]:
    return {
        "account_hint": {
            "account_type": statement.account_hint.account_type,
            "mask": statement.account_hint.mask,
            "currency": statement.account_hint.currency,
        },
        "period_start": statement.period_start.isoformat() if statement.period_start else None,
        "period_end": statement.period_end.isoformat() if statement.period_end else None,
        "stated_total_minor": statement.stated_total_minor,
        "opening_balance_minor": statement.opening_balance_minor,
        "closing_balance_minor": statement.closing_balance_minor,
        "section_totals": [list(pair) for pair in statement.section_totals],
        "warnings": list(statement.warnings),
        "transactions": [
            {
                "posted_date": txn.posted_date.isoformat(),
                "transaction_date": txn.transaction_date.isoformat()
                if txn.transaction_date
                else None,
                "description": txn.description,
                "amount_minor": txn.amount_minor,
                "currency": txn.currency,
                "fx_amount_minor": txn.fx_amount_minor,
                "fx_currency": txn.fx_currency,
                "fx_rate": txn.fx_rate,
                "kind_hint": txn.kind_hint,
                "section": txn.section,
                "issuer_category": txn.issuer_category,
            }
            for txn in statement.transactions
        ],
    }


def _pdf_path(variant: str) -> Path:
    return _GENERATED_DIR / f"layout_b_credit_{variant}.pdf"


def _golden(variant: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_GOLDEN_DIR / f"layout_b_credit_{variant}.json").read_text())
    return data


@pytest.fixture(scope="module")
def parser() -> LayoutBCreditParser:
    return LayoutBCreditParser()


# --------------------------------------------------------------------------------------------
# Golden match
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _GOLDEN_VARIANTS)
def test_golden_match(parser: LayoutBCreditParser, variant: str) -> None:
    doc = _extract_doc(_pdf_path(variant))
    statement = parser.parse(doc)
    assert _statement_to_dict(statement) == _golden(variant)


@pytest.mark.parametrize("variant", _ERROR_VARIANTS)
def test_error_variants_raise_parser_error(parser: LayoutBCreditParser, variant: str) -> None:
    doc = _extract_doc(_pdf_path(variant))
    with pytest.raises(ParserError):
        parser.parse(doc)


def test_goldens_stable_across_two_generator_runs(tmp_path: Path) -> None:
    first = layout_b.build(tmp_path / "run1", variant="normal", seed=0)
    second = layout_b.build(tmp_path / "run2", variant="normal", seed=0)
    assert first == second


def test_committed_pdfs_match_fresh_regeneration(tmp_path: Path) -> None:
    """The committed PDFs under `tests/fixtures/generated/layout_b_credit/` must be exactly what
    the (deterministic) generator produces today — this is what catches drift between the
    builder and what was last committed, since the suite itself never rewrites the committed
    files."""
    for variant in layout_b.variants:
        name = f"layout_b_credit_{variant}"
        layout_b.build(tmp_path, variant=variant, seed=0)
        committed = (_GENERATED_DIR / f"{name}.pdf").read_bytes()
        fresh = (tmp_path / f"{name}.pdf").read_bytes()
        assert committed == fresh, f"{name}.pdf is stale — regenerate committed fixtures"


# --------------------------------------------------------------------------------------------
# detect()
# --------------------------------------------------------------------------------------------


def test_detect_scores_own_fixture_above_threshold(parser: LayoutBCreditParser) -> None:
    doc = _extract_doc(_pdf_path("normal"))
    assert parser.detect(doc) > 0.5


def test_detect_returns_zero_without_raising_on_empty_doc(parser: LayoutBCreditParser) -> None:
    empty_doc = ExtractedDoc(file_sha256="empty", page_count=0, pages=(), has_text_layer=False)
    assert parser.detect(empty_doc) == 0.0


def test_registry_selects_layout_b_credit_for_its_own_fixture() -> None:
    """Regression test: `layout_b_credit.py` must export a module-level parser instance so
    `registry.discover_builtin_parsers()` finds it — otherwise `registry.select()` silently
    routes real Layout B statements to `generic_table`."""
    doc = _extract_doc(_pdf_path("normal"))
    selected = registry.select(doc)
    assert selected.id == "layout_b_credit"


# --------------------------------------------------------------------------------------------
# Sign convention (I5) — asserted per section, not only in aggregate.
# --------------------------------------------------------------------------------------------


def test_sign_convention_purchase_refund_payment(parser: LayoutBCreditParser) -> None:
    doc = _extract_doc(_pdf_path("refund_and_payment"))
    statement = parser.parse(doc)
    by_desc = {txn.description: txn for txn in statement.transactions}

    # A29: a PAYMENTS AND OTHER CREDITS heading cannot tell payment from refund on its own —
    # both are the ambiguous `payment_or_refund` hint; classify/kinds.py resolves it by pattern.
    payment = by_desc["ONLINE PAYMENT THANK YOU"]
    assert payment.kind_hint == "payment_or_refund"
    assert payment.amount_minor == -30000  # negative: money returning (I5)

    refund = by_desc["MERCHANT REFUND CREDIT CO"]
    assert refund.kind_hint == "payment_or_refund"
    assert refund.amount_minor == -2500  # negative: money returning (I5)

    purchase = by_desc["COFFEE ROASTERS DOWNTOWN"]
    assert purchase.kind_hint == "purchase"
    assert purchase.amount_minor == 1250  # positive: money leaving (I5)


# --------------------------------------------------------------------------------------------
# Layout-B must-pass tests (§2c, §1707 acceptance table)
# --------------------------------------------------------------------------------------------


def test_interest_charged_table_contributes_zero_transactions(parser: LayoutBCreditParser) -> None:
    doc = _extract_doc(_pdf_path("normal"))
    statement = parser.parse(doc)
    # The INTEREST CHARGED table's own "PURCHASES" balance-type row must never be mistaken for
    # the ACCOUNT ACTIVITY PURCHASES section: no transaction carries an APR-shaped amount, and
    # the transaction count matches only what ACCOUNT ACTIVITY prints (six rows in the normal
    # fixture; the interest table below it has three balance-type rows that must add nothing).
    assert len(statement.transactions) == 6
    assert all(txn.amount_minor not in (2299, 2599) for txn in statement.transactions)


def test_december_date_on_january_statement_resolves_to_prior_year(tmp_path: Path) -> None:
    """Explicit year-boundary test (§2c: "write an explicit year-boundary test")."""
    widths = (20, 45, 12)
    lines = [
        "YEAR BOUNDARY BANK",
        "Account ending in 9999",
        "Opening Date: 12/20/2025",
        "Closing Date: 01/19/2026",
        "Previous Balance: $100.00",
        "New Balance: $150.00",
        "",
        "ACCOUNT ACTIVITY",
        format_row(
            ["Date of Transaction", "Merchant Name or Transaction Description", "$ Amount"],
            widths,
        ),
        "PURCHASES",
        format_row(["12/22", "WINTER MARKET STALL", "25.00"], widths),
        format_row(["01/05", "NEW YEAR CAFE", "25.00"], widths),
    ]
    pdf_path = tmp_path / "year_boundary.pdf"
    render_lines_pdf(pdf_path, [lines])

    doc = _extract_doc(pdf_path)
    statement = LayoutBCreditParser().parse(doc)

    by_desc = {txn.description: txn for txn in statement.transactions}
    assert by_desc["WINTER MARKET STALL"].posted_date == date(2025, 12, 22)
    assert by_desc["NEW YEAR CAFE"].posted_date == date(2026, 1, 5)


# --------------------------------------------------------------------------------------------
# Reconciliation (A25) — the balance equation catches what totals-of-magnitudes miss.
# --------------------------------------------------------------------------------------------


def test_balance_equation_holds_on_normal_fixture(parser: LayoutBCreditParser) -> None:
    doc = _extract_doc(_pdf_path("normal"))
    statement = parser.parse(doc)
    assert statement.opening_balance_minor is not None
    assert statement.closing_balance_minor is not None
    total = sum(txn.amount_minor for txn in statement.transactions)
    assert statement.closing_balance_minor - statement.opening_balance_minor == total


def test_balance_equation_fails_when_one_sign_is_flipped(parser: LayoutBCreditParser) -> None:
    doc = _extract_doc(_pdf_path("normal"))
    statement = parser.parse(doc)
    assert statement.opening_balance_minor is not None
    assert statement.closing_balance_minor is not None
    total_as_parsed = sum(txn.amount_minor for txn in statement.transactions)
    assert statement.closing_balance_minor - statement.opening_balance_minor == total_as_parsed

    # Flip the sign of one row (as if a parser bug mis-signed a purchase) and show the balance
    # equation, unlike a bare total-of-magnitudes, catches it.
    flipped_total = total_as_parsed - 2 * statement.transactions[1].amount_minor
    assert statement.closing_balance_minor - statement.opening_balance_minor != flipped_total
