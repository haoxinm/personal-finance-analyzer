"""Spec expressiveness check (P1-Z, §2027-2040): a layout spec hand-written for Layout B, run
through `SpecParser` (P1-H), reproduces P1-B2's golden exactly.

``tests/ingest/specs/layout_b.yaml`` is a hand-written spec for Layout B's column bands (read from
the real generated PDF's word coordinates), sections, sign convention, account mask and
foreign-currency continuation lines. It is run against the same generated PDFs `test_layout_b.py`
uses, and compared field by field against the same checked-in goldens — proving the declarative
format is expressive enough to reproduce a hand-written parser's output exactly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from personal_finance_analyzer.core.types import ExtractedDoc, PageText, Word
from personal_finance_analyzer.ingest.layout_spec import load_spec

_GENERATED_DIR = (
    Path(__file__).resolve().parent.parent / "fixtures" / "generated" / "layout_b_credit"
)
_GOLDEN_DIR = Path(__file__).resolve().parent / "golden" / "layout_b_credit"
_SPEC_PATH = Path(__file__).resolve().parent / "specs" / "layout_b.yaml"

_GOLDEN_VARIANTS = ("normal", "multiline", "fx", "refund_and_payment")


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


@pytest.mark.parametrize("variant", _GOLDEN_VARIANTS)
def test_spec_reproduces_layout_b_golden_exactly(variant: str) -> None:
    """§2c/P1-Z acceptance: a hand-written spec run through `SpecParser` reproduces P1-B2's
    golden exactly, on every non-error fixture variant."""
    loaded = load_spec(_SPEC_PATH.read_text(), source="user_authored")
    doc = _extract_doc(_pdf_path(variant))
    statement = loaded.parser.parse(doc)
    assert _statement_to_dict(statement) == _golden(variant)
