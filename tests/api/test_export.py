"""`GET /api/export` must exclude `accounts.mask` (plan's explicit data-leak regression test)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import (
    Account,
    Issuer,
    Statement,
    Transaction,
    User,
    utcnow_iso,
)


def _seed(session: Session) -> None:
    user = User(name="Alex", is_default=True)
    issuer = Issuer(name="Chase", slug="chase", match_terms="[]")
    session.add_all([user, issuer])
    session.flush()
    account = Account(
        user_id=user.id,
        issuer_id=issuer.id,
        account_type="credit",
        display_name="Card",
        mask="4242",
    )
    session.add(account)
    session.flush()
    statement = Statement(
        account_id=account.id,
        file_sha256="c" * 64,
        original_name="s.pdf",
        status="parsed",
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.flush()
    session.add(
        Transaction(
            statement_id=statement.id,
            account_id=account.id,
            user_id=user.id,
            posted_date="2026-01-05",
            description_raw="TRADER JOES #123",
            description_clean="Trader Joes",
            merchant_key="trader joes",
            amount_minor=1250,
            currency="USD",
            kind="purchase",
            is_spend=True,
            dedupe_hash="hash1",
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
    )
    session.commit()


def test_export_csv_excludes_mask(client: TestClient, session: Session) -> None:
    _seed(session)
    response = client.get("/api/export", params={"format": "csv"})
    assert response.status_code == 200
    assert "4242" not in response.text
    assert "mask" not in response.text.lower()
    assert "Trader Joes" in response.text


def test_export_json_excludes_mask(client: TestClient, session: Session) -> None:
    _seed(session)
    response = client.get("/api/export", params={"format": "json"})
    assert response.status_code == 200
    assert "4242" not in response.text
    body = response.json()
    assert len(body) == 1
    assert "mask" not in body[0]
