"""Proves `PRAGMA foreign_keys=ON` (A9) is actually applied: without it, `ON DELETE CASCADE`
would be silently inert and this test would fail."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import Account, Issuer, Statement, Transaction, User


def test_deleting_statement_cascades_to_transactions(session: Session) -> None:
    user = User(name="Alex")
    issuer = Issuer(name="Invented Bank", slug="invented-bank")
    session.add_all([user, issuer])
    session.flush()

    account = Account(
        user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
    )
    session.add(account)
    session.flush()

    statement = Statement(
        file_sha256="deadbeef" * 8,
        original_name="statement.pdf",
        status="parsed",
        account_id=account.id,
    )
    session.add(statement)
    session.flush()

    txn = Transaction(
        statement_id=statement.id,
        account_id=account.id,
        user_id=user.id,
        posted_date="2026-01-05",
        description_raw="COFFEE SHOP #1234",
        description_clean="COFFEE SHOP",
        merchant_key="coffee shop",
        amount_minor=500,
        currency="USD",
        kind="purchase",
        is_spend=True,
        dedupe_hash="hash-1",
    )
    session.add(txn)
    session.commit()

    txn_id = txn.id
    assert session.get(Transaction, txn_id) is not None

    session.delete(statement)
    session.commit()
    # The session factory uses `expire_on_commit=False` (a deliberate choice, §3.2) so that a
    # normal commit does not force extra round trips; `passive_deletes=True` means the ORM never
    # loaded `statement.transactions`, so it has no idea the DB-level `ON DELETE CASCADE` (A9)
    # removed the row. Expire explicitly to prove the *database* row is gone, not just that the
    # ORM forgot to check.
    session.expire_all()

    assert session.get(Transaction, txn_id) is None
    assert session.execute(select(Transaction).where(Transaction.id == txn_id)).first() is None
