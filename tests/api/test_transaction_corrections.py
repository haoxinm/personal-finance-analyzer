"""`PATCH /api/transactions/{id}` corrections: date, amount and description."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Statement, Transaction
from tests.api.test_transactions import _make_transaction


def _txn(session: Session, txn_id: int) -> Transaction:
    session.expire_all()
    txn = session.get(Transaction, txn_id)
    assert txn is not None
    return txn


def test_each_field_round_trips_and_keeps_the_imported_value(
    client: TestClient, session: Session
) -> None:
    txn_id = _make_transaction(session)
    body = client.get("/api/transactions").json()["items"][0]
    assert body["corrections"] is None

    for payload, field, new, old in (
        ({"posted_date": "2026-01-07"}, "posted_date", "2026-01-07", "2026-01-05"),
        ({"amount_minor": 1999}, "amount_minor", 1999, 1250),
        (
            {"description_clean": "  Trader Joe's "},
            "description_clean",
            "Trader Joe's",
            "Trader Joes",
        ),
    ):
        response = client.patch(f"/api/transactions/{txn_id}", json=payload)
        assert response.status_code == 200, response.text
        out = response.json()
        assert out[field] == new
        assert out["corrections"][field] == old

    out = client.get("/api/transactions").json()["items"][0]
    assert out["corrections"] == {
        "posted_date": "2026-01-05",
        "amount_minor": 1250,
        "description_clean": "Trader Joes",
    }
    # The listing's own columns changed, and the evidence did not.
    txn = _txn(session, txn_id)
    assert txn.description_raw == "TRADER JOES #123" and txn.merchant_key == "trader joes"


def test_setting_a_field_back_to_the_imported_value_clears_the_correction(
    client: TestClient, session: Session
) -> None:
    txn_id = _make_transaction(session)
    client.patch(f"/api/transactions/{txn_id}", json={"amount_minor": 1999})
    out = client.patch(f"/api/transactions/{txn_id}", json={"amount_minor": 1250}).json()
    assert out["amount_minor"] == 1250 and out["corrections"] is None


def test_revert_fields_restores_the_imported_values(client: TestClient, session: Session) -> None:
    txn_id = _make_transaction(session)
    client.patch(
        f"/api/transactions/{txn_id}",
        json={"amount_minor": -500, "posted_date": "2026-02-01", "description_clean": "X"},
    )
    out = client.patch(
        f"/api/transactions/{txn_id}", json={"revert_fields": ["amount_minor", "posted_date"]}
    ).json()
    assert out["amount_minor"] == 1250 and out["posted_date"] == "2026-01-05"
    assert out["description_clean"] == "X"
    assert out["corrections"] == {
        "posted_date": None,
        "amount_minor": None,
        "description_clean": "Trader Joes",
    }
    out = client.patch(
        f"/api/transactions/{txn_id}", json={"revert_fields": ["description_clean"]}
    ).json()
    assert out["corrections"] is None and out["description_clean"] == "Trader Joes"


def test_validation(client: TestClient, session: Session) -> None:
    txn_id = _make_transaction(session)
    url = f"/api/transactions/{txn_id}"
    assert client.patch(url, json={"amount_minor": 0}).status_code == 422
    assert client.patch(url, json={"amount_minor": 100_000_000_001}).status_code == 422
    assert client.patch(url, json={"description_clean": "   "}).status_code == 422
    assert client.patch(url, json={"description_clean": "x" * 201}).status_code == 422
    assert client.patch(url, json={"description_clean": "bad\x00text"}).status_code == 422
    far = (date.today() + timedelta(days=400)).isoformat()
    assert client.patch(url, json={"posted_date": far}).status_code == 400
    assert client.patch(url, json={"posted_date": "1969-12-31"}).status_code == 400
    clash = client.patch(url, json={"amount_minor": 5, "revert_fields": ["amount_minor"]})
    assert clash.status_code == 400
    assert client.patch(url, json={"revert_fields": ["bogus"]}).status_code == 422
    assert _txn(session, txn_id).orig_amount_minor is None  # nothing was applied
    assert client.patch(url, json={"posted_date": "2026-01-05"}).status_code == 200


def test_a_correction_changes_neither_the_hash_nor_the_classification(
    client: TestClient, session: Session
) -> None:
    txn_id = _make_transaction(session)
    before = _txn(session, txn_id)
    snapshot = (
        before.dedupe_hash,
        before.classified_by,
        before.needs_review,
        before.kind,
        before.is_spend,
    )
    client.patch(
        f"/api/transactions/{txn_id}",
        json={"amount_minor": 77, "posted_date": "2026-01-09", "description_clean": "Other"},
    )
    after = _txn(session, txn_id)
    assert (
        after.dedupe_hash,
        after.classified_by,
        after.needs_review,
        after.kind,
        after.is_spend,
    ) == snapshot


def test_the_statement_total_and_reconciliation_follow_the_amount(
    client: TestClient, session: Session
) -> None:
    txn_id = _make_transaction(session)
    statement = session.get(Statement, _txn(session, txn_id).statement_id)
    assert statement is not None
    statement.parsed_total_minor = 1250
    statement.opening_balance_minor = 0
    statement.closing_balance_minor = 1250
    session.commit()
    assert client.get(f"/api/statements/{statement.id}").json()["reconciliation_delta_minor"] == 0

    client.patch(f"/api/transactions/{txn_id}", json={"amount_minor": 1500})
    session.expire_all()
    assert statement.parsed_total_minor == 1500
    assert client.get(f"/api/statements/{statement.id}").json()["reconciliation_delta_minor"] == 250
    client.patch(f"/api/transactions/{txn_id}", json={"revert_fields": ["amount_minor"]})
    session.expire_all()
    assert statement.parsed_total_minor == 1250


def test_analytics_reflects_the_corrected_amount_and_date(
    client: TestClient, session: Session
) -> None:
    txn_id = _make_transaction(session)
    client.patch(
        f"/api/transactions/{txn_id}", json={"amount_minor": 4000, "posted_date": "2026-03-10"}
    )
    rows = client.get(
        "/api/analytics/summary", params={"granularity": "month", "group_by": "period"}
    )
    assert rows.status_code == 200, rows.text
    assert [(r["period"], r["total_minor"]) for r in rows.json()] == [("2026-03", 4000)]
