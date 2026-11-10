from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify import taxonomy
from personal_finance_analyzer.core.errors import ConfigError
from personal_finance_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    Statement,
    Subcategory,
    Transaction,
    User,
)


def test_yaml_loads_and_validates() -> None:
    assert taxonomy.TAXONOMY_VERSION == 1
    assert "others" in taxonomy.CATEGORY_KEYS
    assert "uncategorized" in taxonomy.subcategories_for("others")
    for key in taxonomy.CATEGORY_KEYS:
        assert len(taxonomy.subcategories_for(key)) >= 1  # I4


def test_only_online_shopping_is_dynamic() -> None:
    for key in taxonomy.CATEGORY_KEYS:
        if key == "online_shopping":
            assert taxonomy.is_dynamic(key)
        else:
            assert not taxonomy.is_dynamic(key)


def test_sync_taxonomy_is_idempotent(session: Session) -> None:
    # `session` fixture already synced once.
    before = sorted(session.execute(select(Category.key)).scalars())
    taxonomy.sync_taxonomy(session)
    session.commit()
    after = sorted(session.execute(select(Category.key)).scalars())
    assert before == after == sorted(taxonomy.CATEGORY_KEYS)

    sub_count_before = session.execute(select(Subcategory.id)).all()
    taxonomy.sync_taxonomy(session)
    session.commit()
    sub_count_after = session.execute(select(Subcategory.id)).all()
    assert len(sub_count_before) == len(sub_count_after)


def test_sync_taxonomy_raises_when_referenced_category_removed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = User(name="Alex")
    issuer = Issuer(name="Invented Bank", slug="invented-bank")
    session.add_all([user, issuer])
    session.flush()
    account = Account(
        user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
    )
    session.add(account)
    session.flush()
    statement = Statement(file_sha256="a" * 64, original_name="s.pdf", status="parsed")
    session.add(statement)
    session.flush()

    others = session.execute(select(Category).where(Category.key == "others")).scalar_one()
    others_sub = (
        session.execute(select(Subcategory).where(Subcategory.category_id == others.id))
        .scalars()
        .first()
    )
    assert others_sub is not None
    txn = Transaction(
        statement_id=statement.id,
        account_id=account.id,
        user_id=user.id,
        posted_date="2026-01-05",
        description_raw="X",
        description_clean="X",
        merchant_key="x",
        amount_minor=100,
        currency="USD",
        kind="purchase",
        is_spend=True,
        dedupe_hash="h",
        category_id=others.id,
        subcategory_id=others_sub.id,
    )
    session.add(txn)
    session.commit()

    fake_categories = tuple(c for c in taxonomy.CATEGORIES if c.key != "others")
    monkeypatch.setattr(taxonomy, "CATEGORIES", fake_categories)
    monkeypatch.setattr(taxonomy, "CATEGORY_KEYS", tuple(c.key for c in fake_categories))

    with pytest.raises(ConfigError):
        taxonomy.sync_taxonomy(session)
