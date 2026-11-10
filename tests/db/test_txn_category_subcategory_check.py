"""I4: a transaction must have `category_id` and `subcategory_id` both set or both NULL —
`ck_txn_category_subcategory_both_or_neither` (`db/models.py`, migration `0002_...`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from personal_finance_analyzer.classify.taxonomy import sync_taxonomy
from personal_finance_analyzer.db.migrate import build_alembic_config
from personal_finance_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    Statement,
    Subcategory,
    User,
)
from personal_finance_analyzer.db.models import Transaction as TransactionModel
from personal_finance_analyzer.db.session import make_engine_for_path, make_session_factory


def _make_transaction(**overrides: object) -> TransactionModel:
    defaults: dict[str, object] = {
        "statement_id": 1,
        "account_id": 1,
        "user_id": 1,
        "posted_date": "2026-01-01",
        "description_raw": "TEST MERCHANT",
        "description_clean": "TEST MERCHANT",
        "merchant_key": "test_merchant",
        "amount_minor": 1000,
        "currency": "USD",
        "kind": "purchase",
        "is_spend": True,
        "dedupe_hash": "deadbeef",
    }
    defaults.update(overrides)
    return TransactionModel(**defaults)  # type: ignore[arg-type]


def _seed_fk_targets(session: Session) -> None:
    """Insert the minimal `users`/`issuers`/`accounts`/`statements` rows a transaction's foreign
    keys reference, so a half-set-row test fails on the constraint under test, not an unrelated
    FK violation."""
    user = User(name="tester", pii_aliases="[]")
    session.add(user)
    session.flush()
    issuer = Issuer(name="Test Bank", slug="test-bank", match_terms="[]")
    session.add(issuer)
    session.flush()
    account = Account(
        user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Test credit"
    )
    session.add(account)
    session.flush()
    statement = Statement(
        account_id=account.id,
        file_sha256="0" * 64,
        original_name="statement.pdf",
        status="parsed",
        ingested_at="2026-01-01T00:00:00Z",
    )
    session.add(statement)
    session.flush()


class TestHalfSetRowRejected:
    def test_category_without_subcategory_is_rejected(self, session: Session) -> None:
        _seed_fk_targets(session)
        category = session.execute(select(Category)).scalars().first()
        assert category is not None
        session.add(_make_transaction(category_id=category.id, subcategory_id=None))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_subcategory_without_category_is_rejected(self, session: Session) -> None:
        _seed_fk_targets(session)
        subcategory = session.execute(select(Subcategory)).scalars().first()
        assert subcategory is not None
        session.add(_make_transaction(category_id=None, subcategory_id=subcategory.id))
        with pytest.raises(IntegrityError):
            session.flush()

    def test_both_null_is_accepted(self, session: Session) -> None:
        _seed_fk_targets(session)
        session.add(_make_transaction(category_id=None, subcategory_id=None))
        session.flush()  # does not raise

    def test_both_set_is_accepted(self, session: Session) -> None:
        _seed_fk_targets(session)
        category = session.execute(select(Category)).scalars().first()
        subcategory = (
            session.execute(
                select(Subcategory).where(Subcategory.category_id == category.id)  # type: ignore[union-attr]
            )
            .scalars()
            .first()
        )
        assert category is not None and subcategory is not None
        session.add(_make_transaction(category_id=category.id, subcategory_id=subcategory.id))
        session.flush()  # does not raise


def test_migrating_an_existing_pre_check_database_works(tmp_path: Path) -> None:
    """A database created before this constraint existed (`0001_initial` only) upgrades cleanly
    to `head` (`0002_...`, via `batch_alter_table`), keeps its data, and enforces the new check
    constraint from then on."""
    db_path = tmp_path / "existing.db"
    db_url = f"sqlite:///{db_path}"
    config = build_alembic_config(db_url)

    # Simulate a database provisioned before 0002 existed.
    command.upgrade(config, "0001_initial")

    engine = make_engine_for_path(db_path)
    try:
        factory = make_session_factory(engine)
        with factory() as pre_session:
            sync_taxonomy(pre_session)
            pre_session.commit()
            _seed_fk_targets(pre_session)
            category = pre_session.execute(select(Category)).scalars().first()
            subcategory = (
                pre_session.execute(
                    select(Subcategory).where(Subcategory.category_id == category.id)  # type: ignore[union-attr]
                )
                .scalars()
                .first()
            )
            assert category is not None and subcategory is not None
            pre_session.add(
                _make_transaction(category_id=category.id, subcategory_id=subcategory.id)
            )
            pre_session.commit()
    finally:
        engine.dispose()

    # Upgrade the existing database to head — must not fail, and must not lose the row.
    command.upgrade(config, "head")

    engine = make_engine_for_path(db_path)
    try:
        factory = make_session_factory(engine)
        with factory() as post_session:
            rows = post_session.execute(select(TransactionModel)).scalars().all()
            assert len(rows) == 1
            assert rows[0].category_id is not None
            assert rows[0].subcategory_id is not None

            # The constraint is now enforced on this migrated (not freshly created) database.
            post_session.add(
                _make_transaction(category_id=rows[0].category_id, subcategory_id=None)
            )
            with pytest.raises(IntegrityError):
                post_session.flush()
    finally:
        engine.dispose()
