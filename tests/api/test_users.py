from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def test_create_and_list_users(client: TestClient) -> None:
    response = client.post("/api/users", json={"name": "Alex", "is_default": True})
    assert response.status_code == 201
    user = response.json()
    assert user["name"] == "Alex"
    assert user["is_default"] is True

    listing = client.get("/api/users")
    assert listing.status_code == 200
    assert any(u["id"] == user["id"] for u in listing.json())


def test_update_and_delete_user(client: TestClient) -> None:
    user = client.post("/api/users", json={"name": "Sam"}).json()

    updated = client.patch(f"/api/users/{user['id']}", json={"name": "Samantha"})
    assert updated.status_code == 200
    assert updated.json()["name"] == "Samantha"

    deleted = client.delete(f"/api/users/{user['id']}")
    assert deleted.status_code == 204

    missing = client.patch(f"/api/users/{user['id']}", json={"name": "Nope"})
    assert missing.status_code == 404


def test_only_one_default_user(client: TestClient) -> None:
    first = client.post("/api/users", json={"name": "First", "is_default": True}).json()
    second = client.post("/api/users", json={"name": "Second", "is_default": True}).json()

    users = {u["id"]: u for u in client.get("/api/users").json()}
    assert users[first["id"]]["is_default"] is False
    assert users[second["id"]]["is_default"] is True


def test_user_pii_terms_merges_settings_and_aliases(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_finance_analyzer.config import load_settings
    from personal_finance_analyzer.db.models import User
    from personal_finance_analyzer.db.pii_terms import user_pii_terms

    monkeypatch.setenv("PFA_PRIVACY__PII_TERMS", "Jane Doe,312-555-0100")
    user = User(name="Pat", pii_aliases='["jane doe", "Pat Q", 7]')
    session.add(user)
    session.flush()
    terms = user_pii_terms(session, load_settings(), user.id)
    assert terms == ("Jane Doe", "312-555-0100", "Pat Q")
    assert user_pii_terms(session, load_settings(), None) == ("Jane Doe", "312-555-0100")
