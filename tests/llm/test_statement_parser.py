"""The LLM statement parser (network-free: `tests/llm_fakes.py`)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.core.types import ExtractedDoc, PageText, StatementParser
from personal_finance_analyzer.llm import statement_parser as sp
from tests.llm_fakes import FakeLLM, install, remote_cfg


def _doc(*texts: str) -> ExtractedDoc:
    pages = tuple(
        PageText(page_number=i, text=t, words=(), char_count=len(t))
        for i, t in enumerate(texts, start=1)
    )
    return ExtractedDoc(file_sha256="s", page_count=len(pages), pages=pages, has_text_layer=True)


def _txn(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "posted_date": "2026-01-05",
        "transaction_date": None,
        "description": "COFFEE SHOP",
        "amount": "12.34",
        "direction": "outflow",
    }
    base.update(kw)
    return base


def _reply(transactions: list[dict[str, object]], **meta: object) -> str:
    body: dict[str, object] = {
        "account_type": "credit",
        "currency": "USD",
        "account_last4": "5678",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "transactions": transactions,
    }
    body.update(meta)
    return json.dumps(body)


# ---- redaction -------------------------------------------------------------------------------


def test_redacts_card_numbers_keeping_last4() -> None:
    out = sp.redact("Card 4111 1111 1111 1111 and 4111-1111-1111-1234 and 123456789012")
    assert "4111" not in out
    assert "XXXX1111" in out and "XXXX1234" in out and "XXXX9012" in out


def test_redacts_emails_and_pii_terms_case_insensitively_whole_word() -> None:
    out = sp.redact("Jane.Doe@example.com JANE DOE janet", ["jane doe", "Jane"])
    assert "[email]" in out
    assert "@" not in out
    assert out.count("[NAME]") == 1  # "janet" untouched; "JANE DOE" matched as one term
    assert "janet" in out


@pytest.mark.parametrize(
    "written", ["(312) 555-0100", "312.555.0100", "3125550100", "+1 312-555-0100", "1-312-555-0100"]
)
def test_phone_entry_is_fully_removed_not_masked_to_last4(written: str) -> None:
    out = sp.redact(f"Customer service {written} open 24h", ["(312)555-0100"])
    assert out == "Customer service [PHONE] open 24h"
    assert "0100" not in out and "XXXX" not in out


def test_account_entry_removed_before_generic_masking() -> None:
    out = sp.redact(
        "Acct 9876 5432 1098 7654 and card 4111 1111 1111 1234", ["9876-5432-1098-7654"]
    )
    assert "7654" not in out and "[NUMBER]" in out
    assert "XXXX1234" in out  # other numbers still get the generic last-4 mask


def test_count_redactions_counts_user_entries_only() -> None:
    doc = _doc("Jane Doe 312-555-0100 jane@example.com", "DOE, JANE")
    assert sp.count_redactions(doc, ["Jane Doe", "(312) 555-0100"]) == 3
    assert sp.count_redactions(doc, []) == 0


def test_redaction_keeps_amounts_and_dates() -> None:
    text = "2026-01-15 01/05/2026 COFFEE 1,234.56 12345.67 45.10 -500.00 Ref 20260115"
    out = sp.redact(text)
    for kept in ("2026-01-15", "01/05/2026", "1,234.56", "12345.67", "45.10", "-500.00"):
        assert kept in out
    assert "XXXX0115" in out  # a bare 8-digit run is treated as an account-like number


# ---- request building ------------------------------------------------------------------------


def test_build_requests_single_request_with_page_markers() -> None:
    reqs = sp.build_requests(_doc("page one", "page two"), remote_cfg())
    assert len(reqs) == 1
    assert "=== PAGE 1 ===\npage one" in reqs[0]
    assert "=== PAGE 2 ===\npage two" in reqs[0]


def test_build_requests_chunks_with_context_header() -> None:
    doc = _doc("HEADER " + "h" * 3000, "two", "three")
    reqs = sp.build_requests(doc, remote_cfg(parse_pages_per_request=1))
    assert len(reqs) == 3
    assert "context only" in reqs[1] and "do not extract" in reqs[1]
    assert "=== PAGE 2 ===" in reqs[1] and "=== PAGE 1 ===" not in reqs[1]
    assert "context only" not in reqs[0]
    ctx = reqs[1].split("[end of context]")[0]
    assert ctx.count("h") <= sp.CONTEXT_HEADER_CHARS


def test_build_requests_redacts() -> None:
    reqs = sp.build_requests(
        _doc("Card 4111 1111 1111 1111 Bob Smith"), remote_cfg(), ["Bob Smith"]
    )
    assert "XXXX1111" in reqs[0] and "Bob Smith" not in reqs[0] and "4111 1111" not in reqs[0]
    assert sp.preview(_doc("x"), remote_cfg()) == sp.build_requests(_doc("x"), remote_cfg())


# ---- parsing ---------------------------------------------------------------------------------


def test_parse_signs_dates_and_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch)
    fake.queue(
        _reply(
            [
                _txn(),
                _txn(
                    description="PAYMENT THANK YOU",
                    amount="500.00",
                    direction="inflow",
                    kind_hint="payment",
                    transaction_date="2026-01-02",
                ),
                _txn(
                    description="REFUND", amount="$1,020.50", direction="inflow", kind_hint="bogus"
                ),
            ],
            opening_balance="100.00",
            closing_balance="-387.16",
            stated_total="12.34",
            account_last4="xx 5678",
        )
    )
    parser = sp.LLMStatementParser(remote_cfg())
    assert isinstance(parser, StatementParser)
    parsed = parser.parse(_doc("statement text"))
    assert [t.amount_minor for t in parsed.transactions] == [1234, -50000, -102050]
    assert parsed.transactions[1].kind_hint == "payment"
    assert parsed.transactions[2].kind_hint is None
    assert parsed.transactions[1].transaction_date == date(2026, 1, 2)
    assert parsed.account_hint.mask == "5678"
    assert parsed.account_hint.account_type == "credit"
    assert parsed.account_hint.currency == "USD"
    assert parsed.period_start == date(2026, 1, 1) and parsed.period_end == date(2026, 1, 31)
    assert parsed.opening_balance_minor == 10000 and parsed.closing_balance_minor == -38716
    assert parsed.stated_total_minor == 1234
    assert parsed.warnings == ()
    assert all(t.currency == "USD" for t in parsed.transactions)
    # the structured-output schema and system prompt are what we sent
    assert "outflow" in json.dumps(fake.requests[0])


def test_parse_infers_year_from_period_and_handles_year_wrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch).queue(
        _reply(
            [
                _txn(posted_date="12-28"),
                _txn(posted_date="01/03"),
                _txn(posted_date="01/05/2026"),
            ],
            period_start="2025-12-15",
            period_end="2026-01-14",
        )
    )
    parsed = sp.LLMStatementParser(remote_cfg()).parse(_doc("t"))
    assert [t.posted_date for t in parsed.transactions] == [
        date(2025, 12, 28),
        date(2026, 1, 3),
        date(2026, 1, 5),
    ]


def test_parse_skips_invalid_rows_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch).queue(
        _reply(
            [
                _txn(),
                _txn(amount="abc"),
                _txn(direction="sideways"),
                _txn(posted_date="not a date"),
                _txn(description="   "),
                "garbage",
            ]
        )
    )
    parsed = sp.LLMStatementParser(remote_cfg()).parse(_doc("t"))
    assert len(parsed.transactions) == 1
    assert len(parsed.warnings) == 1 and "5 row(s)" in parsed.warnings[0]


def test_parse_accepts_fenced_json_and_default_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch).queue("```json\n" + _reply([_txn()], currency=None) + "\n```")
    parsed = sp.LLMStatementParser(remote_cfg()).parse(_doc("t"))
    assert parsed.transactions[0].currency == "USD"  # [ingest] default_currency


def test_parse_merges_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch)
    fake.queue(
        _reply([_txn()], account_last4=None, closing_balance=None),
        _reply(
            [_txn(description="TWO")],
            account_last4="9999",
            closing_balance="50.00",
            period_end=None,
        ),
    )
    parsed = sp.LLMStatementParser(remote_cfg(parse_pages_per_request=1)).parse(_doc("a", "b"))
    assert len(fake.requests) == 2
    assert [t.description for t in parsed.transactions] == ["COFFEE SHOP", "TWO"]
    assert parsed.account_hint.mask == "9999"
    assert parsed.closing_balance_minor == 5000
    assert parsed.period_end == date(2026, 1, 31)


def test_parse_sends_redacted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch).queue(_reply([_txn()]))
    sp.LLMStatementParser(remote_cfg(), ["Alice Example"]).parse(
        _doc("Alice Example 4111 1111 1111 1111 a@b.co 12.34")
    )
    sent = fake.last_user_text
    assert "Alice Example" not in sent and "a@b.co" not in sent and "XXXX1111" in sent
    assert "12.34" in sent


def test_not_configured_raises_actionable_parser_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch)
    with pytest.raises(ParserError, match="Settings"):
        sp.LLMStatementParser(remote_cfg(enabled=False)).parse(_doc("t"))
    assert fake.requests == []


def test_llm_error_becomes_parser_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch).queue((500, {"error": "boom"}))
    with pytest.raises(ParserError, match="LLM"):
        sp.LLMStatementParser(remote_cfg()).parse(_doc("t"))


def test_unparseable_reply_and_zero_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = install(monkeypatch)
    fake.queue("no json here at all")
    with pytest.raises(ParserError):
        sp.LLMStatementParser(remote_cfg()).parse(_doc("t"))
    fake.queue(_reply([]))
    empty = sp.LLMStatementParser(remote_cfg()).parse(_doc("t"))
    assert empty.transactions == ()
    assert any("no transactions" in w for w in empty.warnings)


def test_parser_identity() -> None:
    parser = sp.LLMStatementParser(remote_cfg())
    assert parser.id == "llm" and parser.detect(_doc("t")) == 0.0
    assert sp.PROMPT_VERSION in parser.version


def test_fake_llm_type() -> None:  # keeps the import used if tests above change
    assert FakeLLM().requests == []
