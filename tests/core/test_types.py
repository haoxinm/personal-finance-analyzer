from __future__ import annotations

from datetime import date

from personal_finance_analyzer.core.types import (
    AccountHint,
    ExtractedDoc,
    PageText,
    ParsedStatement,
    RawTransaction,
    StatementParser,
    Word,
)


def test_extracted_doc_full_text_joins_pages_with_form_feed() -> None:
    doc = ExtractedDoc(
        file_sha256="abc",
        page_count=2,
        pages=(
            PageText(page_number=1, text="page one", words=(), char_count=8),
            PageText(page_number=2, text="page two", words=(), char_count=8),
        ),
        has_text_layer=True,
    )
    assert doc.full_text == "page one\n\f\npage two"


def test_word_and_pagetext_are_frozen() -> None:
    word = Word(text="Trader", x0=1.0, x1=10.0, top=5.0, bottom=15.0)
    try:
        word.text = "changed"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("Word should be frozen")


class _MinimalStubParser:
    id = "stub_parser"
    version = "1.0"
    account_type = "credit"

    def detect(self, doc: ExtractedDoc) -> float:
        return 0.0

    def parse(self, doc: ExtractedDoc) -> ParsedStatement:
        return ParsedStatement(
            account_hint=AccountHint(account_type="credit", mask=None, currency="USD"),
            period_start=None,
            period_end=None,
            stated_total_minor=None,
            transactions=(),
        )


def test_runtime_checkable_protocol_accepts_minimal_stub() -> None:
    stub = _MinimalStubParser()
    assert isinstance(stub, StatementParser)


def test_raw_transaction_amount_sign_convention() -> None:
    purchase = RawTransaction(
        posted_date=date(2026, 1, 5),
        transaction_date=None,
        description="COFFEE SHOP",
        amount_minor=500,  # positive = outflow (I5)
        currency="USD",
    )
    refund = RawTransaction(
        posted_date=date(2026, 1, 6),
        transaction_date=None,
        description="REFUND",
        amount_minor=-500,  # negative = money returning
        currency="USD",
    )
    assert purchase.amount_minor > 0
    assert refund.amount_minor < 0
