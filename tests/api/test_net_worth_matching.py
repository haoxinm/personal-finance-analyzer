"""Net worth: account names match across months, and old carried balances are flagged stale."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics import net_worth_query
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


def test_account_name_is_matched_across_months(client: TestClient, session: Session) -> None:
    """ "chase checking" typed in March continues the account entered in January, so the carried
    January balance is not counted next to a second, differently spelled account."""
    uid = _user(client)
    client.post("/api/net-worth/entries", json=_body(uid, month=1, balance_minor=100))
    march = client.post(
        "/api/net-worth/entries",
        json=_body(uid, month=3, account_name="chase CHECKING", balance_minor=300),
    )
    assert march.status_code == 201  # a new month, but the same account
    assert march.json()["account_name"] == "Chase checking"
    session.expire_all()
    assert set(session.scalars(select(BalanceEntry.account_name))) == {"Chase checking"}
    summary = client.get("/api/net-worth/summary", params={"user_id": uid}).json()
    assert summary["snapshot"]["assets_minor"] == 300  # not 100 + 300
    assert len(summary["snapshot"]["accounts"]) == 1


def test_same_name_in_another_category_or_user_is_a_different_account(client: TestClient) -> None:
    a, b = _user(client, "A"), _user(client, "B")
    client.post("/api/net-worth/entries", json=_body(a, month=1, account_name="Main"))
    other_cat = client.post(
        "/api/net-worth/entries", json=_body(a, month=2, account_name="main", category="savings")
    )
    other_user = client.post("/api/net-worth/entries", json=_body(b, month=2, account_name="main"))
    assert other_cat.json()["account_name"] == "main"
    assert other_user.json()["account_name"] == "main"


def test_summary_marks_old_carried_balances_stale(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(net_worth_query, "_today", lambda: date(2025, 5, 31))
    uid = _user(client)
    client.post(
        "/api/net-worth/entries", json=_body(uid, month=1, account_name="Old", balance_minor=100)
    )
    client.post(
        "/api/net-worth/entries", json=_body(uid, month=5, account_name="Fresh", balance_minor=7)
    )
    snap = client.get("/api/net-worth/summary", params={"user_id": uid}).json()["snapshot"]
    accounts = {a["account_name"]: a for a in snap["accounts"]}
    assert accounts["Old"]["stale"] is True and accounts["Old"]["months_old"] == 4
    assert accounts["Old"]["carried"] is True and accounts["Old"]["as_of"] == "2025-01"
    assert accounts["Fresh"]["stale"] is False and accounts["Fresh"]["months_old"] == 0
    assert snap["assets_minor"] == 107  # the balance is still counted: only the flag is new


def test_non_ascii_names_match_across_months_and_in_the_entry_filter(
    client: TestClient, session: Session
) -> None:
    uid = _user(client)
    client.post("/api/net-worth/entries", json=_body(uid, month=1, account_name="Ärzte Konto"))
    again = client.post(
        "/api/net-worth/entries",
        json=_body(uid, month=1, account_name="ärzte konto", balance_minor=5),
    )
    assert again.status_code == 200  # same month, same account: updated, not a second row
    assert again.json()["account_name"] == "Ärzte Konto" and again.json()["balance_minor"] == 5
    march = client.post(
        "/api/net-worth/entries", json=_body(uid, month=3, account_name="ÄRZTE KONTO")
    )
    assert march.status_code == 201 and march.json()["account_name"] == "Ärzte Konto"
    listed = client.get(
        "/api/net-worth/entries", params={"user_id": uid, "account_name": "ärzte konto"}
    ).json()
    assert len(listed) == 2
    session.expire_all()
    assert len(list(session.scalars(select(BalanceEntry)))) == 2


def test_summary_carries_the_warnings_field(client: TestClient) -> None:
    uid = _user(client)
    client.post("/api/net-worth/entries", json=_body(uid, month=1))
    assert client.get("/api/net-worth/summary", params={"user_id": uid}).json()["warnings"] == []
