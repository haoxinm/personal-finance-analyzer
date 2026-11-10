"""Per-year history and suggested defaults for the net worth forecast (local only; no LLM/HTTP).

Read-only aggregation over three existing sources, joined by calendar year:

- **Net worth** (year-end): per person, the last month with data in that year from
  `run_net_worth_query` (balance entries, carried forward per account), else that person's manual
  value from `net_worth_history`; a person's own entries *in that year* win over a manual value for
  the same year, and a manual value for such a year is reported next to it
  (`manual_net_worth_minor`) but never used. A year in which the person recorded nothing is not
  "a year with entries" just because an earlier balance carries forward: the manual value wins,
  and with no manual value the carried balance fills in, labelled ``"carried"`` (`as_of` is the
  month of the real entry it comes from). With ``user_id=None`` (all users) the choice is made per
  person and per year, then summed, so one person's entries never hide another person's manual
  value; the row's `source` is ``"mixed"`` when several kinds contributed.
- **Income and tax**: `run_tax_summary`. `income_minor` is the gross taxable income on the imported
  forms (`total_income_minor`, so it includes interest, dividends and capital gains) and
  `earned_income_minor` is its wages + self-employment part; tax is the summary's headline total
  (`headline.total_minor`: federal on the headline basis [return, else estimate, else withholding]
  + FICA + state), the same figure the Income & tax screen shows as "Total tax" and divides by
  `total_income_minor` for its all-in rate.
- **Spending**: `run_query` over the default spend kinds, refunds netted, grouped by month and
  summed per year. ``spending_months`` is how many calendar months of that year have any
  transaction, so a partial year is visible (and annualised only for the suggested default).

USD only: the tax tables and balance entries are USD-only, and spending is filtered to USD.

Suggested defaults: the latest net worth; **earned** income (wages + self-employment: interest,
dividends and gains are left out because the forecast's investment return already compounds them),
the tax rate (all-in tax / earned income, so the year's tax dollars are reproduced on the earned
income alone) and spending from the latest year that has them (a partial spending year is
annualised when it covers at least `_MIN_ANNUALISE_MONTHS` months); growth rates are compound
annual rates between the first and last year with data (earned income; spending: complete years
only), ``None`` with fewer than two such years or a non-positive start. Each default carries the
year it was measured in (`net_worth_year`, `income_year`, `spending_year`), because they can
differ; the screen rebases income and spending to the chosen starting year.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from personal_finance_analyzer.analytics.net_worth_query import NetWorthQuery, run_net_worth_query
from personal_finance_analyzer.analytics.query import SpendQuery, run_query
from personal_finance_analyzer.analytics.tax_query import IncomeBasis, TaxQuery, run_tax_summary
from personal_finance_analyzer.core.net_worth import signed_balance
from personal_finance_analyzer.core.tax_types import IncomeCategory
from personal_finance_analyzer.db.models import NetWorthHistory

CURRENCY: Final = "USD"
#: A partial spending year needs at least this many months to be scaled up to a full year.
_MIN_ANNUALISE_MONTHS = 3
#: Income categories that count as earned: the rest (interest, dividends, gains) is what the
#: forecast's investment return already models.
_EARNED_CATEGORIES: Final[tuple[IncomeCategory, ...]] = ("salary_wages", "self_employment")


@dataclass(frozen=True)
class HistoryRow:
    year: int
    net_worth_minor: int | None
    source: str | None  # "entries" | "manual" | "carried" | "mixed" (all-users view only) | None
    as_of: str | None
    manual_net_worth_minor: int | None
    manual_note: str | None
    income_minor: int | None
    earned_income_minor: int | None
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
    income_growth_clamped: bool
    spending_growth_clamped: bool
    savings_rate_pct: float | None


@dataclass(frozen=True)
class ForecastBaseline:
    currency: str
    user_id: int | None
    history: list[HistoryRow]
    defaults: Defaults


@dataclass(frozen=True)
class _NetWorthYear:
    net_worth_minor: int
    source: str  # "entries" | "manual" | "carried" | "mixed"
    as_of: str | None
    manual_minor: int | None
    manual_note: str | None


@dataclass(frozen=True)
class _EntryYear:
    net_worth_minor: int
    period: str  # "YYYY-MM": the year-end month the value is read at
    real: bool  # the person has a balance entry of their own in that year
    as_of: str | None  # the latest real entry month at or before the period


def _entries_by_user_year(
    session: Session, user_id: int | None
) -> dict[int, dict[int, _EntryYear]]:
    """user -> year -> that user's year-end net worth from balance entries.

    The year-end month is the last month of the (combined) series in that year, so with several
    users a person whose entries stop early is carried forward exactly as the net-worth screen
    does. `real` says whether that person recorded a balance in the year itself: a value that is
    only carried forward from an earlier year is not evidence about the year, so a manual value
    for it wins (see `_net_worth_by_year`)."""
    result = run_net_worth_query(session, NetWorthQuery(user_id=user_id, currency=CURRENCY))
    year_end: dict[int, str] = {}
    for point in result.series:  # chronological, so the last point of a year wins
        year_end[int(point.period[:4])] = point.period
    ends = set(year_end.values())
    totals: dict[tuple[int, str], int] = defaultdict(int)
    real_years: set[tuple[int, int]] = set()
    real_periods: dict[int, set[str]] = defaultdict(set)
    for acc in result.account_series:
        for pt in acc.points:
            if not pt.carried:
                real_years.add((acc.user_id, int(pt.period[:4])))
                real_periods[acc.user_id].add(pt.period)
            if pt.period in ends:
                totals[(acc.user_id, pt.period)] += signed_balance(acc.category, pt.balance_minor)
    out: dict[int, dict[int, _EntryYear]] = defaultdict(dict)
    for (uid, period), total in sorted(totals.items()):
        year = int(period[:4])
        as_of = max((p for p in real_periods[uid] if p <= period), default=None)
        out[uid][year] = _EntryYear(total, period, (uid, year) in real_years, as_of)
    return out


def _manual_by_user_year(
    session: Session, user_id: int | None
) -> dict[int, dict[int, tuple[int, str | None]]]:
    """user -> year -> (manual net worth, note)."""
    stmt = select(NetWorthHistory).order_by(NetWorthHistory.year, NetWorthHistory.user_id)
    if user_id is not None:
        stmt = stmt.where(NetWorthHistory.user_id == user_id)
    out: dict[int, dict[int, tuple[int, str | None]]] = defaultdict(dict)
    for row in session.scalars(stmt):
        out[row.user_id][row.year] = (row.amount_minor, row.note)
    return out


def _net_worth_by_year(session: Session, user_id: int | None) -> dict[int, _NetWorthYear]:
    """year -> year-end net worth, decided **per person per year**, then summed.

    For each person and year: their own balance entries in that year win; else their manual value;
    else (nothing recorded for the year) the balance carried forward from their earlier entries,
    labelled ``"carried"``. The note is kept only for a single user's value."""
    entries = _entries_by_user_year(session, user_id)
    manual = _manual_by_user_year(session, user_id)
    years = {y for per_user in entries.values() for y in per_user}
    years |= {y for per_user in manual.values() for y in per_user}
    out: dict[int, _NetWorthYear] = {}
    for year in sorted(years):
        total = 0
        kinds: set[str] = set()
        periods: list[str] = []
        manual_total: int | None = None
        note: str | None = None
        for uid in sorted({*entries, *manual}):
            e = entries.get(uid, {}).get(year)
            m = manual.get(uid, {}).get(year)
            if m is not None:
                manual_total = (manual_total or 0) + m[0]
                if user_id is not None:
                    note = m[1]
            if e is not None and e.real:
                total += e.net_worth_minor
                kinds.add("entries")
                periods.append(e.period)
            elif m is not None:
                total += m[0]
                kinds.add("manual")
            elif e is not None:
                total += e.net_worth_minor
                kinds.add("carried")
                if e.as_of is not None:
                    periods.append(e.as_of)
        if "entries" in kinds:  # a carried balance alongside real entries is how the screen works
            kinds.discard("carried")
        source = next(iter(kinds)) if len(kinds) == 1 else "mixed"
        as_of = max(periods) if periods else None
        out[year] = _NetWorthYear(total, source, as_of, manual_total, note)
    return out


def _tax_by_year(
    session: Session, user_id: int | None
) -> dict[int, tuple[int, int, int | None, IncomeBasis]]:
    """year -> (gross income, earned income, all-in tax or None, basis); years without income are
    left out. Earned income is wages + self-employment."""
    query = TaxQuery(user_ids=[user_id] if user_id is not None else None, currency=CURRENCY)
    out: dict[int, tuple[int, int, int | None, IncomeBasis]] = {}
    for s in run_tax_summary(session, query):
        if s.income_basis == "none" or s.total_income_minor <= 0:
            continue
        tax = s.headline.total_minor if s.headline is not None else None
        earned = sum(s.income_by_category.get(c, 0) for c in _EARNED_CATEGORIES)
        out[s.tax_year] = (s.total_income_minor, earned, tax, s.income_basis)
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


#: The forecast API accepts growth rates in this range; observed rates are clamped to it.
MAX_GROWTH_PCT: Final = 50.0


def _growth(values: dict[int, int]) -> tuple[float | None, bool]:
    """(rate, clamped?). A rate outside +/-`MAX_GROWTH_PCT` (a partial first year, say $10k then
    $150k, gives ~287%) is clamped to it so "Reset to data" stays savable."""
    years = sorted(values)
    if len(years) < 2:
        return None, False
    rate = cagr_pct(values[years[0]], values[years[-1]], years[-1] - years[0])
    if rate is None:
        return None, False
    clamped = min(max(rate, -MAX_GROWTH_PCT), MAX_GROWTH_PCT)
    return clamped, clamped != rate


def run_forecast_baseline(session: Session, user_id: int | None) -> ForecastBaseline:
    """History per year (ascending) and the defaults derived from it.

    Args:
        user_id: One user, or None for everyone combined.
    """
    net_worth_by_year = _net_worth_by_year(session, user_id)
    tax = _tax_by_year(session, user_id)
    spending = _spending_by_year(session, user_id)

    rows: list[HistoryRow] = []
    for year in sorted({*net_worth_by_year, *tax, *spending}):
        nw = net_worth_by_year.get(year)
        income, earned, tax_minor, basis = tax.get(year, (None, None, None, None))
        spent, months = spending.get(year, (None, 0))
        savings = (
            income - tax_minor - spent
            if income is not None and tax_minor is not None and spent is not None and months == 12
            else None
        )
        rows.append(
            HistoryRow(
                year=year,
                net_worth_minor=nw.net_worth_minor if nw else None,
                source=nw.source if nw else None,
                as_of=nw.as_of if nw else None,
                manual_net_worth_minor=nw.manual_minor if nw else None,
                manual_note=nw.manual_note if nw else None,
                income_minor=income,
                earned_income_minor=earned,
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
    latest_income = next((r for r in reversed(rows) if r.earned_income_minor), None)
    tax_rate: float | None = None
    if latest_income and latest_income.earned_income_minor and latest_income.tax_minor is not None:
        # All the year's tax is attributed to earned income (the only income the forecast taxes).
        tax_rate = round(
            min(latest_income.tax_minor / latest_income.earned_income_minor * 100, 100.0), 2
        )

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

    earned_by_year = {
        r.year: r.earned_income_minor
        for r in rows
        if r.earned_income_minor is not None and r.earned_income_minor > 0
    }
    complete_spending = {
        r.year: r.spending_minor
        for r in rows
        if r.spending_months == 12 and r.spending_minor is not None and r.spending_minor > 0
    }
    income_growth, income_clamped = _growth(earned_by_year)
    spending_growth, spending_clamped = _growth(complete_spending)
    return Defaults(
        net_worth_minor=latest_nw.net_worth_minor if latest_nw else None,
        net_worth_year=latest_nw.year if latest_nw else None,
        annual_income_minor=latest_income.earned_income_minor if latest_income else None,
        income_year=latest_income.year if latest_income else None,
        effective_tax_rate_pct=tax_rate,
        annual_spending_minor=spending[0] if spending else None,
        spending_year=spending_year,
        spending_annualized=spending[1] if spending else False,
        income_growth_pct=income_growth,
        spending_growth_pct=spending_growth,
        income_growth_clamped=income_clamped,
        spending_growth_clamped=spending_clamped,
        savings_rate_pct=savings_rate,
    )
