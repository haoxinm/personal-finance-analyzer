"""§6.6 performance budget: a group-by-month-and-category analytics query over 50k transactions
runs in < 200 ms.

Opt-in only (`@pytest.mark.benchmark`; run with `uv run pytest -m benchmark tests/bench -v -s`).
`tests/analytics/test_query.py::test_benchmark_50k_rows_under_budget_and_uses_index` (P1-E, not
owned here) already asserts this same budget as part of the always-on suite; this module is P4-D's
own copy so the P4-D benchmark suite is a complete, self-contained report of every §6.6 budget in
one place, run and read together (import, classify, analytics)."""

from __future__ import annotations

import time
import uuid
from datetime import date

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.query import SpendQuery, run_query
from personal_finance_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    Statement,
    Subcategory,
    Transaction,
    User,
)

pytestmark = pytest.mark.benchmark

_ROW_COUNT = 50_000
_BUDGET_S = 0.2


def _make_account(session: Session) -> tuple[User, Account, Statement]:
    user = User(name="bench-user")
    issuer = Issuer(name="Bench Bank", slug=f"bench-bank-{uuid.uuid4().hex[:8]}")
    session.add_all([user, issuer])
    session.flush()
    account = Account(
        user_id=user.id, issuer_id=issuer.id, account_type="credit", display_name="Card"
    )
    session.add(account)
    session.flush()
    statement = Statement(
        file_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        original_name="statement.pdf",
        status="parsed",
        account_id=account.id,
    )
    session.add(statement)
    session.flush()
    return user, account, statement


def _cat_id(session: Session, key: str) -> int:
    cat = session.execute(select(Category).where(Category.key == key)).scalar_one()
    assert cat.id is not None
    return cat.id


def _sub_id(session: Session, category_key: str, sub_key: str) -> int:
    cat_id = _cat_id(session, category_key)
    sub = session.execute(
        select(Subcategory).where(Subcategory.category_id == cat_id, Subcategory.key == sub_key)
    ).scalar_one()
    assert sub.id is not None
    return sub.id


def test_analytics_50k_rows_under_budget(session: Session) -> None:
    user, account, statement = _make_account(session)
    base = date(2024, 1, 1)
    cat_id = _cat_id(session, "grocery")
    sub_id = _sub_id(session, "grocery", "grocery_stores")

    rows = [
        {
            "statement_id": statement.id,
            "account_id": account.id,
            "user_id": user.id,
            "posted_date": date.fromordinal(base.toordinal() + (i % 700)).isoformat(),
            "description_raw": "BENCH MERCHANT",
            "description_clean": "BENCH MERCHANT",
            "merchant_key": "bench merchant",
            "amount_minor": 100 + (i % 50),
            "currency": "USD",
            "kind": "purchase",
            "is_spend": True,
            "category_id": cat_id,
            "subcategory_id": sub_id,
            "dedupe_hash": f"p4d-bench-{i}",
        }
        for i in range(_ROW_COUNT)
    ]
    session.execute(insert(Transaction), rows)
    session.commit()

    query = SpendQuery(
        user_ids=[user.id],
        date_from=base,
        date_to=date.fromordinal(base.toordinal() + 699),
        granularity="month",
        group_by=["period", "category"],
    )

    start = time.perf_counter()
    result_rows = run_query(session, query)
    elapsed = time.perf_counter() - start

    print(
        f"\n[bench] analytics group-by over {_ROW_COUNT} rows: {elapsed * 1000:.1f} ms "
        f"(budget {_BUDGET_S * 1000:.0f} ms)"
    )
    assert sum(int(r["txn_count"]) for r in result_rows) == _ROW_COUNT  # type: ignore[call-overload]
    assert elapsed < _BUDGET_S, (
        f"query took {elapsed * 1000:.1f} ms, budget is {_BUDGET_S * 1000:.0f} ms"
    )
