"""Tests for `layout_a_credit` (P1-B1, §2c Layout A)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, RawTransaction, Word
from personal_finance_analyzer.ingest.parsers.layout_a_credit import parser
from tests.fixtures.gen.layout_a import builder

_HERE = Path(__file__).resolve().parent
_FIXTURES_DIR = _HERE.parent / "fixtures" / "generated" / "layout_a_credit"
_GOLDEN_DIR = _HERE / "golden" / "layout_a_credit"

_GOLDEN_VARIANTS = ("normal", "multiline", "fx", "refund_and_payment", "no_summary")


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


def _load_golden(variant: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        (_GOLDEN_DIR / f"layout_a_credit_{variant}.json").read_text()
    )
    return result


def _load_doc(variant: str) -> ExtractedDoc:
    return _extract_doc(_FIXTURES_DIR / f"layout_a_credit_{variant}.pdf")


def _txn_dict(txn: RawTransaction) -> dict[str, Any]:
    return {
        "posted_date": txn.posted_date.isoformat(),
        "transaction_date": txn.transaction_date.isoformat() if txn.transaction_date else None,
        "description": txn.description,
        "amount_minor": txn.amount_minor,
        "currency": txn.currency,
        "kind_hint": txn.kind_hint,
        "section": txn.section,
        "fx_amount_minor": txn.fx_amount_minor,
        "fx_currency": txn.fx_currency,
        "fx_rate": txn.fx_rate,
    }


def _balance_equation_holds(
    opening: int | None, closing: int | None, transactions: tuple[RawTransaction, ...]
) -> bool:
    if opening is None or closing is None:
        return False
    return closing - opening == sum(t.amount_minor for t in transactions)


# --------------------------------------------------------------------------------------------
# Golden match per variant
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _GOLDEN_VARIANTS)
def test_golden_match(variant: str) -> None:
    doc = _load_doc(variant)
    golden = _load_golden(variant)
    parsed = parser.parse(doc)

    assert (parsed.period_start.isoformat() if parsed.period_start else None) == golden[
        "period_start"
    ]
    assert (parsed.period_end.isoformat() if parsed.period_end else None) == golden["period_end"]
    assert parsed.opening_balance_minor == golden["opening_balance_minor"]
    assert parsed.closing_balance_minor == golden["closing_balance_minor"]
    assert [list(t) for t in parsed.section_totals] == golden["section_totals"]
    assert [_txn_dict(t) for t in parsed.transactions] == golden["transactions"]
    assert parsed.account_hint.mask == golden["mask"]


def test_golden_stable_across_two_generator_runs(tmp_path: Path) -> None:
    for variant in builder.variants:
        first = builder.build(tmp_path / "run1", variant=variant, seed=0)
        second = builder.build(tmp_path / "run2", variant=variant, seed=0)
        assert first == second


def test_committed_pdfs_match_fresh_regeneration(tmp_path: Path) -> None:
    """The committed PDFs under `tests/fixtures/generated/layout_a_credit/` must be exactly what
    the (deterministic) generator produces today — this is what catches drift between the
    builder and what was last committed, since the suite itself never rewrites the committed
    files."""
    for variant in builder.variants:
        name = f"layout_a_credit_{variant}"
        builder.build(tmp_path, variant=variant, seed=0)
        committed = (_FIXTURES_DIR / f"{name}.pdf").read_bytes()
        fresh = (tmp_path / f"{name}.pdf").read_bytes()
        assert committed == fresh, f"{name}.pdf is stale — regenerate committed fixtures"


# --------------------------------------------------------------------------------------------
# detect()
# --------------------------------------------------------------------------------------------


def test_detect_scores_own_fixture_above_threshold() -> None:
    doc = _load_doc("normal")
    assert parser.detect(doc) > 0.5


def test_detect_empty_doc_returns_zero_without_raising() -> None:
    empty = ExtractedDoc(file_sha256="x", page_count=0, pages=(), has_text_layer=False)
    assert parser.detect(empty) == 0.0


# --------------------------------------------------------------------------------------------
# Sign convention (I5)
# --------------------------------------------------------------------------------------------


def test_sign_purchase_refund_payment() -> None:
    doc = _load_doc("refund_and_payment")
    parsed = parser.parse(doc)

    purchases = [t for t in parsed.transactions if t.section == "Purchases and Adjustments"]
    payments = [t for t in parsed.transactions if t.section == "Payments and Other Credits"]

    purchase = next(t for t in purchases if t.description == "COFFEE SHOP SEATTLE WA")
    assert purchase.amount_minor > 0  # a purchase is positive (outflow)

    payment = next(t for t in payments if "PAYMENT" in t.description)
    assert payment.amount_minor < 0  # a payment is negative (inflow)

    refund = next(t for t in payments if "REFUND" in t.description)
    assert refund.amount_minor < 0  # a refund is negative (inflow)

    adjustment = next(t for t in purchases if t.kind_hint == "adjustment")
    assert adjustment.amount_minor < 0  # a merchandise-return credit is negative (inflow)


# --------------------------------------------------------------------------------------------
# Layout-A-specific must-pass tests
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _GOLDEN_VARIANTS)
def test_reference_and_account_number_never_leak_into_description(variant: str) -> None:
    doc = _load_doc(variant)
    parsed = parser.parse(doc)
    for txn in parsed.transactions:
        assert not re.search(r"\bREF\d", txn.description)
        assert "XXXX" not in txn.description


def test_section_totals_parsed_and_reconciled_independently() -> None:
    doc = _load_doc("normal")
    parsed = parser.parse(doc)

    by_section: dict[str, int] = {}
    for txn in parsed.transactions:
        assert txn.section is not None
        by_section[txn.section] = by_section.get(txn.section, 0) + txn.amount_minor

    assert dict(parsed.section_totals) == by_section


def test_balance_equation_holds_on_normal_fixture() -> None:
    doc = _load_doc("normal")
    parsed = parser.parse(doc)
    assert _balance_equation_holds(
        parsed.opening_balance_minor, parsed.closing_balance_minor, parsed.transactions
    )


def test_balance_equation_fails_when_a_sign_is_flipped() -> None:
    doc = _load_doc("normal")
    parsed = parser.parse(doc)
    assert parsed.transactions  # sanity: there is something to flip

    flipped = list(parsed.transactions)
    first = flipped[0]
    flipped[0] = RawTransaction(
        posted_date=first.posted_date,
        transaction_date=first.transaction_date,
        description=first.description,
        amount_minor=-first.amount_minor,
        currency=first.currency,
        kind_hint=first.kind_hint,
        section=first.section,
    )

    assert not _balance_equation_holds(
        parsed.opening_balance_minor, parsed.closing_balance_minor, tuple(flipped)
    )


def test_multiline_description_merges_with_single_space() -> None:
    doc = _load_doc("multiline")
    parsed = parser.parse(doc)
    merged = next(t for t in parsed.transactions if t.description.startswith("AMAZON"))
    assert merged.description == "AMAZON MARKETPLACE ORDER #123-4567890 SEATTLE WA"


def test_fx_line_populates_fx_fields() -> None:
    doc = _load_doc("fx")
    parsed = parser.parse(doc)
    fx_txn = next(t for t in parsed.transactions if t.description == "PARIS BISTRO PARIS FR")
    assert fx_txn.fx_amount_minor == 4000
    assert fx_txn.fx_currency == "EUR"
    assert fx_txn.fx_rate == pytest.approx(1.125)


def test_no_summary_parses_with_balances_none_and_shape_warning() -> None:
    doc = _load_doc("no_summary")
    parsed = parser.parse(doc)
    assert parsed.opening_balance_minor is None
    assert parsed.closing_balance_minor is None
    assert parsed.period_start is None
    assert parsed.period_end is None
    assert any("summary" in w.lower() for w in parsed.warnings)
    # Transactions still parse fine: layout A's dates carry an explicit year (§2c).
    assert len(parsed.transactions) >= 3


def test_malformed_raises_parser_error(tmp_path: Path) -> None:
    builder.build(tmp_path, variant="malformed", seed=0)
    doc = _extract_doc(tmp_path / "layout_a_credit_malformed.pdf")
    with pytest.raises(ParserError):
        parser.parse(doc)
