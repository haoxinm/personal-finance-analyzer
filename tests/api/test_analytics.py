from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from spend_analyzer.db.models import Account, Issuer, Statement, Transaction, User, utcnow_iso


def _seed(session: Session) -> None:
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
        file_sha256="d" * 64,
        original_name="s.pdf",
        status="parsed",
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.flush()
    for i, (merchant, amount) in enumerate([("Trader Joes", 1000), ("Shell", 5000)]):
        session.add(
            Transaction(
                statement_id=statement.id,
                account_id=account.id,
                user_id=user.id,
                posted_date="2026-01-05",
                description_raw=merchant,
                description_clean=merchant,
                merchant_key=merchant.lower(),
                amount_minor=amount,
                currency="USD",
                kind="purchase",
                is_spend=True,
                dedupe_hash=f"hash{i}",
                created_at=utcnow_iso(),
                updated_at=utcnow_iso(),
            )
        )
    session.commit()


def test_analytics_summary(client: TestClient, session: Session) -> None:
    _seed(session)
    response = client.get("/api/analytics/summary")
    assert response.status_code == 200
    rows = response.json()
    assert sum(r["total_minor"] for r in rows) == 6000


def test_analytics_invalid_granularity_is_400(client: TestClient) -> None:
    response = client.get("/api/analytics/summary", params={"granularity": "fortnight"})
    assert response.status_code in (400, 422)


def test_top_merchants(client: TestClient, session: Session) -> None:
    _seed(session)
    response = client.get("/api/analytics/top-merchants")
    assert response.status_code == 200
    rows = response.json()
    assert rows[0]["merchant"] == "shell"
    assert rows[0]["total_minor"] == 5000
    # No `merchant_canonical` was ever recorded for either merchant, so `display_name` falls
    # back to the A5 representative `description_clean` (§ "Add a `display_name`" polish item).
    assert rows[0]["display_name"] == "Shell"
    assert rows[1]["display_name"] == "Trader Joes"


def test_top_merchants_drops_unkeyed_rows_before_the_limit(
    client: TestClient, session: Session
) -> None:
    _seed(session)
    first = session.query(Transaction).first()
    assert first is not None
    # A large bucket with no merchant key would otherwise take the single slot the limit allows.
    session.add(
        Transaction(
            statement_id=first.statement_id,
            account_id=first.account_id,
            user_id=first.user_id,
            posted_date="2026-01-06",
            description_raw="MYSTERY",
            description_clean="MYSTERY",
            merchant_key="",
            amount_minor=90000,
            currency="USD",
            kind="purchase",
            is_spend=True,
            dedupe_hash="hash-unkeyed",
            created_at=utcnow_iso(),
            updated_at=utcnow_iso(),
        )
    )
    session.commit()

    response = client.get("/api/analytics/top-merchants", params={"limit": 1})
    assert response.status_code == 200
    assert [row["merchant"] for row in response.json()] == ["shell"]


def test_top_merchants_display_name_prefers_the_most_common_merchant_canonical(
    client: TestClient, session: Session
) -> None:
    _seed(session)
    for txn in session.query(Transaction).filter(Transaction.merchant_key == "shell").all():
        txn.merchant_canonical = "Shell Oil Co."
    session.commit()

    response = client.get("/api/analytics/top-merchants")
    assert response.status_code == 200
    rows = response.json()
    shell_row = next(r for r in rows if r["merchant"] == "shell")
    assert shell_row["display_name"] == "Shell Oil Co."
