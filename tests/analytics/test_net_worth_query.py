from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.net_worth_query import (
    NetWorthQuery,
    parse_period,
    run_net_worth_query,
)
from personal_finance_analyzer.db.models import BalanceEntry, User


def _users(session: Session) -> tuple[int, int]:
    a, b = User(name="A"), User(name="B")
    session.add_all([a, b])
    session.flush()
    return a.id, b.id


def _add(
    session: Session,
    user_id: int,
    category: str,
    name: str,
    period: str,
    bal: int,
    currency: str = "USD",
) -> None:
    y, m = parse_period(period)
    session.add(
        BalanceEntry(
            user_id=user_id,
            category=category,
            account_name=name,
            year=y,
            month=m,
            balance_minor=bal,
            currency=currency,
        )
    )
    session.flush()


def test_empty(session: Session) -> None:
    r = run_net_worth_query(session, NetWorthQuery(user_id=None))
    assert r.series == [] and r.snapshot is None and r.periods == []


def test_net_worth_assets_minus_card(session: Session) -> None:
    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-03", 100)
    _add(session, u, "savings", "S", "2025-03", 250)
    _add(session, u, "investment", "I", "2025-03", 500)
    _add(session, u, "credit_card", "Card", "2025-03", 30)
    r = run_net_worth_query(session, NetWorthQuery(user_id=u))
    assert r.snapshot is not None
    assert r.snapshot.assets_minor == 850
    assert r.snapshot.liabilities_minor == 30
    assert r.snapshot.net_worth_minor == 820
    assert [c.category for c in r.snapshot.categories] == [
        "checking",
        "savings",
        "investment",
        "credit_card",
    ]
    assert r.snapshot.categories[3].signed_minor == -30
    assert r.snapshot.previous_period is None and r.snapshot.change_minor is None
    assert r.series[0].by_category["credit_card"] == 30


def test_carry_forward(session: Session) -> None:
    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-01", 100)
    _add(session, u, "savings", "S", "2025-01", 10)
    _add(session, u, "savings", "S", "2025-03", 30)
    r = run_net_worth_query(session, NetWorthQuery(user_id=u))
    assert [p.period for p in r.series] == ["2025-01", "2025-02", "2025-03"]
    assert r.periods == ["2025-01", "2025-03"]
    assert [p.net_worth_minor for p in r.series] == [110, 110, 130]
    checking = next(s for s in r.account_series if s.account_name == "C")
    assert [p.carried for p in checking.points] == [False, True, True]
    assert r.snapshot is not None
    assert r.snapshot.change_minor == 20 and r.snapshot.previous_period == "2025-01"
    carried = next(a for a in r.snapshot.accounts if a.account_name == "C")
    assert carried.carried and carried.as_of == "2025-01"

    r2 = run_net_worth_query(session, NetWorthQuery(user_id=u, carry_forward=False))
    assert [p.net_worth_minor for p in r2.series] == [110, 0, 30]
    assert r2.snapshot is not None and r2.snapshot.net_worth_minor == 30


def test_no_value_before_first_entry(session: Session) -> None:
    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-01", 100)
    _add(session, u, "savings", "New", "2025-02", 50)
    r = run_net_worth_query(session, NetWorthQuery(user_id=u))
    new = next(s for s in r.account_series if s.account_name == "New")
    assert [p.period for p in new.points] == ["2025-02"]
    assert r.series[0].net_worth_minor == 100


def test_snapshot_period_selection(session: Session) -> None:
    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-01", 100)
    _add(session, u, "checking", "C", "2025-03", 300)
    r = run_net_worth_query(session, NetWorthQuery(user_id=u, period="2025-01"))
    assert r.snapshot is not None
    assert r.snapshot.period == "2025-01" and r.snapshot.net_worth_minor == 100
    latest = run_net_worth_query(session, NetWorthQuery(user_id=u))
    assert latest.snapshot is not None and latest.snapshot.period == "2025-03"


def test_user_isolation_and_all(session: Session) -> None:
    a, b = _users(session)
    _add(session, a, "checking", "C", "2025-01", 100)
    _add(session, b, "checking", "C", "2025-01", 40)
    only_a = run_net_worth_query(session, NetWorthQuery(user_id=a))
    both = run_net_worth_query(session, NetWorthQuery(user_id=None))
    assert only_a.snapshot is not None and only_a.snapshot.net_worth_minor == 100
    assert both.snapshot is not None and both.snapshot.net_worth_minor == 140
    assert len(both.account_series) == 2


def test_currency_filter(session: Session) -> None:
    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-01", 100)
    _add(session, u, "checking", "E", "2025-01", 7, currency="EUR")
    usd = run_net_worth_query(session, NetWorthQuery(user_id=u))
    eur = run_net_worth_query(session, NetWorthQuery(user_id=u, currency="EUR"))
    assert usd.snapshot is not None and usd.snapshot.net_worth_minor == 100
    assert eur.snapshot is not None and eur.snapshot.net_worth_minor == 7


def test_period_from_to_range(session: Session) -> None:
    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-01", 100)
    _add(session, u, "checking", "C", "2025-04", 400)
    r = run_net_worth_query(
        session, NetWorthQuery(user_id=u, period_from="2025-02", period_to="2025-03")
    )
    assert [p.period for p in r.series] == ["2025-02", "2025-03"]
    assert [p.net_worth_minor for p in r.series] == [100, 100]


@pytest.mark.parametrize("bad", ["2025-13", "2025", "abc", "2025-1"])
def test_bad_period(session: Session, bad: str) -> None:
    with pytest.raises(ValueError):
        run_net_worth_query(session, NetWorthQuery(user_id=None, period=bad))


def test_period_range_only_narrows_and_is_capped(session: Session) -> None:
    from personal_finance_analyzer.analytics.net_worth_query import MAX_SERIES_MONTHS

    u, _ = _users(session)
    _add(session, u, "checking", "C", "2025-03", 100)
    _add(session, u, "checking", "C", "2025-05", 200)
    wide = run_net_worth_query(
        session, NetWorthQuery(user_id=u, period_from="0000-01", period_to="9999-12")
    )
    assert [p.period for p in wide.series] == ["2025-03", "2025-04", "2025-05"]
    assert [p.period for p in wide.account_series[0].points] == ["2025-03", "2025-04", "2025-05"]
    outside = run_net_worth_query(
        session, NetWorthQuery(user_id=u, period_from="2030-01", period_to="2031-01")
    )
    assert outside.series == [] and outside.account_series == []
    inverted = run_net_worth_query(
        session, NetWorthQuery(user_id=u, period_from="2025-05", period_to="2025-03")
    )
    assert inverted.series == []

    _add(session, u, "checking", "Old", "1900-01", 5)  # data spanning > 1200 months
    long = run_net_worth_query(session, NetWorthQuery(user_id=u))
    assert len(long.series) == MAX_SERIES_MONTHS
    assert long.series[-1].period == "2025-05"  # the most recent months are kept
