"""Net-worth aggregation over `balance_entries` (local only; no LLM/HTTP imports).

Data volume is tiny (a handful of accounts x months), so rows are loaded and aggregated in
Python. With `carry_forward` an account's value for month M is its latest entry at or before M,
flagged `carried` when that entry is from an earlier month and `stale` when it is more than
`STALE_AFTER_MONTHS` months old (`months_old` says how many; the balance is still counted: the flag
only tells the UI the figure may be out of date). Age is measured to the requested ``period``, or,
for the default latest snapshot, to the current calendar month (`NetWorthQuery.today`, injectable
for tests), so an account that has not been updated for months is flagged even when it is the
newest entry in the data; a closed account stops counting once a zero balance is entered. An account has no value (not zero) before its
first entry. Account names that differ only in case (compared with Python's ``str.casefold``, never
SQLite's ASCII-only ``lower()``) are one account and the newest spelling is shown. If such
names both have an entry in the *same* month (older data, from before the entry form matched
names), only the most recently entered balance is used and a warning says so: summing would
double count a re-typed balance, and the entry form now updates the existing row instead.
Totals sum magnitudes per category; net worth = assets - liabilities.

The series covers the months of the data: ``period_from`` / ``period_to`` can only narrow the range
between the first and last entry (never widen it), and at most `MAX_SERIES_MONTHS` months, the most
recent ones, are returned, so a request cannot make the server build a 10,000-year series.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.core.net_worth import (
    ASSET_CATEGORIES,
    BALANCE_CATEGORIES,
    LIABILITY_CATEGORIES,
    signed_balance,
)
from personal_finance_analyzer.db.models import BalanceEntry

_PERIOD_RE = re.compile(r"^(\d{4})-(\d{2})$")
#: Upper bound on the number of monthly points in a series.
MAX_SERIES_MONTHS = 1200
#: A carried-forward balance older than this many months is flagged `stale`.
STALE_AFTER_MONTHS = 3


def _today() -> date:
    return date.today()


@dataclass(frozen=True)
class NetWorthQuery:
    user_id: int | None
    currency: str = "USD"
    period: str | None = None  # snapshot period "YYYY-MM"
    period_from: str | None = None
    period_to: str | None = None
    carry_forward: bool = True
    #: "Now" for staleness of the default (latest) snapshot; None = the real current date.
    today: date | None = None


@dataclass(frozen=True)
class CategoryTotal:
    category: str
    balance_minor: int
    signed_minor: int


@dataclass(frozen=True)
class AccountBalance:
    user_id: int
    category: str
    account_name: str
    balance_minor: int
    signed_minor: int
    as_of: str
    carried: bool
    stale: bool
    months_old: int


@dataclass(frozen=True)
class Point:
    period: str
    by_category: dict[str, int]
    assets_minor: int
    liabilities_minor: int
    net_worth_minor: int


@dataclass(frozen=True)
class AccountPoint:
    period: str
    balance_minor: int
    carried: bool


@dataclass(frozen=True)
class AccountSeries:
    user_id: int
    category: str
    account_name: str
    points: list[AccountPoint]


@dataclass(frozen=True)
class Snapshot:
    period: str
    assets_minor: int
    liabilities_minor: int
    net_worth_minor: int
    previous_period: str | None
    change_minor: int | None
    categories: list[CategoryTotal]
    accounts: list[AccountBalance]


@dataclass(frozen=True)
class NetWorthResult:
    currency: str
    user_id: int | None
    periods: list[str]
    series: list[Point]
    account_series: list[AccountSeries]
    snapshot: Snapshot | None
    warnings: list[str] = field(default_factory=list)


def parse_period(s: str) -> tuple[int, int]:
    """Parse "YYYY-MM"; ValueError on anything else."""
    m = _PERIOD_RE.match(s)
    if m is None:
        raise ValueError(f"invalid period {s!r}; expected YYYY-MM")
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid period {s!r}: month must be 01-12")
    return year, month


def _idx(year: int, month: int) -> int:
    return year * 12 + month - 1


def _label(idx: int) -> str:
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


_Key = tuple[int, str, str]  # (user_id, category, account_name)


class _Account:
    def __init__(self) -> None:
        self.idxs: list[int] = []
        self.values: list[int] = []

    def add(self, idx: int, value: int) -> bool:
        """Record the balance for month `idx`; True when it replaced one already recorded for that
        month (entries arrive oldest first, ties by id, so the latest entered wins)."""
        if self.idxs and self.idxs[-1] == idx:
            self.values[-1] = value
            return True
        self.idxs.append(idx)
        self.values.append(value)
        return False

    def at(self, idx: int, carry: bool) -> tuple[int, int] | None:
        """(balance, entry_idx) in effect for month `idx`, or None."""
        pos = bisect_right(self.idxs, idx) - 1
        if pos < 0:
            return None
        if not carry and self.idxs[pos] != idx:
            return None
        return self.values[pos], self.idxs[pos]


def _totals(
    accounts: dict[_Key, _Account], idx: int, carry: bool
) -> tuple[dict[str, int], int, int, int]:
    by_cat = dict.fromkeys(BALANCE_CATEGORIES, 0)
    for (_, category, _), acc in accounts.items():
        hit = acc.at(idx, carry)
        if hit is not None:
            by_cat[category] += hit[0]
    assets = sum(by_cat[c] for c in ASSET_CATEGORIES)
    liabilities = sum(by_cat[c] for c in LIABILITY_CATEGORIES)
    return by_cat, assets, liabilities, assets - liabilities


def run_net_worth_query(session: Session, q: NetWorthQuery) -> NetWorthResult:
    # Validate periods first so a bad one fails even on an empty table.
    snap_ym = parse_period(q.period) if q.period else None
    from_ym = parse_period(q.period_from) if q.period_from else None
    to_ym = parse_period(q.period_to) if q.period_to else None

    stmt = (
        select(BalanceEntry)
        .where(BalanceEntry.currency == q.currency)
        .order_by(BalanceEntry.year, BalanceEntry.month, BalanceEntry.id)
    )
    if q.user_id is not None:
        stmt = stmt.where(BalanceEntry.user_id == q.user_id)
    rows = list(session.scalars(stmt))
    if not rows:
        return NetWorthResult(q.currency, q.user_id, [], [], [], None)

    accounts: dict[_Key, _Account] = {}
    names: dict[_Key, str] = {}  # display spelling: the newest entry's (rows are oldest first)
    real: set[int] = set()
    warnings: list[str] = []
    for r in rows:
        i = _idx(r.year, r.month)
        real.add(i)
        key = (r.user_id, r.category, r.account_name.casefold())
        names[key] = r.account_name
        if accounts.setdefault(key, _Account()).add(i, r.balance_minor):
            warnings.append(
                f"{r.account_name!r} has more than one entry in {_label(i)} whose names differ "
                "only in letter case; only the most recently entered balance is used."
            )
    real_sorted = sorted(real)
    carry = q.carry_forward

    start = max(_idx(*from_ym), real_sorted[0]) if from_ym else real_sorted[0]
    end = min(_idx(*to_ym), real_sorted[-1]) if to_ym else real_sorted[-1]
    start = max(start, end - MAX_SERIES_MONTHS + 1)
    month_range = range(start, end + 1)  # empty when from > to or the window misses the data

    series: list[Point] = []
    for i in month_range:
        by_cat, assets, liabilities, net = _totals(accounts, i, carry)
        series.append(Point(_label(i), by_cat, assets, liabilities, net))

    order = {c: n for n, c in enumerate(BALANCE_CATEGORIES)}
    account_series: list[AccountSeries] = []
    for key in sorted(accounts, key=lambda k: (order[k[1]], k[0], k[2])):
        acc = accounts[key]
        points = []
        for i in month_range:
            hit = acc.at(i, carry)
            if hit is not None:
                points.append(AccountPoint(_label(i), hit[0], hit[1] != i))
        if points:
            account_series.append(AccountSeries(key[0], key[1], names[key], points))

    snap_idx = _idx(*snap_ym) if snap_ym else real_sorted[-1]
    # An explicit period is judged as of that period; the default snapshot as of the current month.
    now = q.today or _today()
    age_idx = snap_idx if snap_ym else max(snap_idx, _idx(now.year, now.month))
    by_cat, assets, liabilities, net = _totals(accounts, snap_idx, carry)
    previous = [i for i in real_sorted if i < snap_idx]
    prev_idx = previous[-1] if previous else None
    change = net - _totals(accounts, prev_idx, carry)[3] if prev_idx is not None else None
    balances: list[AccountBalance] = []
    for key in sorted(accounts, key=lambda k: (order[k[1]], k[0], k[2])):
        hit = accounts[key].at(snap_idx, carry)
        if hit is not None:
            balances.append(
                AccountBalance(
                    user_id=key[0],
                    category=key[1],
                    account_name=names[key],
                    balance_minor=hit[0],
                    signed_minor=signed_balance(key[1], hit[0]),
                    as_of=_label(hit[1]),
                    carried=hit[1] != snap_idx,
                    stale=age_idx - hit[1] > STALE_AFTER_MONTHS,
                    months_old=age_idx - hit[1],
                )
            )
    snapshot = Snapshot(
        period=_label(snap_idx),
        assets_minor=assets,
        liabilities_minor=liabilities,
        net_worth_minor=net,
        previous_period=_label(prev_idx) if prev_idx is not None else None,
        change_minor=change,
        categories=[CategoryTotal(c, by_cat[c], signed_balance(c, by_cat[c])) for c in order],
        accounts=balances,
    )
    return NetWorthResult(
        currency=q.currency,
        user_id=q.user_id,
        periods=[_label(i) for i in real_sorted],
        series=series,
        account_series=account_series,
        snapshot=snapshot,
        warnings=list(dict.fromkeys(warnings)),
    )
