"""Per-year history and suggested defaults for the net worth forecast (local only; no LLM/HTTP).

Read-only aggregation over three existing sources, joined by calendar year:

- **Net worth** (year-end): the last month with data in that year from `run_net_worth_query`
  (balance entries, carried forward per account), else the user's manual value from
  `net_worth_history`. Entries always win; a manual value for a year with entries is reported next
  to it (`manual_net_worth_minor`) but never used. With ``user_id=None`` (all users) entries are
  summed across users as the net-worth screen does, and manual values are summed per year.
- **Income and tax**: `run_tax_summary`. Income is the gross taxable income on the imported forms
  (`total_income_minor`, so it includes interest, dividends and capital gains); tax is the all-in
  figure (federal + state + local + FICA), rebuilt as ``all_in_rate * total_income_minor``.
- **Spending**: `run_query` over the default spend kinds, refunds netted, grouped by month and
  summed per year. ``spending_months`` is how many calendar months of that year have any
  transaction, so a partial year is visible (and annualised only for the suggested default).

USD only: the tax tables and balance entries are USD-only, and spending is filtered to USD.

Suggested defaults: the latest net worth; income, tax rate and spending from the latest year that
has them (a partial spending year is annualised when it covers at least `_MIN_ANNUALISE_MONTHS`
months); growth rates are compound annual rates between the first and last year with data
(spending: complete years only), ``None`` with fewer than two such years or a non-positive start.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from spend_analyzer.analytics.net_worth_query import NetWorthQuery, run_net_worth_query
from spend_analyzer.analytics.query import SpendQuery, run_query
from spend_analyzer.analytics.tax_query import IncomeBasis, TaxQuery, run_tax_summary
from spend_analyzer.db.models import NetWorthHistory

CURRENCY: Final = "USD"
#: A partial spending year needs at least this many months to be scaled up to a full year.
_MIN_ANNUALISE_MONTHS = 3


@dataclass(frozen=True)
class HistoryRow:
    year: int
    net_worth_minor: int | None
    source: str | None  # "entries" | "manual" | None
    as_of: str | None
    manual_net_worth_minor: int | None
    manual_note: str | None
    income_minor: int | None
    tax_minor: int | None
    income_basis: IncomeBasis | None
    spending_minor: int | None
    spending_months: int
    savings_minor: int | None


@dataclass(frozen=True)
class Defaults:
    net_worth_minor: int | None
    net_worth_year: int | None
    annual_income_minor: int | None
    income_year: int | None
    effective_tax_rate_pct: float | None
    annual_spending_minor: int | None
    spending_year: int | None
    spending_annualized: bool
    income_growth_pct: float | None
    spending_growth_pct: float | None
    savings_rate_pct: float | None


@dataclass(frozen=True)
class ForecastBaseline:
    currency: str
    user_id: int | None
    history: list[HistoryRow]
    defaults: Defaults


def _entries_by_year(session: Session, user_id: int | None) -> dict[int, tuple[int, str]]:
    """year -> (year-end net worth, "YYYY-MM") from balance entries."""
    result = run_net_worth_query(session, NetWorthQuery(user_id=user_id, currency=CURRENCY))
    out: dict[int, tuple[int, str]] = {}
    for point in result.series:  # chronological, so the last point of a year wins
        out[int(point.period[:4])] = (point.net_worth_minor, point.period)
    return out


def _manual_by_year(session: Session, user_id: int | None) -> dict[int, tuple[int, str | None]]:
    """year -> (manual net worth, note). Summed across users for the all-users view (the note is
    kept only for a single user's value)."""
    stmt = select(NetWorthHistory).order_by(NetWorthHistory.year, NetWorthHistory.user_id)
    if user_id is not None:
        stmt = stmt.where(NetWorthHistory.user_id == user_id)
    totals: dict[int, int] = defaultdict(int)
    notes: dict[int, str | None] = {}
    for row in session.scalars(stmt):
        totals[row.year] += row.amount_minor
        notes[row.year] = row.note if user_id is not None else None
    return {y: (totals[y], notes[y]) for y in totals}


def _tax_by_year(
    session: Session, user_id: int | None
) -> dict[int, tuple[int, int | None, IncomeBasis]]:
    """year -> (gross income, all-in tax or None, basis); years without income are left out."""
    query = TaxQuery(user_ids=[user_id] if user_id is not None else None, currency=CURRENCY)
    out: dict[int, tuple[int, int | None, IncomeBasis]] = {}
    for s in run_tax_summary(session, query):
        if s.income_basis == "none" or s.total_income_minor <= 0:
            continue
        tax = round(s.all_in_rate * s.total_income_minor) if s.all_in_rate is not None else None
        out[s.tax_year] = (s.total_income_minor, tax, s.income_basis)
    return out


def _spending_by_year(session: Session, user_id: int | None) -> dict[int, tuple[int, int]]:
    """year -> (net spending, months with any transaction)."""
    rows = run_query(
        session,
        SpendQuery(
            user_ids=[user_id] if user_id is not None else None,
            granularity="month",
            group_by=["period"],
            currency=CURRENCY,
        ),
    )
    totals: dict[int, int] = defaultdict(int)
    months: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        period = str(row["period"])
        year = int(period[:4])
        total = row["total_minor"]
        totals[year] += int(total) if isinstance(total, int) else 0
        months[year].add(period)
    return {y: (totals[y], len(months[y])) for y in totals}


def cagr_pct(first: int, last: int, years: int) -> float | None:
    """Compound annual growth, in percent rounded to 2 dp, from `first` to `last` over `years`.

    None when `years` < 1 or either value is not positive (a growth rate is undefined)."""
    if years < 1 or first <= 0 or last <= 0:
        return None
    ratio: float = (last / first) ** (1.0 / years)
    return round((ratio - 1) * 100, 2)


def _growth(values: dict[int, int]) -> float | None:
    years = sorted(values)
    if len(years) < 2:
        return None
    return cagr_pct(values[years[0]], values[years[-1]], years[-1] - years[0])


def run_forecast_baseline(session: Session, user_id: int | None) -> ForecastBaseline:
    """History per year (ascending) and the defaults derived from it.

    Args:
        user_id: One user, or None for everyone combined.
    """
    entries = _entries_by_year(session, user_id)
    manual = _manual_by_year(session, user_id)
    tax = _tax_by_year(session, user_id)
    spending = _spending_by_year(session, user_id)

    rows: list[HistoryRow] = []
    for year in sorted({*entries, *manual, *tax, *spending}):
        net_worth: int | None = None
        source: str | None = None
        as_of: str | None = None
        if year in entries:
            net_worth, as_of = entries[year]
            source = "entries"
        elif year in manual:
            net_worth, source = manual[year][0], "manual"
        income, tax_minor, basis = tax.get(year, (None, None, None))
        spent, months = spending.get(year, (None, 0))
        savings = (
            income - tax_minor - spent
            if income is not None and tax_minor is not None and spent is not None and months == 12
            else None
        )
        rows.append(
            HistoryRow(
                year=year,
                net_worth_minor=net_worth,
                source=source,
                as_of=as_of,
                manual_net_worth_minor=manual[year][0] if year in manual else None,
                manual_note=manual[year][1] if year in manual else None,
                income_minor=income,
                tax_minor=tax_minor,
                income_basis=basis,
                spending_minor=spent,
                spending_months=months,
                savings_minor=savings,
            )
        )
    return ForecastBaseline(CURRENCY, user_id, rows, _defaults(rows))


def _annual_spending(row: HistoryRow) -> tuple[int, bool] | None:
    """(12-month spending, annualised?) for a year, or None when it has too little data."""
    if row.spending_minor is None:
        return None
    if row.spending_months == 12:
        return row.spending_minor, False
    if row.spending_months >= _MIN_ANNUALISE_MONTHS:
        return round(row.spending_minor / row.spending_months * 12), True
    return None


def _defaults(rows: list[HistoryRow]) -> Defaults:
    latest_nw = next((r for r in reversed(rows) if r.net_worth_minor is not None), None)
    latest_income = next((r for r in reversed(rows) if r.income_minor), None)
    tax_rate: float | None = None
    if latest_income and latest_income.income_minor and latest_income.tax_minor is not None:
        tax_rate = round(latest_income.tax_minor / latest_income.income_minor * 100, 2)

    spending: tuple[int, bool] | None = None
    spending_year: int | None = None
    for row in reversed(rows):
        spending = _annual_spending(row)
        if spending is not None:
            spending_year = row.year
            break

    savings_rate: float | None = None
    latest_savings = next((r for r in reversed(rows) if r.savings_minor is not None), None)
    if latest_savings and latest_savings.savings_minor is not None and latest_savings.income_minor:
        savings_rate = round(latest_savings.savings_minor / latest_savings.income_minor * 100, 2)

    income_by_year = {
        r.year: r.income_minor for r in rows if r.income_minor is not None and r.income_minor > 0
    }
    complete_spending = {
        r.year: r.spending_minor
        for r in rows
        if r.spending_months == 12 and r.spending_minor is not None and r.spending_minor > 0
    }
    return Defaults(
        net_worth_minor=latest_nw.net_worth_minor if latest_nw else None,
        net_worth_year=latest_nw.year if latest_nw else None,
        annual_income_minor=latest_income.income_minor if latest_income else None,
        income_year=latest_income.year if latest_income else None,
        effective_tax_rate_pct=tax_rate,
        annual_spending_minor=spending[0] if spending else None,
        spending_year=spending_year,
        spending_annualized=spending[1] if spending else False,
        income_growth_pct=_growth(income_by_year),
        spending_growth_pct=_growth(complete_spending),
        savings_rate_pct=savings_rate,
    )
