"""Net-worth query: stale carried balances and case-insensitive account identity."""

from __future__ import annotations

from sqlalchemy.orm import Session

from spend_analyzer.analytics.net_worth_query import (
    STALE_AFTER_MONTHS,
    NetWorthQuery,
    parse_period,
    run_net_worth_query,
)
from spend_analyzer.db.models import BalanceEntry, User


def _user(session: Session) -> int:
    user = User(name="A")
    session.add(user)
    session.flush()
    return user.id


def _add(session: Session, user_id: int, category: str, name: str, period: str, bal: int) -> None:
    y, m = parse_period(period)
    session.add(
        BalanceEntry(
            user_id=user_id,
            category=category,
            account_name=name,
            year=y,
            month=m,
            balance_minor=bal,
        )
    )
    session.flush()


def test_stale_means_carried_more_than_three_months(session: Session) -> None:
    assert STALE_AFTER_MONTHS == 3
    a = _user(session)
    _add(session, a, "savings", "S", "2025-01", 100)
    _add(session, a, "checking", "C", "2025-04", 10)
    _add(session, a, "investment", "I", "2025-05", 5)
    snap = run_net_worth_query(session, NetWorthQuery(user_id=a)).snapshot
    assert snap is not None
    by = {x.account_name: x for x in snap.accounts}
    assert (by["S"].months_old, by["S"].stale, by["S"].carried) == (4, True, True)
    assert (by["C"].months_old, by["C"].stale, by["C"].carried) == (1, False, True)
    assert (by["I"].months_old, by["I"].stale, by["I"].carried) == (0, False, False)
    assert snap.assets_minor == 115  # staleness never changes the totals
    exact = run_net_worth_query(session, NetWorthQuery(user_id=a, period="2025-04")).snapshot
    assert exact is not None
    assert {x.account_name: x.stale for x in exact.accounts} == {"S": False, "C": False}


def test_account_names_differing_only_in_case_are_one_account(session: Session) -> None:
    """Legacy rows entered before names were matched across months must not be counted twice."""
    a = _user(session)
    _add(session, a, "checking", "Chase", "2025-01", 100)
    _add(session, a, "checking", "chase", "2025-02", 250)
    snap = run_net_worth_query(session, NetWorthQuery(user_id=a)).snapshot
    assert snap is not None
    assert snap.assets_minor == 250
    assert [x.account_name for x in snap.accounts] == ["chase"]  # the newest spelling


def test_case_variants_in_the_same_month_keep_the_latest_entry_and_warn(session: Session) -> None:
    """Two legacy rows for one account in one month: the later-entered balance is used (summing
    would double count a re-typed balance) and the result carries a warning."""
    a = _user(session)
    _add(session, a, "checking", "Chase", "2025-03", 100)
    _add(session, a, "checking", "chase", "2025-03", 250)  # entered later: higher id
    result = run_net_worth_query(session, NetWorthQuery(user_id=a))
    assert result.snapshot is not None
    assert result.snapshot.assets_minor == 250
    assert [x.account_name for x in result.snapshot.accounts] == ["chase"]
    assert len(result.warnings) == 1 and "2025-03" in result.warnings[0]
    other = User(name="B")
    session.add(other)
    session.flush()
    clean = other.id
    _add(session, clean, "checking", "Chase", "2025-03", 100)
    assert run_net_worth_query(session, NetWorthQuery(user_id=clean)).warnings == []


def test_non_ascii_case_variants_are_one_account(session: Session) -> None:
    """The match is Python's casefold, which SQLite's ASCII-only lower() would miss."""
    a = _user(session)
    _add(session, a, "savings", "Ärzte Sparkasse", "2025-01", 100)
    _add(session, a, "savings", "ärzte sparkasse", "2025-02", 300)
    _add(session, a, "savings", "Straße", "2025-01", 5)
    _add(session, a, "savings", "STRASSE", "2025-02", 7)  # casefold: "ß" -> "ss"
    snap = run_net_worth_query(session, NetWorthQuery(user_id=a)).snapshot
    assert snap is not None
    assert snap.assets_minor == 307 and len(snap.accounts) == 2
