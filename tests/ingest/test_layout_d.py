"""Tests for Layout D (`layout_d_bank`), §2c — bank statement, debit/credit columns (A26).

Golden fixtures live in `tests/fixtures/generated/layout_d_bank/` (PDFs, committed per
`.gitignore`'s exception) with a frozen reference copy of their expected `ParsedStatement` under
`tests/ingest/golden/layout_d_bank/`. `tests/fixtures/gen/layout_d.py` is the `LayoutBuilder`
that produced both (the directory name matches `builder.layout_id` / the parser's `id`, per
`tests/generate_fixtures.py`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import (
    ExtractedDoc,
    PageText,
    ParsedStatement,
    RawTransaction,
    Word,
)
from personal_finance_analyzer.ingest.parsers.layout_d_bank import PARSER
from tests.fixtures.gen.base import format_row, render_lines_pdf
from tests.fixtures.gen.layout_d import _HEADER, _WIDTHS, BUILDER, _data_row

_GENERATED_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "generated" / "layout_d_bank"
_GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "layout_d_bank"

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


def _load_golden(variant: str) -> dict[str, Any]:
    return json.loads((_GOLDEN_DIR / f"layout_d_bank_{variant}.json").read_text())  # type: ignore[no-any-return]


def _txn_to_dict(txn: RawTransaction) -> dict[str, Any]:
    return {
        "posted_date": txn.posted_date.isoformat(),
        "transaction_date": txn.transaction_date.isoformat() if txn.transaction_date else None,
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


def _statement_to_dict(statement: ParsedStatement) -> dict[str, Any]:
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
        "transactions": [_txn_to_dict(t) for t in statement.transactions],
    }


# ------------------------------------------------------------------------------------------------
# Golden match
# ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("variant", _GOLDEN_VARIANTS)
def test_golden_match(variant: str) -> None:
    doc = _extract_doc(_GENERATED_DIR / f"layout_d_bank_{variant}.pdf")
    statement = PARSER.parse(doc)
    assert _statement_to_dict(statement) == _load_golden(variant)


@pytest.mark.parametrize("variant", _GOLDEN_VARIANTS)
def test_golden_stable_across_two_generator_runs(tmp_path: Path, variant: str) -> None:
    first = BUILDER.build(tmp_path / "run1", variant=variant, seed=0)
    second = BUILDER.build(tmp_path / "run2", variant=variant, seed=0)
    assert first == second
    assert first == _load_golden(variant)


def test_committed_pdfs_match_fresh_regeneration(tmp_path: Path) -> None:
    """The committed PDFs under `tests/fixtures/generated/layout_d_bank/` must be exactly what
    the (deterministic) generator produces today — this is what catches drift between the
    builder and what was last committed, since the suite itself never rewrites the committed
    files."""
    for variant in BUILDER.variants:
        name = f"layout_d_bank_{variant}"
        BUILDER.build(tmp_path, variant=variant, seed=0)
        committed = (_GENERATED_DIR / f"{name}.pdf").read_bytes()
        fresh = (tmp_path / f"{name}.pdf").read_bytes()
        assert committed == fresh, f"{name}.pdf is stale — regenerate committed fixtures"


@pytest.mark.parametrize("variant", _ERROR_VARIANTS)
def test_error_variants_raise_parser_error(variant: str) -> None:
    doc = _extract_doc(_GENERATED_DIR / f"layout_d_bank_{variant}.pdf")
    with pytest.raises(ParserError):
        PARSER.parse(doc)


# ------------------------------------------------------------------------------------------------
# detect()
# ------------------------------------------------------------------------------------------------


def test_detect_scores_normal_fixture_above_threshold() -> None:
    doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_normal.pdf")
    assert PARSER.detect(doc) > 0.5
    assert PARSER.detect(doc) == pytest.approx(0.9)


def test_detect_returns_zero_on_empty_doc_without_raising() -> None:
    empty = ExtractedDoc(file_sha256="e", page_count=0, pages=(), has_text_layer=False)
    assert PARSER.detect(empty) == 0.0


def test_detect_scores_below_half_on_a_different_layout(tmp_path: Path) -> None:
    # A plain credit-card-style statement: no debit/credit column pair at all.
    lines = [
        "Some Card Issuer",
        "Transactions",
        format_row(["Transaction Date", "Description", "Amount"], (18, 40, 12)),
        format_row(["03/05/2026", "COFFEE SHOP SEATTLE WA", "12.34"], (18, 40, 12)),
    ]
    pdf_path = tmp_path / "other_layout.pdf"
    render_lines_pdf(pdf_path, [lines])
    doc = _extract_doc(pdf_path)
    assert PARSER.detect(doc) < 0.5


# ------------------------------------------------------------------------------------------------
# Sign test (I5) — purchase positive, refund negative, deposit negative, payment (transfer) positive
# column-wise but excluded from spend by kind_hint.
# ------------------------------------------------------------------------------------------------


def test_sign_purchase_positive_refund_and_deposit_negative() -> None:
    doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_normal.pdf")
    statement = PARSER.parse(doc)
    by_desc = {t.description: t for t in statement.transactions}
    assert by_desc["ATM WITHDRAWAL MAIN ST BRANCH"].amount_minor > 0  # purchase: outflow
    assert by_desc["DIRECT DEPOSIT PAYROLL ACME CORP"].amount_minor < 0  # deposit: inflow

    refund_doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_refund_and_payment.pdf")
    refund_statement = PARSER.parse(refund_doc)
    by_desc2 = {t.description: t for t in refund_statement.transactions}
    assert by_desc2["MERCHANT REFUND WIDGET CO"].amount_minor < 0
    assert by_desc2["MERCHANT REFUND WIDGET CO"].kind_hint == "refund"
    assert by_desc2["AUTOPAY CRD PMT VISA CARD"].amount_minor > 0  # debit column, still positive
    assert by_desc2["AUTOPAY CRD PMT VISA CARD"].kind_hint == "transfer"  # but excluded from spend


# ------------------------------------------------------------------------------------------------
# Reconciliation (A25)
# ------------------------------------------------------------------------------------------------


def test_balance_equation_holds_on_normal_fixture() -> None:
    doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_normal.pdf")
    statement = PARSER.parse(doc)
    total = sum(t.amount_minor for t in statement.transactions)
    assert statement.opening_balance_minor is not None
    assert statement.closing_balance_minor is not None
    # checking/savings: opening - closing == sum(amount_minor) (I5, A25)
    assert statement.opening_balance_minor - statement.closing_balance_minor == total
    assert statement.warnings == ()


def test_running_balance_break_is_a_warning_not_a_raise(tmp_path: Path) -> None:
    """A fixture with one row's printed balance inconsistent with its amount must still parse,
    recording a `shape_warning` (A18) rather than raising — this is what proves the running-
    balance check actually catches a dropped/mis-signed row, not just totals-of-magnitudes."""
    lines = [
        "Lakeshore Community Bank",
        "Transaction Detail",
        "Checking Account Statement",
        "March 1, 2026 through March 31, 2026",
        "Beginning Balance $1,000.00",
        "Ending Balance $900.00",
        "",
        format_row(_HEADER, _WIDTHS),
        "Electronic Withdrawals",
        # Correct balance would be 900.00 (1000.00 - 100.00), but the fixture prints a wrong one.
        _data_row("03/05", "UTILITY BILL", debit=10_000, credit=None, balance=80_000),
    ]
    pdf_path = tmp_path / "layout_d_break.pdf"
    render_lines_pdf(pdf_path, [lines])
    doc = _extract_doc(pdf_path)
    statement = PARSER.parse(doc)
    assert len(statement.transactions) == 1
    assert len(statement.warnings) == 1
    assert "running balance break" in statement.warnings[0]


# ------------------------------------------------------------------------------------------------
# Layout-D-specific must-pass tests
# ------------------------------------------------------------------------------------------------


def test_debit_and_credit_same_day_take_opposite_signs(tmp_path: Path) -> None:
    lines = [
        "Lakeshore Community Bank",
        "Transaction Detail",
        "Checking Account Statement",
        "March 1, 2026 through March 31, 2026",
        "Beginning Balance $500.00",
        "Ending Balance $520.00",
        "",
        format_row(_HEADER, _WIDTHS),
        "Deposits and Additions",
        _data_row("03/05", "REFUND FROM STORE", debit=None, credit=5_000, balance=55_000),
        "Electronic Withdrawals",
        _data_row("03/05", "PHONE BILL", debit=3_000, credit=None, balance=52_000),
    ]
    pdf_path = tmp_path / "layout_d_same_day.pdf"
    render_lines_pdf(pdf_path, [lines])
    doc = _extract_doc(pdf_path)
    statement = PARSER.parse(doc)
    assert len(statement.transactions) == 2
    credit_txn, debit_txn = statement.transactions
    assert credit_txn.posted_date == debit_txn.posted_date
    assert credit_txn.amount_minor < 0
    assert debit_txn.amount_minor > 0


def test_every_row_satisfies_running_balance_on_normal_fixture() -> None:
    doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_normal.pdf")
    statement = PARSER.parse(doc)
    assert statement.opening_balance_minor is not None
    balance = statement.opening_balance_minor
    for txn in statement.transactions:
        balance -= txn.amount_minor
    assert balance == statement.closing_balance_minor


def test_card_payment_row_gets_transfer_kind_hint() -> None:
    doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_refund_and_payment.pdf")
    statement = PARSER.parse(doc)
    payment = next(t for t in statement.transactions if "AUTOPAY" in t.description)
    assert payment.kind_hint == "transfer"


def test_bills_with_payment_words_are_not_hinted_as_transfers(tmp_path: Path) -> None:
    """Regression: a bare PAYMENT/PMT/EPAY is rent, a utility or a loan — real spend — so only
    card-specific wording gets the `transfer` hint, in whichever section the row printed."""
    lines = [
        "Lakeshore Community Bank",
        "Transaction Detail",
        "Checking Account Statement",
        "March 1, 2026 through March 31, 2026",
        "Beginning Balance $5,000.00",
        "Ending Balance $2,700.00",
        "",
        format_row(_HEADER, _WIDTHS),
        "Electronic Withdrawals",
        _data_row("03/02", "RENT PAYMENT UNIT 4B", debit=150_000, credit=None, balance=350_000),
        _data_row("03/03", "CITY WATER EPAY", debit=6_000, credit=None, balance=344_000),
        _data_row("03/04", "AUTO LOAN PMT ACME", debit=34_000, credit=None, balance=310_000),
        _data_row("03/05", "ACME CRD PMT VISA", debit=40_000, credit=None, balance=270_000),
    ]
    pdf_path = tmp_path / "layout_d_bills.pdf"
    render_lines_pdf(pdf_path, [lines])
    statement = PARSER.parse(_extract_doc(pdf_path))
    hints = {t.description: t.kind_hint for t in statement.transactions}
    assert hints == {
        "RENT PAYMENT UNIT 4B": "purchase",
        "CITY WATER EPAY": "purchase",
        "AUTO LOAN PMT ACME": "purchase",
        "ACME CRD PMT VISA": "transfer",
    }


def test_card_payments_with_issuer_wording_are_hinted_as_transfers(tmp_path: Path) -> None:
    """Regression (B1): a checking statement words card payments many ways; none may fall through
    as a `purchase`, while a same-named mortgage or loan payment stays spend."""
    rows = [
        ("CHASE CREDIT CRD AUTOPAY", "transfer"),
        ("Payment To Chase Card Ending IN x1234", "transfer"),
        ("DISCOVER E-PAYMENT", "transfer"),
        ("AMEX EPAYMENT ACH PMT", "transfer"),
        ("CHASE MORTGAGE PAYMENT", "purchase"),
        ("CITI LOAN PMT", "purchase"),
        ("VENMO PAYMENT", "purchase"),
    ]
    lines = [
        "Lakeshore Community Bank",
        "Transaction Detail",
        "Checking Account Statement",
        "March 1, 2026 through March 31, 2026",
        "Beginning Balance $5,000.00",
        "Ending Balance $4,300.00",
        "",
        format_row(_HEADER, _WIDTHS),
        "Electronic Withdrawals",
    ]
    balance = 500_000
    for i, (description, _) in enumerate(rows):
        balance -= 10_000
        lines.append(
            _data_row(f"03/{i + 2:02d}", description, debit=10_000, credit=None, balance=balance)
        )
    pdf_path = tmp_path / "layout_d_card_payments.pdf"
    render_lines_pdf(pdf_path, [lines])
    statement = PARSER.parse(_extract_doc(pdf_path))
    hints = {t.description: t.kind_hint for t in statement.transactions}
    assert hints == dict(rows)


def test_both_debit_and_credit_filled_raises_parser_error() -> None:
    doc = _extract_doc(_GENERATED_DIR / "layout_d_bank_malformed.pdf")
    with pytest.raises(ParserError, match="both the debit and credit column"):
        PARSER.parse(doc)
