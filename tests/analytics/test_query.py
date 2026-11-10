"""Tests for the analytics query builder (§3.9, P1-E)."""

from __future__ import annotations

import time
import uuid
from datetime import date

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from spend_analyzer.analytics.query import SpendQuery, build_select, run_query
from spend_analyzer.db.models import (
    Account,
    Category,
    Issuer,
    Statement,
    Subcategory,
    Transaction,
    User,
)


def _make_account(session: Session, name: str = "Alex") -> tuple[User, Account, Statement]:
    user = User(name=name)
    issuer = Issuer(name=f"{name} Bank", slug=f"{name.lower()}-bank-{uuid.uuid4().hex[:8]}")
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


def _add_txn(
    session: Session,
    *,
    statement: Statement,
    account: Account,
    user: User,
    posted_date: date,
    amount_minor: int,
    kind: str = "purchase",
    is_spend: bool = True,
    category_key: str | None = None,
    subcategory_key: str | None = None,
    subcategory_id: int | None = None,
    description_clean: str = "Coffee Shop",
    merchant_key: str = "coffee shop",
    currency: str = "USD",
) -> Transaction:
    cat_id = _cat_id(session, category_key) if category_key else None
    if subcategory_id is None and subcategory_key is not None:
        assert category_key is not None
        subcategory_id = _sub_id(session, category_key, subcategory_key)
    txn = Transaction(
        statement_id=statement.id,
        account_id=account.id,
        user_id=user.id,
        posted_date=posted_date.isoformat(),
        description_raw=description_clean,
        description_clean=description_clean,
        merchant_key=merchant_key,
        amount_minor=amount_minor,
        currency=currency,
        kind=kind,
        is_spend=is_spend,
        category_id=cat_id,
        subcategory_id=subcategory_id,
        dedupe_hash=uuid.uuid4().hex,
    )
    session.add(txn)
    session.flush()
    return txn


def test_default_kinds_and_group_by_category(session: Session) -> None:
    """A7: with defaults, purchases/fees/interest are summed by (period, category); refunds and
    payments are excluded because `net_refunds` is netting-only, not inclusion, until a refund is
    present, and `payment`/`transfer`/`adjustment` never count as spend regardless."""
    user, account, statement = _make_account(session)
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 5),
        amount_minor=1000,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 20),
        amount_minor=500,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 10),
        amount_minor=2000,
        kind="payment",
        is_spend=False,
        category_key="others",
        subcategory_key="payments_transfers",
    )
    session.commit()

    rows = run_query(session, SpendQuery(granularity="month", group_by=["period", "category"]))
    assert rows == [
        {
            "period": "2026-01",
            "category": "grocery",
            "currency": "USD",
            "total_minor": 1500,
            "txn_count": 2,
            "avg_minor": 750,
        }
    ]


def test_net_refunds_true_includes_refund_with_negative_sign(session: Session) -> None:
    """A7: with `net_refunds=True` (the default), a refund's negative amount subtracts from the
    total; the accepts criterion is that netting changes the total by exactly the refund amount."""
    user, account, statement = _make_account(session)
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 2, 1),
        amount_minor=3000,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 2, 3),
        amount_minor=-500,
        kind="refund",
        is_spend=False,
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    session.commit()

    netted = run_query(
        session,
        SpendQuery(granularity="month", group_by=["period", "category"], net_refunds=True),
    )
    not_netted = run_query(
        session,
        SpendQuery(granularity="month", group_by=["period", "category"], net_refunds=False),
    )
    assert netted[0]["total_minor"] == 2500
    assert netted[0]["txn_count"] == 2
    assert not_netted[0]["total_minor"] == 3000
    assert not_netted[0]["txn_count"] == 1
    # Netting changed the total by exactly the refund's (negative) amount.
    assert netted[0]["total_minor"] - not_netted[0]["total_minor"] == -500


def test_include_non_spend_removes_kind_filter_entirely(session: Session) -> None:
    user, account, statement = _make_account(session)
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 3, 1),
        amount_minor=1000,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 3, 2),
        amount_minor=5000,
        kind="transfer",
        is_spend=False,
        category_key="others",
        subcategory_key="payments_transfers",
    )
    session.commit()

    rows = run_query(
        session,
        SpendQuery(granularity="all", group_by=["category"], include_non_spend=True),
    )
    total = sum(r["total_minor"] for r in rows)
    assert total == 6000
    assert sum(r["txn_count"] for r in rows) == 2


@pytest.mark.parametrize(
    ("granularity", "expected_period"),
    [
        ("day", "2026-06-15"),
        ("month", "2026-06"),
        ("quarter", "2026-Q2"),
        ("year", "2026"),
        ("all", "all"),
    ],
)
def test_period_bucketing_every_granularity(
    session: Session, granularity: str, expected_period: str
) -> None:
    user, account, statement = _make_account(session)
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 6, 15),
        amount_minor=1234,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    session.commit()

    rows = run_query(session, SpendQuery(granularity=granularity, group_by=["period"]))  # type: ignore[arg-type]
    assert rows == [
        {
            "period": expected_period,
            "currency": "USD",
            "total_minor": 1234,
            "txn_count": 1,
            "avg_minor": 1234,
        }
    ]


def test_week_bucketing_is_iso_week_across_year_boundaries(session: Session) -> None:
    """Weekly periods are ISO weeks (`isocalendar`), so a week spanning New Year is one bucket
    keyed by its ISO week-year instead of a `%W` split into `2025-W52` and `2026-W00`."""
    user, account, statement = _make_account(session)
    days = [
        date(2025, 12, 28),  # Sunday: last day of 2025-W52
        date(2025, 12, 29),  # Monday: 2026-W01 (week-year differs from the calendar year)
        date(2026, 1, 1),  # Thursday of that same week
        date(2026, 1, 4),  # Sunday: last day of 2026-W01
        date(2026, 1, 5),  # Monday: 2026-W02
        date(2026, 6, 17),
        date(2027, 1, 3),  # Sunday: 2026-W53
    ]
    for d in days:
        _add_txn(
            session,
            statement=statement,
            account=account,
            user=user,
            posted_date=d,
            amount_minor=100,
            kind="purchase",
            category_key="grocery",
            subcategory_key="grocery_stores",
        )
    session.commit()

    rows = run_query(session, SpendQuery(granularity="week", group_by=["period"]))
    by_period = {str(r["period"]): r["txn_count"] for r in rows}
    expected: dict[str, int] = {}
    for d in days:
        iso = d.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        expected[key] = expected.get(key, 0) + 1
    assert by_period == expected
    assert by_period["2026-W01"] == 3


def test_merged_subcategory_resolves_to_merged_into(session: Session) -> None:
    """A merged subcategory's historical rows land in the merged bucket in grouping output."""
    user, account, statement = _make_account(session)
    online_cat_id = _cat_id(session, "online_shopping")
    amazon_id = _sub_id(session, "online_shopping", "amazon")
    old = Subcategory(
        category_id=online_cat_id,
        key="amazon_old",
        label="Amazon (old)",
        is_dynamic=True,
        status="merged",
        merged_into=amazon_id,
    )
    session.add(old)
    session.flush()

    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 1),
        amount_minor=1000,
        kind="purchase",
        category_key="online_shopping",
        subcategory_id=old.id,
    )
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 2),
        amount_minor=500,
        kind="purchase",
        category_key="online_shopping",
        subcategory_id=amazon_id,
    )
    session.commit()

    rows = run_query(
        session, SpendQuery(granularity="all", group_by=["subcategory"], net_refunds=False)
    )
    assert rows == [
        {
            "subcategory": "amazon",
            "currency": "USD",
            "total_minor": 1500,
            "txn_count": 2,
            "avg_minor": 750,
        }
    ]


def test_search_handles_percent_underscore_and_quote_literally(session: Session) -> None:
    user, account, statement = _make_account(session)
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 1),
        amount_minor=100,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
        description_clean="100% off_sale it's here",
        merchant_key="hundred percent",
    )
    _add_txn(
        session,
        statement=statement,
        account=account,
        user=user,
        posted_date=date(2026, 1, 2),
        amount_minor=200,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
        description_clean="anything else entirely",
        merchant_key="other merchant",
    )
    session.commit()

    for needle in ["100%", "off_sale", "it's"]:
        rows = run_query(
            session,
            SpendQuery(granularity="all", group_by=["category"], search=needle),
        )
        assert rows and rows[0]["txn_count"] == 1, f"search {needle!r} should match exactly one row"

    no_match = run_query(
        session,
        SpendQuery(granularity="all", group_by=["category"], search="100_"),
    )
    assert no_match == []


def test_invalid_group_by_and_granularity_raise() -> None:
    with pytest.raises(ValueError):
        build_select(SpendQuery(group_by=["not_a_key"]))  # type: ignore[list-item]
    with pytest.raises(ValueError):
        build_select(SpendQuery(granularity="fortnight"))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_select(SpendQuery(group_by=[]))
    with pytest.raises(ValueError):
        build_select(SpendQuery(kinds=["not_a_kind"]))


def test_amount_and_account_and_user_filters(session: Session) -> None:
    user1, account1, statement1 = _make_account(session, "Alex")
    user2, account2, statement2 = _make_account(session, "Sam")
    _add_txn(
        session,
        statement=statement1,
        account=account1,
        user=user1,
        posted_date=date(2026, 1, 1),
        amount_minor=100,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    _add_txn(
        session,
        statement=statement2,
        account=account2,
        user=user2,
        posted_date=date(2026, 1, 1),
        amount_minor=9000,
        kind="purchase",
        category_key="grocery",
        subcategory_key="grocery_stores",
    )
    session.commit()

    by_user = run_query(
        session, SpendQuery(granularity="all", group_by=["user"], user_ids=[user1.id])
    )
    assert by_user == [
        {"user": user1.id, "currency": "USD", "total_minor": 100, "txn_count": 1, "avg_minor": 100}
    ]

    by_account = run_query(
        session, SpendQuery(granularity="all", group_by=["account"], account_ids=[account2.id])
    )
    assert by_account[0]["account"] == account2.id
    assert by_account[0]["total_minor"] == 9000

    by_amount = run_query(
        session,
        SpendQuery(granularity="all", group_by=["category"], amount_min_minor=1000),
    )
    assert by_amount[0]["total_minor"] == 9000


def test_benchmark_50k_rows_under_budget_and_uses_index(session: Session) -> None:
    """§6.6: a group-by-month-and-category query over 50k transactions must run in < 200 ms and
    use `ix_txn_user_date` rather than a full scan."""
    user, account, statement = _make_account(session, "Bench")
    n = 50_000
    base = date(2024, 1, 1)
    rows = []
    for i in range(n):
        posted = date.fromordinal(base.toordinal() + (i % 700))
        rows.append(
            {
                "statement_id": statement.id,
                "account_id": account.id,
                "user_id": user.id,
                "posted_date": posted.isoformat(),
                "description_raw": "BENCH MERCHANT",
                "description_clean": "BENCH MERCHANT",
                "merchant_key": "bench merchant",
                "amount_minor": 100 + (i % 50),
                "currency": "USD",
                "kind": "purchase",
                "is_spend": True,
                "category_id": _cat_id(session, "grocery"),
                "subcategory_id": _sub_id(session, "grocery", "grocery_stores"),
                "dedupe_hash": f"bench-{i}",
            }
        )
    session.execute(insert(Transaction), rows)
    session.commit()

    query = SpendQuery(
        user_ids=[user.id],
        date_from=base,
        date_to=date.fromordinal(base.toordinal() + 699),
        granularity="month",
        group_by=["period", "category"],
    )
    stmt = build_select(query)

    plan_sql = str(stmt.compile(session.bind, compile_kwargs={"literal_binds": True}))
    plan = session.execute(text(f"EXPLAIN QUERY PLAN {plan_sql}")).fetchall()
    plan_text = "\n".join(str(row) for row in plan)
    assert "ix_txn_user_date" in plan_text, plan_text

    start = time.perf_counter()
    result_rows = run_query(session, query)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2, f"query took {elapsed * 1000:.1f} ms, budget is 200 ms"
    assert sum(r["txn_count"] for r in result_rows) == n
