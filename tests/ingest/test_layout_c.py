"""Tests for the Layout C parser (`layout_c_credit`, §2c "Layout C", P1-B3)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, RawTransaction, Word
from personal_finance_analyzer.ingest.parsers.layout_c_credit import _match_header, layout_c_credit
from tests.fixtures.gen.layout_c import layout_c_credit as layout_c_builder
from tests.generate_fixtures import generate_all

#: The committed PDFs/goldens under version control — read-only. Tests never write here; a test
#: session regenerates fixtures into a pytest tmp dir instead (see `_generated_dir` below) so
#: running the suite never modifies the repo tree.
_COMMITTED_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "generated" / "layout_c_credit"
)
#: The committed, authoritative golden copies (Owns: tests/ingest/golden/layout_c_credit/**).
#: `generate_fixtures.py` always writes its golden JSON next to the PDF it renders (frozen,
#: P0-7) — these are hand-verified copies of that same JSON, kept here so a golden change is
#: visible in review independently of the (also committed) generated directory.
_GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "layout_c_credit"

#: Set by the `_ensure_generated` autouse fixture to a pytest tmp directory holding a fresh
#: regeneration of every builder's fixtures. Tests parse from here, never from `_COMMITTED_DIR`,
#: so the suite is read-only with respect to the repo tree.
_generated_root: Path | None = None


def _generated_dir() -> Path:
    assert _generated_root is not None, "_ensure_generated fixture has not run yet"
    return _generated_root / "layout_c_credit"


_ERROR_TYPES: dict[str, type[Exception]] = {
    "ParserError": ParserError,
    "UnsupportedLayoutError": UnsupportedLayoutError,
}


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


def _load_variant(variant: str) -> tuple[ExtractedDoc, dict[str, Any]]:
    pdf_path = _generated_dir() / f"layout_c_credit_{variant}.pdf"
    golden_path = _GOLDEN_DIR / f"layout_c_credit_{variant}.json"
    doc = _extract_doc(pdf_path)
    golden = json.loads(golden_path.read_text())
    return doc, golden


def test_golden_dir_matches_generated_dir() -> None:
    """The committed `tests/ingest/golden/` copy and a fresh regeneration's own output must
    agree — a stale copy would make every other test in this module pass against the wrong
    golden."""
    for variant in layout_c_builder.variants:
        generated = json.loads((_generated_dir() / f"layout_c_credit_{variant}.json").read_text())
        golden = json.loads((_GOLDEN_DIR / f"layout_c_credit_{variant}.json").read_text())
        assert generated == golden


def test_committed_pdfs_match_fresh_regeneration() -> None:
    """The committed PDFs under `tests/fixtures/generated/layout_c_credit/` must be exactly what
    the (deterministic) generator produces today — this is what catches drift between the
    builder and what was last committed, since the suite itself never rewrites the committed
    files (see `_ensure_generated`)."""
    for variant in layout_c_builder.variants:
        name = f"layout_c_credit_{variant}"
        committed = (_COMMITTED_DIR / f"{name}.pdf").read_bytes()
        fresh = (_generated_dir() / f"{name}.pdf").read_bytes()
        assert committed == fresh, f"{name}.pdf is stale — regenerate committed fixtures"
        committed_json = json.loads((_COMMITTED_DIR / f"{name}.json").read_text())
        fresh_json = json.loads((_generated_dir() / f"{name}.json").read_text())
        assert committed_json == fresh_json, f"{name}.json is stale — regenerate committed fixtures"


def _empty_doc() -> ExtractedDoc:
    return ExtractedDoc(file_sha256="empty", page_count=0, pages=(), has_text_layer=False)


# --------------------------------------------------------------------------------------------
# Generated fixtures exist and are current
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _ensure_generated(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Regenerate every builder's fixtures into a pytest tmp directory (never the repo tree) so
    a stale commit fails loudly (`test_committed_pdfs_match_fresh_regeneration`) instead of
    silently testing against old goldens, and so running the suite never modifies tracked
    files."""
    global _generated_root
    _generated_root = tmp_path_factory.mktemp("layout_c_generated")
    generate_all(_generated_root)


# --------------------------------------------------------------------------------------------
# detect()
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", layout_c_builder.variants)
def test_detect_scores_above_threshold_on_own_fixtures(variant: str) -> None:
    doc, _ = _load_variant(variant)
    assert layout_c_credit.detect(doc) > 0.5


def test_detect_returns_zero_without_raising_on_empty_doc() -> None:
    assert layout_c_credit.detect(_empty_doc()) == 0.0


# --------------------------------------------------------------------------------------------
# Header spelling (§2c "Layout C" must-pass: both spellings matched)
# --------------------------------------------------------------------------------------------


def test_both_header_spellings_are_matched() -> None:
    doc, _ = _load_variant("normal")
    rows_text = [row for page in doc.pages for row in page.text.splitlines()]
    assert any("TRANS. DATE" in row for row in rows_text)
    assert any("TRANS DATE" in row and "TRANS. DATE" not in row for row in rows_text)

    # Both header shapes are recognized directly by the parser's own matcher.
    from personal_finance_analyzer.ingest.layout import cluster_rows

    all_rows = [row for page in doc.pages for row in cluster_rows(page.words)]
    matches = [m for row in all_rows if (m := _match_header(row)) is not None]
    assert ("PAYMENTS AND CREDITS", False) in matches
    assert ("PURCHASES", True) in matches


# --------------------------------------------------------------------------------------------
# Successful parses, golden-matched
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["normal", "multiline", "fx", "refund_and_payment"])
def test_parse_matches_golden(variant: str) -> None:
    doc, golden = _load_variant(variant)
    expected = golden["parsed"]

    parsed = layout_c_credit.parse(doc)

    assert parsed.account_hint.account_type == expected["account_hint"]["account_type"]
    assert parsed.account_hint.mask == expected["account_hint"]["mask"]
    assert parsed.account_hint.currency == expected["account_hint"]["currency"]
    assert parsed.period_start == date.fromisoformat(expected["period_start"])
    assert parsed.period_end == date.fromisoformat(expected["period_end"])
    assert parsed.stated_total_minor == expected["stated_total_minor"]
    assert parsed.opening_balance_minor == expected["opening_balance_minor"]
    assert parsed.closing_balance_minor == expected["closing_balance_minor"]
    assert len(parsed.transactions) == len(expected["transactions"])
    for actual_tx, expected_tx in zip(parsed.transactions, expected["transactions"], strict=True):
        _assert_transaction_matches(actual_tx, expected_tx)

    # No spurious drift warnings on a well-formed statement.
    assert parsed.warnings == ()


def _assert_transaction_matches(actual: RawTransaction, expected: dict[str, Any]) -> None:
    assert actual.posted_date == date.fromisoformat(expected["posted_date"])
    assert actual.transaction_date is None
    assert actual.description == expected["description"]
    assert actual.amount_minor == expected["amount_minor"]
    assert actual.currency == expected["currency"]
    assert actual.fx_amount_minor == expected["fx_amount_minor"]
    assert actual.fx_currency == expected["fx_currency"]
    assert actual.fx_rate == expected["fx_rate"]
    assert actual.kind_hint == expected["kind_hint"]
    assert actual.section == expected["section"]
    assert actual.issuer_category == expected["issuer_category"]


# --------------------------------------------------------------------------------------------
# Sign test (I5) — the single most common source of bugs in this codebase
# --------------------------------------------------------------------------------------------


def test_sign_purchase_refund_payment() -> None:
    doc, _ = _load_variant("normal")
    parsed = layout_c_credit.parse(doc)

    purchases = [t for t in parsed.transactions if t.section == "PURCHASES"]
    payments_and_credits = [t for t in parsed.transactions if t.section == "PAYMENTS AND CREDITS"]

    assert purchases, "expected at least one purchase transaction"
    assert all(t.amount_minor > 0 for t in purchases), "a purchase must be positive (I5)"

    assert payments_and_credits, "expected at least one payment/credit transaction"
    assert all(t.amount_minor < 0 for t in payments_and_credits), (
        "a payment or refund must be negative (I5)"
    )

    # Specifically: the payment row and the refund row are both negative.
    payment = next(t for t in payments_and_credits if "PAYMENT" in t.description)
    refund = next(t for t in payments_and_credits if "REFUND" in t.description)
    assert payment.amount_minor < 0
    assert refund.amount_minor < 0


# --------------------------------------------------------------------------------------------
# Multi-line description merging
# --------------------------------------------------------------------------------------------


def test_multiline_description_merges_with_single_space() -> None:
    doc, _ = _load_variant("multiline")
    parsed = layout_c_credit.parse(doc)

    merged = next(t for t in parsed.transactions if "WEEKLY ORDER" in t.description)
    assert merged.description == "GROCERY STORE ANYTOWN WEEKLY ORDER"
    assert "  " not in merged.description


# --------------------------------------------------------------------------------------------
# Foreign-currency line handling
# --------------------------------------------------------------------------------------------


def test_fx_line_populates_fx_fields_and_is_excluded_from_description() -> None:
    doc, _ = _load_variant("fx")
    parsed = layout_c_credit.parse(doc)

    fx_tx = next(t for t in parsed.transactions if t.fx_currency is not None)
    assert fx_tx.fx_amount_minor == 12_500
    assert fx_tx.fx_currency == "EUR"
    assert fx_tx.fx_rate == pytest.approx(1.0870)
    assert "FX" not in fx_tx.description
    assert "EUR" not in fx_tx.description


# --------------------------------------------------------------------------------------------
# MERCHANT CATEGORY / A16
# --------------------------------------------------------------------------------------------


def test_merchant_category_captured_and_excluded_from_description() -> None:
    doc, _ = _load_variant("normal")
    parsed = layout_c_credit.parse(doc)

    purchases = [t for t in parsed.transactions if t.section == "PURCHASES"]
    assert all(t.issuer_category is not None for t in purchases)
    for tx in parsed.transactions:
        if tx.issuer_category:
            assert tx.issuer_category not in tx.description

    payments_and_credits = [t for t in parsed.transactions if t.section == "PAYMENTS AND CREDITS"]
    assert all(t.issuer_category is None for t in payments_and_credits)


# --------------------------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["malformed", "no_summary"])
def test_failure_variants_raise_and_never_return_wrong_rows(variant: str) -> None:
    doc, golden = _load_variant(variant)
    expected_error = _ERROR_TYPES[golden["expect_error"]]
    with pytest.raises(expected_error):
        layout_c_credit.parse(doc)


def test_no_summary_raises_parser_error_specifically() -> None:
    doc, _ = _load_variant("no_summary")
    with pytest.raises(ParserError):
        layout_c_credit.parse(doc)


def test_malformed_raises_unsupported_layout_error_specifically() -> None:
    doc, _ = _load_variant("malformed")
    with pytest.raises(UnsupportedLayoutError):
        layout_c_credit.parse(doc)


# --------------------------------------------------------------------------------------------
# Fixture-generator determinism
# --------------------------------------------------------------------------------------------


def test_goldens_are_stable_across_two_generator_runs(tmp_path: Path) -> None:
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    generate_all(out1)
    generate_all(out2)

    for variant in layout_c_builder.variants:
        golden1 = json.loads(
            (out1 / "layout_c_credit" / f"layout_c_credit_{variant}.json").read_text()
        )
        golden2 = json.loads(
            (out2 / "layout_c_credit" / f"layout_c_credit_{variant}.json").read_text()
        )
        assert golden1 == golden2
