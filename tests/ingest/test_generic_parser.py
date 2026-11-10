"""Tests for the generic fallback parser (P1-B).

Fixtures are generated once per test session (deterministic, seed=0) by
`tests.fixtures.gen.generic.GenericTableBuilder` and re-parsed here against the golden JSON
`generate_fixtures.py` writes next to each PDF.
"""

from __future__ import annotations

import json
from pathlib import Path

import pdfplumber
import pytest

from personal_finance_analyzer.core.errors import ParserError, UnsupportedLayoutError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout import Band
from personal_finance_analyzer.ingest.parsers.generic_table import (
    GenericTableParser,
    _ColumnRoles,
    _row_amount_minor,
    generic_table,
)
from tests.fixtures.gen.generic import generic_table_builder

#: The committed PDFs/goldens under version control — read-only. Tests never write here; each
#: test session regenerates fixtures into a pytest tmp dir instead (see `_fixtures_generated`)
#: so running the suite never modifies the repo tree.
_COMMITTED_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "generated" / "generic_table"

#: Set by the `_fixtures_generated` autouse fixture to a pytest tmp directory holding a fresh
#: regeneration of every variant. Tests parse from here, never from `_COMMITTED_DIR`.
_generated_dir: Path | None = None


def _extract(path: Path) -> ExtractedDoc:
    """A minimal, local PDF->`ExtractedDoc` reader for this test module only. P1-A owns the real
    `personal_finance_analyzer.ingest.extract.extract()`; this parser-level test does not depend on it."""
    with pdfplumber.open(path) as pdf:
        pages = []
        for page_number, page in enumerate(pdf.pages, start=1):
            words = tuple(
                Word(
                    text=w["text"],
                    x0=w["x0"],
                    x1=w["x1"],
                    top=w["top"],
                    bottom=w["bottom"],
                )
                for w in page.extract_words()
            )
            text = page.extract_text() or ""
            pages.append(
                PageText(page_number=page_number, text=text, words=words, char_count=len(text))
            )
    return ExtractedDoc(
        file_sha256="0" * 64,
        page_count=len(pages),
        pages=tuple(pages),
        has_text_layer=True,
    )


def _build(variant: str) -> tuple[ExtractedDoc, dict[str, object]]:
    assert _generated_dir is not None, "_fixtures_generated fixture has not run yet"
    golden = generic_table_builder.build(_generated_dir, variant=variant, seed=0)
    pdf_path = _generated_dir / f"generic_table_{variant}.pdf"
    return _extract(pdf_path), golden


@pytest.fixture(scope="module", autouse=True)
def _fixtures_generated(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Fixtures are committed (see `.gitignore`'s `!tests/fixtures/generated/**/*.pdf`), but
    regenerate them into a pytest tmp directory here too so the test is self-sufficient and
    catches drift between the builder and its own goldens, without ever writing into the repo
    tree (see `test_committed_pdfs_match_fresh_regeneration`)."""
    global _generated_dir
    _generated_dir = tmp_path_factory.mktemp("generic_table_generated")
    for variant in generic_table_builder.variants:
        generic_table_builder.build(_generated_dir, variant=variant, seed=0)


def _assert_transactions_match(doc: ExtractedDoc, golden: dict[str, object]) -> None:
    result = generic_table.parse(doc)
    assert (
        result.period_start and result.period_start.isoformat() == golden.get("period_start")
    ) or (result.period_start is None and golden.get("period_start") is None)
    assert result.opening_balance_minor == golden["opening_balance_minor"]
    assert result.closing_balance_minor == golden["closing_balance_minor"]
    golden_txns = golden["transactions"]
    assert isinstance(golden_txns, list)
    assert len(result.transactions) == len(golden_txns)
    for txn, expected in zip(result.transactions, golden_txns, strict=True):
        assert txn.posted_date.isoformat() == expected["posted_date"]
        assert txn.amount_minor == expected["amount_minor"]
        assert txn.description == expected["description"]
    # A parse that reconciles cleanly must carry no warnings.
    assert result.warnings == ()


# --------------------------------------------------------------------------------------------
# Card-style (signed amount column) variants
# --------------------------------------------------------------------------------------------


def test_normal_multi_page_card_statement() -> None:
    doc, golden = _build("normal")
    _assert_transactions_match(doc, golden)
    result = generic_table.parse(doc)
    assert result.account_hint.account_type == "credit"


def test_multiline_description_merges_with_single_space() -> None:
    doc, golden = _build("multiline")
    _assert_transactions_match(doc, golden)


def test_fx_continuation_line_folds_into_description() -> None:
    doc, golden = _build("fx")
    _assert_transactions_match(doc, golden)
    result = generic_table.parse(doc)
    assert "eur" in result.transactions[0].description.lower()
    assert "eur" in result.transactions[2].description.lower()


def test_refund_and_payment_signs() -> None:
    """I5: a purchase is positive, a refund is negative, a payment is negative."""
    doc, golden = _build("refund_and_payment")
    _assert_transactions_match(doc, golden)
    result = generic_table.parse(doc)
    purchase, refund, payment, purchase2 = result.transactions
    assert purchase.amount_minor > 0
    assert refund.amount_minor < 0
    assert payment.amount_minor < 0
    assert purchase2.amount_minor > 0


def test_no_summary_parses_with_balances_none() -> None:
    doc, golden = _build("no_summary")
    _assert_transactions_match(doc, golden)
    result = generic_table.parse(doc)
    assert result.period_start is None
    assert result.period_end is None
    assert result.opening_balance_minor is None
    assert result.closing_balance_minor is None


def test_malformed_raises_unsupported_layout() -> None:
    doc, _golden = _build("malformed")
    with pytest.raises(UnsupportedLayoutError):
        generic_table.parse(doc)


# --------------------------------------------------------------------------------------------
# Bank-style (debit/credit column pair + running balance) variant
# --------------------------------------------------------------------------------------------


def test_bank_debit_credit_mode_signs_and_running_balance() -> None:
    """A26/D11/D3: the generic parser's debit/credit-column mode, mirroring Layout D."""
    doc, golden = _build("bank_debit_credit")
    _assert_transactions_match(doc, golden)
    result = generic_table.parse(doc)
    assert result.account_hint.account_type == "checking"
    withdrawal, deposit, purchase, refund, purchase2, autopay = result.transactions
    assert withdrawal.amount_minor > 0  # debit column -> outflow
    assert deposit.amount_minor < 0  # credit column -> inflow
    assert purchase.amount_minor > 0
    assert refund.amount_minor < 0
    assert purchase2.amount_minor > 0
    assert autopay.amount_minor > 0


def test_both_debit_and_credit_populated_raises_parser_error() -> None:
    date_band = Band(name="date", x0=0.0, x1=10.0)
    debit_band = Band(name="debit", x0=10.0, x1=20.0)
    credit_band = Band(name="credit", x0=20.0, x1=30.0)
    roles = _ColumnRoles(
        date_band=date_band, description_bands=(), money_bands=(debit_band, credit_band)
    )
    cells = {"debit": "10.00", "credit": "5.00"}
    with pytest.raises(ParserError, match="both"):
        _row_amount_minor(cells, roles)


# --------------------------------------------------------------------------------------------
# Registry-facing behavior owned by this WP
# --------------------------------------------------------------------------------------------


def test_detect_never_raises_on_an_empty_document() -> None:
    empty_doc = ExtractedDoc(
        file_sha256="0" * 64,
        page_count=1,
        pages=(PageText(page_number=1, text="", words=(), char_count=0),),
        has_text_layer=False,
    )
    assert generic_table.detect(empty_doc) == 0.0
    with pytest.raises(UnsupportedLayoutError):
        generic_table.parse(empty_doc)


def test_detect_stays_below_registry_selection_threshold() -> None:
    doc, _golden = _build("normal")
    assert 0.0 <= generic_table.detect(doc) <= 0.5


def test_parser_identity() -> None:
    assert generic_table.id == "generic_table"
    assert isinstance(generic_table, GenericTableParser)
    assert generic_table.account_type in ("credit", "checking", "savings")


# --------------------------------------------------------------------------------------------
# Layout-independent fixtures (P1-B additionally owns these three)
# --------------------------------------------------------------------------------------------


def test_no_text_layer_fixture_has_no_extractable_text() -> None:
    doc, _golden = _build("no_text_layer")
    assert doc.full_text.strip() == ""


def test_encrypted_fixture_cannot_be_opened_without_a_password() -> None:
    assert _generated_dir is not None
    pdf_path = _generated_dir / "generic_table_encrypted.pdf"
    with pytest.raises(Exception), pdfplumber.open(pdf_path) as pdf:  # noqa: B017
        pdf.pages[0].extract_text()


def test_empty_fixture_is_a_single_blank_page() -> None:
    doc, _golden = _build("empty")
    assert doc.page_count == 1
    assert doc.full_text.strip() == ""


def test_goldens_are_json_serializable_and_stable_across_two_runs(tmp_path: Path) -> None:
    for variant in generic_table_builder.variants:
        first = generic_table_builder.build(tmp_path / "run1", variant=variant, seed=0)
        second = generic_table_builder.build(tmp_path / "run2", variant=variant, seed=0)
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_committed_pdfs_match_fresh_regeneration() -> None:
    """The committed PDFs under `tests/fixtures/generated/generic_table/` must be exactly what
    the (deterministic) generator produces today — this is what catches drift between the
    builder and what was last committed, since the suite itself never rewrites the committed
    files (see `_fixtures_generated`)."""
    assert _generated_dir is not None
    for variant in generic_table_builder.variants:
        name = f"generic_table_{variant}"
        committed = (_COMMITTED_DIR / f"{name}.pdf").read_bytes()
        fresh = (_generated_dir / f"{name}.pdf").read_bytes()
        assert committed == fresh, f"{name}.pdf is stale — regenerate committed fixtures"
        # `.build()` itself only returns the golden (it is `generate_fixtures.py` that writes it
        # to a `.json` file next to the PDF), so compare the freshly-built golden dict directly.
        committed_json = json.loads((_COMMITTED_DIR / f"{name}.json").read_text())
        fresh_golden = generic_table_builder.build(_generated_dir, variant=variant, seed=0)
        assert committed_json == json.loads(json.dumps(fresh_golden, default=str)), (
            f"{name}.json is stale — regenerate committed fixtures"
        )
