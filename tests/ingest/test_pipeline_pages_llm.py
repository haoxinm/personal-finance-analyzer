"""Page selection and the LLM parser through the pipeline (network-free)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.errors import ParserError, PfaError
from personal_finance_analyzer.db.models import Issuer, Statement, Transaction, User
from personal_finance_analyzer.ingest import pipeline
from tests.fixtures.gen.base import render_lines_pdf
from tests.llm_fakes import install


def _pdf(path: Path) -> None:
    render_lines_pdf(
        path,
        [
            ["Fixture Bank", "Account summary page with enough text to count as a text layer " * 2],
            ["Transactions " * 6, "01/05 COFFEE SHOP 12.34", "01/09 BOOKSTORE 29.99"],
            ["Legal boilerplate page about interest rates and disclosures " * 3],
        ],
    )


def _setup(session: Session, tmp_path: Path) -> tuple[int, int]:
    user = User(name="alice")
    issuer = Issuer(name="Test Bank", slug="test-bank")
    session.add_all([user, issuer])
    session.flush()
    pdf = tmp_path / "s.pdf"
    _pdf(pdf)
    proposal = pipeline.propose_import(session, pdf, user_id=user.id, original_name="s.pdf")
    return proposal.statement_id, issuer.id


def _llm_env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("PFA_LLM__ENABLED", "true")
    monkeypatch.setenv("PFA_LLM__BASE_URL", "https://api.example.com")
    monkeypatch.setenv("PFA_LLM__MODEL", "m")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


_REPLY = json.dumps(
    {
        "account_type": "credit",
        "account_last4": "1234",
        "period_start": "2026-01-01",
        "period_end": "2026-01-31",
        "opening_balance": "0.00",
        "closing_balance": "42.33",
        "transactions": [
            {
                "posted_date": "01/05",
                "description": "COFFEE SHOP",
                "amount": "12.34",
                "direction": "outflow",
            },
            {
                "posted_date": "2026-01-09",
                "description": "BOOKSTORE",
                "amount": "29.99",
                "direction": "outflow",
            },
        ],
    }
)


def test_llm_parse_end_to_end_with_pages(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch)
    fake = install(monkeypatch).queue(_REPLY)
    statement_id, issuer_id = _setup(session, tmp_path)

    result = pipeline.confirm_import(
        session,
        statement_id,
        issuer_id=issuer_id,
        parser_id="llm",
        layout_spec_id=None,
        remember=False,
        pages=[2, 2],
    )
    assert result.inserted == 2
    assert result.reconciliation_delta_minor == 0
    assert not result.layout_drift
    statement = session.get(Statement, statement_id)
    assert statement is not None
    assert statement.selected_pages == "[2]"
    assert statement.parser_id == "llm" and statement.parser_version.startswith("llm-")
    sent = fake.last_user_text
    assert "=== PAGE 2 ===" in sent and "=== PAGE 1 ===" not in sent and "Legal" not in sent
    rows = session.query(Transaction).order_by(Transaction.id).all()
    assert [r.amount_minor for r in rows] == [1234, 2999]
    assert rows[0].posted_date == "2026-01-05"


def test_invalid_pages_rejected(session: Session, tmp_path: Path) -> None:
    statement_id, issuer_id = _setup(session, tmp_path)
    for bad in ([], [9], [0]):
        with pytest.raises(PfaError):
            pipeline.confirm_import(
                session,
                statement_id,
                issuer_id=issuer_id,
                parser_id="generic_table",
                layout_spec_id=None,
                remember=False,
                pages=bad,
            )


def test_llm_failure_marks_statement_error(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch)
    install(monkeypatch).queue((500, {"error": "x"}))
    statement_id, issuer_id = _setup(session, tmp_path)
    with pytest.raises(ParserError):
        pipeline.confirm_import(
            session,
            statement_id,
            issuer_id=issuer_id,
            parser_id="llm",
            layout_spec_id=None,
            remember=False,
        )
    statement = session.get(Statement, statement_id)
    assert statement is not None
    assert statement.status == "error" and "LLM" in (statement.error_detail or "")


def test_llm_not_configured_is_actionable(session: Session, tmp_path: Path) -> None:
    statement_id, issuer_id = _setup(session, tmp_path)
    with pytest.raises(ParserError, match="Settings"):
        pipeline.confirm_import(
            session,
            statement_id,
            issuer_id=issuer_id,
            parser_id="llm",
            layout_spec_id=None,
            remember=False,
        )


def test_saved_selection_applies_on_reparse(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch)
    fake = install(monkeypatch).queue(_REPLY, _REPLY, _REPLY)
    statement_id, issuer_id = _setup(session, tmp_path)
    pipeline.confirm_import(
        session,
        statement_id,
        issuer_id=issuer_id,
        parser_id="llm",
        layout_spec_id=None,
        remember=False,
        pages=[2, 3],
    )
    pipeline.reparse(session, statement_id, parser_id="llm", layout_spec_id=None)
    assert "=== PAGE 1 ===" not in fake.last_user_text and "=== PAGE 3 ===" in fake.last_user_text
    pipeline.reparse(session, statement_id, parser_id="llm", layout_spec_id=None, pages=[2])
    statement = session.get(Statement, statement_id)
    assert statement is not None and statement.selected_pages == "[2]"


def test_propose_default_extractor_llm(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _llm_env(monkeypatch, PFA_INGEST__DEFAULT_EXTRACTOR="llm")
    user = User(name="bob")
    session.add(user)
    session.flush()
    pdf = tmp_path / "s.pdf"
    _pdf(pdf)
    proposal = pipeline.propose_import(session, pdf, user_id=user.id, original_name="s.pdf")
    assert proposal.parser_id == "llm"
    assert proposal.confident is False
    assert not proposal.detect_score


def test_propose_default_extractor_llm_needs_configured_llm(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PFA_INGEST__DEFAULT_EXTRACTOR", "llm")
    user = User(name="bob")
    session.add(user)
    session.flush()
    pdf = tmp_path / "s.pdf"
    _pdf(pdf)
    proposal = pipeline.propose_import(session, pdf, user_id=user.id, original_name="s.pdf")
    assert proposal.parser_id != "llm"
