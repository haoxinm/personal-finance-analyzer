"""A failing built-in parser is a warning, not a dead end: the staged PDF is kept so the same
statement can be confirmed again with the LLM parser (network-free)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.errors import ParserError
from personal_finance_analyzer.db.models import Statement, Transaction
from personal_finance_analyzer.ingest import pipeline
from tests.ingest.test_pipeline import _build_bank_pdf, _make_issuer, _make_user
from tests.llm_fakes import install

_LLM_REPLY = json.dumps(
    {
        "account_type": "checking",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "transactions": [
            {
                "posted_date": "2026-01-03",
                "description": "ATM WITHDRAWAL",
                "amount": "40.00",
                "direction": "outflow",
            }
        ],
    }
)


def _llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "m")
    # The case that used to delete the staged PDF on failure.
    monkeypatch.setenv("PFA_PRIVACY__STORE_PDF_COPIES", "false")


def _failing_generic_statement(session: Session, tmp_path: Path) -> tuple[int, int]:
    """A checking statement whose generic-parser run raises "value in both the debit and credit
    columns"."""
    pdf = tmp_path / "both.pdf"
    _build_bank_pdf(
        pdf,
        [
            ("01/02/2026", "PAYCHECK DEPOSIT", "", "300.00", "1,300.00"),
            ("01/03/2026", "ATM WITHDRAWAL", "40.00", "", "1,260.00"),
            ("01/04/2026", "MIXED ROW", "20.00", "5.00", "1,245.00"),
            ("01/25/2026", "ONLINE SHOPPING", "60.00", "", "1,185.00"),
        ],
        opening="1,000.00",
        closing="1,185.00",
    )
    user = _make_user(session)
    issuer = _make_issuer(session, "Retry Bank")
    proposal = pipeline.propose_import(session, pdf, user_id=user.id, original_name="both.pdf")
    return proposal.statement_id, issuer.id


def test_failed_builtin_keeps_pdf_and_llm_retry_succeeds(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch)
    install(monkeypatch).queue(_LLM_REPLY)
    statement_id, issuer_id = _failing_generic_statement(session, tmp_path)
    staged = pipeline._staged_pdf_path(session.get(Statement, statement_id).file_sha256)  # type: ignore[union-attr]

    with pytest.raises(ParserError, match="both the debit and credit"):
        pipeline.confirm_import(
            session,
            statement_id,
            issuer_id=issuer_id,
            parser_id="generic_table",
            layout_spec_id=None,
            remember=False,
        )
    statement = session.get(Statement, statement_id)
    assert statement is not None
    assert statement.status == "error"
    assert statement.error_detail is not None and "debit and credit" in statement.error_detail
    assert staged.exists()

    result = pipeline.confirm_import(
        session,
        statement_id,
        issuer_id=issuer_id,
        parser_id="llm",
        layout_spec_id=None,
        remember=False,
    )
    assert result.inserted == 1
    session.refresh(statement)
    assert statement.status == "parsed" and statement.error_detail is None
    assert statement.parser_id == "llm"
    assert session.query(Transaction).filter_by(statement_id=statement_id).count() == 1
    assert not staged.exists()  # success is terminal: the staged copy is cleaned up now


def test_llm_confirm_with_no_transactions_is_a_parsed_empty_statement(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch)
    install(monkeypatch).queue(json.dumps({"account_type": "credit", "transactions": []}))
    statement_id, issuer_id = _failing_generic_statement(session, tmp_path)

    result = pipeline.confirm_import(
        session,
        statement_id,
        issuer_id=issuer_id,
        parser_id="llm",
        layout_spec_id=None,
        remember=False,
    )
    assert result.inserted == 0
    statement = session.get(Statement, statement_id)
    assert statement is not None
    assert statement.status == "parsed" and statement.txn_count == 0


def test_llm_timeout_then_retry_with_larger_timeout_uses_fresh_config(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from personal_finance_analyzer.config import LLMConfig
    from personal_finance_analyzer.llm import client
    from tests.llm_fakes import wrap_text

    _llm_env(monkeypatch)
    monkeypatch.setenv("PFA_LLM__TIMEOUT_S", "5")
    seen: list[int] = []

    def fake_post(cfg: LLMConfig, body: dict[str, object]) -> tuple[int, object]:
        seen.append(cfg.timeout_s)
        if cfg.timeout_s < 60:
            raise httpx.ReadTimeout("slow")
        return 200, wrap_text(cfg.api_format, _LLM_REPLY)

    monkeypatch.setattr(client, "_post", fake_post)
    statement_id, issuer_id = _failing_generic_statement(session, tmp_path)

    with pytest.raises(ParserError, match=r"timed out after 5s.*Timeout in Settings"):
        pipeline.confirm_import(
            session,
            statement_id,
            issuer_id=issuer_id,
            parser_id="llm",
            layout_spec_id=None,
            remember=False,
        )
    statement = session.get(Statement, statement_id)
    assert statement is not None and statement.status == "error"

    # The user raises the timeout in Settings; no restart, same process.
    monkeypatch.setenv("PFA_LLM__TIMEOUT_S", "300")
    result = pipeline.confirm_import(
        session,
        statement_id,
        issuer_id=issuer_id,
        parser_id="llm",
        layout_spec_id=None,
        remember=False,
    )
    assert result.inserted == 1
    assert seen == [5, 300]


def test_reproposing_a_failed_statement_reopens_it(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from personal_finance_analyzer.llm import client

    _llm_env(monkeypatch)

    def timeout_post(cfg: object, body: object) -> tuple[int, object]:
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(client, "_post", timeout_post)
    statement_id, issuer_id = _failing_generic_statement(session, tmp_path)
    with pytest.raises(ParserError):
        pipeline.confirm_import(
            session,
            statement_id,
            issuer_id=issuer_id,
            parser_id="llm",
            layout_spec_id=None,
            remember=False,
        )
    assert session.get(Statement, statement_id).status == "error"  # type: ignore[union-attr]

    proposal = pipeline.propose_import(
        session, tmp_path / "both.pdf", user_id=1, original_name="both.pdf"
    )
    assert proposal.statement_id == statement_id
    assert proposal.status == "awaiting_extractor"
    assert proposal.confident is False
    statement = session.get(Statement, statement_id)
    assert statement is not None
    assert statement.status == "awaiting_extractor"
    assert statement.error_detail is not None and "timed out" in statement.error_detail
