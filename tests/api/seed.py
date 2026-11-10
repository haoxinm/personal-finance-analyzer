"""Seed helpers shared by the API tests that need many transactions across accounts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    Statement,
    Subcategory,
    Transaction,
    User,
    utcnow_iso,
)


def base(session: Session) -> tuple[User, list[Account], Statement]:
    user = User(name="Alex", is_default=True)
    issuer = Issuer(name="Chase", slug="chase", match_terms="[]")
    session.add_all([user, issuer])
    session.flush()
    accounts = [
        Account(
            user_id=user.id,
            issuer_id=issuer.id,
            account_type="credit",
            display_name=name,
            mask=name[-1],
        )
        for name in ("Card A", "Card B")
    ]
    session.add_all(accounts)
    session.flush()
    statement = Statement(
        account_id=accounts[0].id,
        file_sha256="e" * 64,
        original_name="s.pdf",
        status="parsed",
        ingested_at=utcnow_iso(),
    )
    session.add(statement)
    session.flush()
    return user, accounts, statement


def category_ids(session: Session, category: str, subcategory: str) -> tuple[int, int]:
    cat = session.execute(select(Category).where(Category.key == category)).scalar_one()
    sub = session.execute(
        select(Subcategory).where(Subcategory.category_id == cat.id, Subcategory.key == subcategory)
    ).scalar_one()
    return cat.id, sub.id


def add_txn(
    session: Session,
    base: tuple[User, list[Account], Statement],
    index: int,
    *,
    account: int = 0,
    description: str | None = None,
    amount_minor: int = 1000,
    posted_date: str = "2026-01-05",
    category: tuple[str, str] | None = None,
    needs_review: bool = False,
    notes: str | None = None,
    merchant_key: str | None = None,
    classified_by: str | None = None,
    kind: str = "purchase",
) -> Transaction:
    user, accounts, statement = base
    category_id: int | None = None
    subcategory_id: int | None = None
    if category is not None:
        category_id, subcategory_id = category_ids(session, *category)
    txn = Transaction(
        statement_id=statement.id,
        account_id=accounts[account].id,
        user_id=user.id,
        posted_date=posted_date,
        description_raw=description or f"MERCHANT {index}",
        description_clean=description or f"Merchant {index}",
        merchant_key=merchant_key if merchant_key is not None else f"merchant {index}",
        amount_minor=amount_minor,
        currency="USD",
        kind=kind,
        is_spend=kind in ("purchase", "fee", "interest"),
        classified_by=classified_by,
        category_id=category_id,
        subcategory_id=subcategory_id,
        needs_review=needs_review,
        notes=notes,
        dedupe_hash=f"hash-{index}",
        created_at=utcnow_iso(),
        updated_at=utcnow_iso(),
    )
    session.add(txn)
    return txn
