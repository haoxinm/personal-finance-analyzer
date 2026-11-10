"""`/api/transactions/*`. A category/subcategory patch routes through
`classify.cascade.apply_user_correction` (faked here; P2-B lands separately, §3.12a)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services import gateway
from personal_finance_analyzer.db.models import (
    Account,
    Issuer,
    Statement,
    Transaction,
    User,
    utcnow_iso,
)


def _make_transaction(session: Session) -> int:
    user = User(name="Alex", is_default=True)
    issuer = Issuer(name="Chase", slug="chase", match_terms="[]")
    session.add_all([user, issuer])
    session.flush()
    account = Account(
        user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
    )
    session.add(account)
    session.flush()
    statement = Statement(
        account_id=account.id,
        file_sha256="b" * 64,
        original_name="s.pdf",
        status="parsed",
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.flush()
    txn = Transaction(
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
    session.add(txn)
    session.commit()
    return txn.id


def test_list_transactions(client: TestClient, session: Session) -> None:
    _make_transaction(session)
    response = client.get("/api/transactions")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["description_clean"] == "Trader Joes"
    assert "mask" not in body["items"][0]


def test_patch_notes_is_plain_crud(client: TestClient, session: Session) -> None:
    txn_id = _make_transaction(session)
    response = client.patch(f"/api/transactions/{txn_id}", json={"notes": "business expense"})
    assert response.status_code == 200
    assert response.json()["notes"] == "business expense"


def test_patch_category_calls_apply_user_correction(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    txn_id = _make_transaction(session)
    calls: list[dict[str, object]] = []

    def fake_apply_user_correction(
        db_session: Session,
        transaction_id: int,
        *,
        category_key: str,
        subcategory_key: str,
        kind: str | None,
        create_rule: bool,
    ) -> None:
        calls.append(
            {
                "transaction_id": transaction_id,
                "category_key": category_key,
                "subcategory_key": subcategory_key,
                "create_rule": create_rule,
            }
        )

    monkeypatch.setattr(gateway, "apply_user_correction", fake_apply_user_correction)

    response = client.patch(
        f"/api/transactions/{txn_id}",
        json={
            "category_key": "grocery",
            "subcategory_key": "grocery_stores",
            "create_rule": True,
        },
    )
    assert response.status_code == 200
    assert calls == [
        {
            "transaction_id": txn_id,
            "category_key": "grocery",
            "subcategory_key": "grocery_stores",
            "create_rule": True,
        }
    ]


def test_patch_requires_transaction_to_exist(client: TestClient) -> None:
    response = client.patch("/api/transactions/999999", json={"notes": "x"})
    assert response.status_code == 404
