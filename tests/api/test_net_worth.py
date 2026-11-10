from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import BalanceEntry


def _user(client: TestClient, name: str = "Alex") -> int:
    return int(client.post("/api/users", json={"name": name}).json()["id"])


def _body(user_id: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user_id": user_id,
        "category": "checking",
        "account_name": "Chase checking",
        "year": 2025,
        "month": 3,
        "balance_minor": 100000,
    }
    base.update(kw)
    return base


def test_upsert_create_then_update(client: TestClient, session: Session) -> None:
    uid = _user(client)
    first = client.post("/api/net-worth/entries", json=_body(uid))
    assert first.status_code == 201
    second = client.post("/api/net-worth/entries", json=_body(uid, balance_minor=5, note="x"))
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["balance_minor"] == 5 and second.json()["note"] == "x"
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(BalanceEntry)) == 1


def test_account_name_case_insensitive_reuse(client: TestClient) -> None:
    uid = _user(client)
    first = client.post("/api/net-worth/entries", json=_body(uid)).json()
    second = client.post(
        "/api/net-worth/entries", json=_body(uid, account_name="  chase   CHECKING ")
    )
    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    assert second.json()["account_name"] == "Chase checking"


def test_validation_errors(client: TestClient) -> None:
    uid = _user(client)
    for bad in (
        {"balance_minor": -1},
        {"month": 13},
        {"category": "cash"},
        {"account_name": ""},
        {"currency": "usd"},
    ):
        assert client.post("/api/net-worth/entries", json=_body(uid, **bad)).status_code == 422
    blank = client.post("/api/net-worth/entries", json=_body(uid, account_name="   "))
    assert blank.status_code == 422


def test_unknown_user_404(client: TestClient) -> None:
    assert client.post("/api/net-worth/entries", json=_body(9999)).status_code == 404


def test_list_filters(client: TestClient) -> None:
    a, b = _user(client, "A"), _user(client, "B")
    client.post("/api/net-worth/entries", json=_body(a))
    client.post("/api/net-worth/entries", json=_body(a, month=4, category="savings"))
    client.post("/api/net-worth/entries", json=_body(b, year=2024))
    allr = client.get("/api/net-worth/entries").json()
    assert len(allr) == 3
    assert [(e["year"], e["month"]) for e in allr] == [(2025, 4), (2025, 3), (2024, 3)]
    assert len(client.get("/api/net-worth/entries", params={"user_id": a}).json()) == 2
    assert len(client.get("/api/net-worth/entries", params={"category": "savings"}).json()) == 1
    assert len(client.get("/api/net-worth/entries", params={"year": 2024}).json()) == 1
    by_name = client.get("/api/net-worth/entries", params={"account_name": "CHASE checking"})
    assert len(by_name.json()) == 3


def test_patch_and_delete(client: TestClient) -> None:
    uid = _user(client)
    eid = client.post("/api/net-worth/entries", json=_body(uid, note="keep")).json()["id"]
    patched = client.patch(f"/api/net-worth/entries/{eid}", json={"balance_minor": 7})
    assert patched.status_code == 200
    assert patched.json()["balance_minor"] == 7 and patched.json()["note"] == "keep"
    cleared = client.patch(f"/api/net-worth/entries/{eid}", json={"note": None})
    assert cleared.json()["note"] is None and cleared.json()["balance_minor"] == 7
    assert (
        client.patch(f"/api/net-worth/entries/{eid}", json={"balance_minor": -1}).status_code == 422
    )
    assert client.patch("/api/net-worth/entries/999", json={"balance_minor": 1}).status_code == 404
    assert client.delete(f"/api/net-worth/entries/{eid}").status_code == 204
    assert client.delete(f"/api/net-worth/entries/{eid}").status_code == 404


def test_summary_numbers_and_isolation(client: TestClient) -> None:
    a, b = _user(client, "A"), _user(client, "B")
    for cat, name, bal in (
        ("checking", "C", 100000),
        ("savings", "S", 250000),
        ("investment", "I", 500000),
        ("credit_card", "Card", 30000),
    ):
        client.post(
            "/api/net-worth/entries",
            json=_body(a, category=cat, account_name=name, balance_minor=bal),
        )
    client.post("/api/net-worth/entries", json=_body(b, balance_minor=1000))
    data = client.get("/api/net-worth/summary", params={"user_id": a}).json()
    assert data["currency"] == "USD" and data["user_id"] == a
    assert data["periods"] == ["2025-03"]
    snap = data["snapshot"]
    assert snap["net_worth_minor"] == 820000
    assert snap["assets_minor"] == 850000 and snap["liabilities_minor"] == 30000
    assert [c["category"] for c in snap["categories"]] == [
        "checking",
        "savings",
        "investment",
        "credit_card",
    ]
    assert len(snap["accounts"]) == 4
    assert data["series"][0]["by_category"]["credit_card"] == 30000
    assert len(data["account_series"]) == 4
    combined = client.get("/api/net-worth/summary").json()
    assert combined["user_id"] is None
    assert combined["snapshot"]["net_worth_minor"] == 821000


def test_summary_empty_and_bad_period(client: TestClient) -> None:
    data = client.get("/api/net-worth/summary").json()
    assert data["series"] == [] and data["snapshot"] is None and data["periods"] == []
    assert client.get("/api/net-worth/summary", params={"period": "2025-13"}).status_code == 400


def test_export_has_no_balances(client: TestClient) -> None:
    uid = _user(client)
    client.post("/api/net-worth/entries", json=_body(uid, account_name="Secret acct"))
    for fmt in ("csv", "json"):
        out = client.get("/api/export", params={"format": fmt})
        assert out.status_code == 200
        assert "balance" not in out.text.lower()
        assert "Secret acct" not in out.text


def test_user_delete_cascades_entries(client: TestClient) -> None:
    uid = _user(client)
    client.post("/api/net-worth/entries", json=_body(uid))
    assert client.delete(f"/api/users/{uid}").status_code == 204
    assert client.get("/api/net-worth/entries").json() == []


def test_currency_is_usd_only(client: TestClient) -> None:
    uid = _user(client)
    assert client.post("/api/net-worth/entries", json=_body(uid, currency="USD")).status_code == 201
    for cur in ("EUR", "JPY"):
        response = client.post("/api/net-worth/entries", json=_body(uid, month=4, currency=cur))
        assert response.status_code == 422


def test_balance_upper_bound_avoids_int64_overflow(client: TestClient) -> None:
    uid = _user(client)
    ok = client.post("/api/net-worth/entries", json=_body(uid, balance_minor=10**15 - 1))
    assert ok.status_code == 201
    for huge in (10**15, 2**63, 10**30):
        bad = client.post("/api/net-worth/entries", json=_body(uid, month=5, balance_minor=huge))
        assert bad.status_code == 422, huge
    entry_id = ok.json()["id"]
    assert (
        client.patch(
            f"/api/net-worth/entries/{entry_id}", json={"balance_minor": 2**63}
        ).status_code
        == 422
    )
    assert (
        client.patch(f"/api/net-worth/entries/{entry_id}", json={"balance_minor": 7}).status_code
        == 200
    )


def test_patch_note_length_matches_create(client: TestClient) -> None:
    uid = _user(client)
    entry_id = client.post("/api/net-worth/entries", json=_body(uid, note="a" * 500)).json()["id"]
    assert (
        client.post("/api/net-worth/entries", json=_body(uid, month=6, note="a" * 501)).status_code
        == 422
    )
    too_long = client.patch(f"/api/net-worth/entries/{entry_id}", json={"note": "a" * 501})
    assert too_long.status_code == 422
    fine = client.patch(f"/api/net-worth/entries/{entry_id}", json={"note": "b" * 500})
    assert fine.status_code == 200 and fine.json()["note"] == "b" * 500
    cleared = client.patch(f"/api/net-worth/entries/{entry_id}", json={"note": None})
    assert cleared.status_code == 200 and cleared.json()["note"] is None


def test_summary_range_cannot_widen_beyond_data(client: TestClient) -> None:
    uid = _user(client)
    client.post("/api/net-worth/entries", json=_body(uid, year=2025, month=1))
    client.post("/api/net-worth/entries", json=_body(uid, year=2025, month=3))
    wide = client.get(
        "/api/net-worth/summary",
        params={"user_id": uid, "period_from": "0000-01", "period_to": "9999-12"},
    )
    assert wide.status_code == 200
    assert [p["period"] for p in wide.json()["series"]] == ["2025-01", "2025-02", "2025-03"]
    narrow = client.get(
        "/api/net-worth/summary",
        params={"user_id": uid, "period_from": "2025-02", "period_to": "2025-02"},
    )
    assert [p["period"] for p in narrow.json()["series"]] == ["2025-02"]
