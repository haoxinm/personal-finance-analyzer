"""`/api/classify/*`. `classify_transactions`/`preview_egress` are faked (§3.12a; `classify/
cascade.py` is P2-B, in a separate branch)."""

from __future__ import annotations

import time

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


def _make_unclassified_transaction(session: Session) -> int:
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
        file_sha256="e" * 64,
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


def test_run_classify_enqueues_job(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_unclassified_transaction(session)

    def fake_classify_transactions(session_factory, transaction_ids, *, cfg, group_id, progress_cb):
        progress_cb(1, 1, 0.01)
        return gateway.ClassifyResult(
            group_id=group_id, classified=1, needs_review=0, llm_requests=1, cost_usd=0.01
        )

    monkeypatch.setattr(gateway, "classify_transactions", fake_classify_transactions)

    response = client.post("/api/classify/run", json={"only_unclassified": True})
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] == "done":
            break
        time.sleep(0.05)
    assert job["status"] == "done"


def test_preview_classify_calls_egress_guard(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_unclassified_transaction(session)
    calls: list[list[int]] = []

    def fake_preview_egress(db_session: Session, transaction_ids: list[int]) -> str:
        calls.append(list(transaction_ids))
        return "id,description\n1,Trader Joes\n"

    monkeypatch.setattr(gateway, "preview_egress", fake_preview_egress)

    response = client.get("/api/classify/preview")
    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["description_clean"] == "Trader Joes"
    assert len(calls) == 1
