from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.api.services import gateway
from personal_finance_analyzer.db.models import Category, Subcategory, Transaction


def test_get_taxonomy_includes_others_uncategorized(client: TestClient) -> None:
    response = client.get("/api/taxonomy")
    assert response.status_code == 200
    categories = response.json()
    others = next(c for c in categories if c["key"] == "others")
    assert any(s["key"] == "uncategorized" for s in others["subcategories"])


def test_approve_subcategory_calls_gateway(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    category = session.execute(
        select(Category).where(Category.key == "online_shopping")
    ).scalar_one()
    sub = Subcategory(
        category_id=category.id,
        key="temu",
        label="Temu",
        is_dynamic=True,
        status="pending_approval",
    )
    session.add(sub)
    session.commit()

    calls: list[int] = []
    monkeypatch.setattr(
        gateway, "approve_subcategory", lambda db_session, sub_id: calls.append(sub_id)
    )

    response = client.post(f"/api/taxonomy/subcategories/{sub.id}/approve")
    assert response.status_code == 200
    assert calls == [sub.id]


def test_merge_endpoint_remaps_a_hand_edited_transaction_instead_of_resetting_it(
    client: TestClient, session: Session
) -> None:
    from tests.api.seed import add_txn, base

    category = session.execute(
        select(Category).where(Category.key == "online_shopping")
    ).scalar_one()
    source = Subcategory(
        category_id=category.id,
        key="teh_store",
        label="Teh Store",
        is_dynamic=True,
        status="pending_approval",
    )
    target = Subcategory(
        category_id=category.id,
        key="the_store",
        label="The Store",
        is_dynamic=True,
        status="active",
    )
    session.add_all([source, target])
    session.flush()
    seed = base(session)
    txn = add_txn(session, seed, 1, merchant_key="teh store")
    session.commit()

    edited = client.patch(
        f"/api/transactions/{txn.id}",
        json={"category_key": "online_shopping", "subcategory_key": "teh_store"},
    )
    assert edited.status_code == 200

    merged = client.post(
        f"/api/taxonomy/subcategories/{source.id}/merge", json={"into_id": target.id}
    )
    assert merged.status_code == 200

    session.expire_all()
    row = session.get(Transaction, txn.id)
    assert row is not None
    assert row.subcategory_id == target.id and row.category_id == category.id
    assert row.classified_by == "user" and row.needs_review is False
    shown = client.get("/api/transactions").json()["items"]
    assert [(t["category_key"], t["subcategory_key"]) for t in shown] == [
        ("online_shopping", "the_store")
    ]
