"""Federal income tax calculations (tax years 2022-2026).

Pure arithmetic over the parameters of `core/tax_params.py` (bundled ``tax_data/federal_params.yaml``,
optionally refreshed or overridden locally; see that module). No database, no network, and no LLM
import. Treasury Fiscal Data does not publish brackets or deductions, so the figures come from the
IRS inflation-adjustment revenue procedures and ship with the package.

Used by the per-user estimate (`analytics/tax_estimate.py`) and by the 1040 parser's line-16
ordinary/preferential split (`ingest/tax/estimate.py` re-exports the split API).

Units: every amount is an integer number of minor units (cents). Tax results are whole dollars
(multiples of 100) like the printed form. For taxable income under $100,000 the IRS Tax Table
(tax computed at the midpoint of each $25/$50 row) is reproduced; at or above $100,000 the Tax
Computation Worksheet (exact bracket arithmetic) is used. Income is floored to whole dollars.

Out of scope (the 1040 split is flagged ``approximate`` by callers when the printed tax disagrees):
Schedule D Tax Worksheet (28% collectibles gain, unrecaptured section 1250 gain), Form 4952
investment-interest election, Form 8814/4972 tax, foreign earned income exclusion.

Public API::

    supported_years() -> tuple[int, ...]
    params_for(tax_year) -> FederalParams | None            # everything for one year
    has_table(tax_year, filing_status) -> bool
    bracket_tax(tax_year, filing_status, taxable_income_minor) -> int | None       # exact, cents
    regular_tax(tax_year, filing_status, taxable_income_minor) -> int | None       # Tax Table / worksheet, whole dollars
    worksheet_tax(...) -> int | None                                               # line 25 of the worksheet
    estimate_tax_split(...) -> TaxSplit | None
    marginal_rate_bp(...) / preferential_rate_bp(...) -> int | None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from personal_finance_analyzer.core.tax_params import FILING_STATUSES, FederalParams, Table, resolve

__all__ = [
    "APPROXIMATE_TOLERANCE_MINOR",
    "FILING_STATUSES",
    "FederalParams",
    "TaxSplit",
    "bracket_tax",
    "estimate_tax_split",
    "has_table",
    "marginal_rate_bp",
    "params_for",
    "preferential_rate_bp",
    "regular_tax",
    "supported_years",
    "worksheet_tax",
]

_CENTS = 100
#: The IRS Tax Table covers taxable income below this many dollars.
_TAX_TABLE_LIMIT = 100_000
_BP = 10_000
_FIFTEEN_BP = 1_500
_TWENTY_BP = 2_000
#: Printed tax may differ from the recomputation by this much (dollars) before a split is
#: reported as approximate.
APPROXIMATE_TOLERANCE_MINOR = 2 * _CENTS


@dataclass(frozen=True, slots=True)
class TaxSplit:
    """Result of `estimate_tax_split`, whole-dollar minor units, both non-negative.

    `approximate` is True when the printed income tax is more than $2 away from the worksheet
    recomputation (so the split is only indicative, e.g. Schedule D worksheet or 28% gains).
    """

    ordinary_minor: int
    preferential_minor: int
    basis: Literal["worksheet"] = "worksheet"
    approximate: bool = False


def supported_years() -> tuple[int, ...]:
    """Tax years with parameters, ascending."""
    return tuple(sorted(resolve().years))


def params_for(tax_year: int) -> FederalParams | None:
    """The parameters of `tax_year`, or `None` for a year the package has none for."""
    return resolve().years.get(tax_year)


def _table(tax_year: int, filing_status: str) -> Table | None:
    params = params_for(tax_year)
    return None if params is None else params.tables.get(filing_status)


def has_table(tax_year: int, filing_status: str) -> bool:
    """True when `tax_year`/`filing_status` has a bracket table (``qss`` shares ``mfj``)."""
    return _table(tax_year, filing_status) is not None


# --- tax computation --------------------------------------------------------------------------------


def _round_half_up_div(numerator: int, denominator: int) -> int:
    return (numerator * 2 + denominator) // (denominator * 2)


def _bracket_tax_cents(table: Table, income: int) -> int:
    """Exact bracket arithmetic on `income` cents, rounded half up to a whole cent."""
    if income <= 0:
        return 0
    total_times_bp = 0
    brackets = table.brackets
    for i, (lower, rate) in enumerate(brackets):
        if income <= lower:
            break
        upper = brackets[i + 1][0] if i + 1 < len(brackets) else income
        total_times_bp += (min(income, upper) - lower) * rate
    return _round_half_up_div(total_times_bp, _BP)


def _regular_tax(table: Table, income: int) -> int:
    """Whole-dollar tax in cents: Tax Table midpoint rule below $100,000, else exact."""
    dollars = max(income, 0) // _CENTS
    if dollars < _TAX_TABLE_LIMIT:
        if dollars < 5:
            return 0
        if dollars < 15:
            mid_cents = 1_000
        elif dollars < 25:
            mid_cents = 2_000
        elif dollars < 3_000:
            mid_cents = (dollars // 25 * 25) * _CENTS + 1_250
        else:
            mid_cents = (dollars // 50 * 50) * _CENTS + 2_500
        tax = _bracket_tax_cents(table, mid_cents)
    else:
        tax = _bracket_tax_cents(table, dollars * _CENTS)
    return _round_half_up_div(tax, _CENTS) * _CENTS


def bracket_tax(tax_year: int, filing_status: str, taxable_income_minor: int) -> int | None:
    """Exact ordinary-rate tax on `taxable_income_minor` in cents (no Tax Table rounding);
    `None` without a table for the year/status."""
    table = _table(tax_year, filing_status)
    return None if table is None else _bracket_tax_cents(table, taxable_income_minor)


def regular_tax(tax_year: int, filing_status: str, taxable_income_minor: int) -> int | None:
    """Tax as printed on the form for ordinary income: Tax Table below $100,000 of taxable
    income, Tax Computation Worksheet above; whole dollars in minor units. `None` without a
    table."""
    table = _table(tax_year, filing_status)
    return None if table is None else _regular_tax(table, taxable_income_minor)


def _worksheet(table: Table, taxable: int, qualified: int, net_ltcg: int) -> tuple[int, int]:
    """The worksheet. Returns (line 25 tax, line 22 tax on the ordinary part); cents."""
    line1 = max(taxable, 0)
    line6 = min(line1, max(qualified, 0) + max(net_ltcg, 0))  # lines 2-6 (no Form 4952)
    line7 = max(0, line1 - line6)
    line8 = min(line1, table.ltcg_0_to)
    line9 = min(line7, line8)
    line10 = line8 - line9  # taxed at 0%
    line11 = line6
    line13 = line11 - line10
    line14 = min(line1, table.ltcg_15_to)
    line16 = max(0, line14 - (line7 + line10))
    line17 = min(line13, line16)  # taxed at 15%
    line18 = _round_half_up_div(line17 * _FIFTEEN_BP, _BP * _CENTS) * _CENTS
    line20 = line11 - (line10 + line17)  # taxed at 20%
    line21 = _round_half_up_div(line20 * _TWENTY_BP, _BP * _CENTS) * _CENTS
    line22 = _regular_tax(table, line7)
    line23 = line18 + line21 + line22
    line24 = _regular_tax(table, line1)
    return min(line23, line24), line22


#: Employee Medicare rate and the rounding slack when reading the Additional Medicare Tax
#: withheld out of W-2 box 6.
_EMPLOYEE_MEDICARE_RATE_BP = 145
_WITHHELD_TOLERANCE_MINOR = 100


def additional_medicare_withheld(medicare_tax_minor: int, medicare_wages_minor: int) -> int:
    """The Additional Medicare Tax inside W-2 box 6 totals: the box 6 tax beyond 1.45% of Medicare
    wages (employers withhold the extra 0.9% on wages over $200,000, whatever the filing status).
    Medicare wages (box 5) are not stored, so callers pass an approximation (box 1 wages plus box
    12 deferrals). A difference under $1 is rounding noise and reads as 0.
    """
    extra = medicare_tax_minor - (
        (max(medicare_wages_minor, 0) * _EMPLOYEE_MEDICARE_RATE_BP + 5_000) // 10_000
    )
    return extra if extra > _WITHHELD_TOLERANCE_MINOR else 0


def worksheet_tax(
    *,
    tax_year: int,
    filing_status: str,
    taxable_income_minor: int,
    qualified_dividends_minor: int,
    net_ltcg_minor: int,
) -> int | None:
    """Line 25 of the Qualified Dividends and Capital Gain Tax Worksheet (Form 1040 line 16
    when the worksheet applies), whole dollars in minor units; `None` without a table."""
    table = _table(tax_year, filing_status)
    if table is None:
        return None
    return _worksheet(table, taxable_income_minor, qualified_dividends_minor, net_ltcg_minor)[0]


def estimate_tax_split(
    *,
    tax_year: int,
    filing_status: str,
    taxable_income_minor: int,
    qualified_dividends_minor: int,
    net_ltcg_minor: int,
    income_tax_minor: int,
) -> TaxSplit | None:
    """Split `income_tax_minor` (Form 1040 line 16, NOT line 24) into ordinary and preferential
    parts.

    `qualified_dividends_minor`: line 3a. `net_ltcg_minor`: lesser of Schedule D lines 15 and 16
    when Schedule D is present, else line 7 when positive (negative values count as 0).
    Preferential income is clamped to taxable income; ordinary tax is the worksheet's line 22
    (tax on taxable income minus preferential income); preferential tax is
    ``max(income_tax - ordinary, 0)`` and ordinary is capped at the printed tax, so the two
    always sum to the printed tax.

    Returns `None` when there is no table for `tax_year`/`filing_status`, or an input is
    negative (taxable income, qualified dividends, tax). `approximate` is set when the printed
    tax is more than $2 from the worksheet recomputation.
    """
    table = _table(tax_year, filing_status)
    if table is None or taxable_income_minor < 0 or qualified_dividends_minor < 0:
        return None
    if income_tax_minor < 0:
        return None
    computed, line22 = _worksheet(
        table, taxable_income_minor, qualified_dividends_minor, net_ltcg_minor
    )
    ordinary = min(line22, income_tax_minor)
    preferential = max(income_tax_minor - ordinary, 0)
    return TaxSplit(
        ordinary_minor=ordinary,
        preferential_minor=preferential,
        approximate=abs(computed - income_tax_minor) > APPROXIMATE_TOLERANCE_MINOR,
    )


def marginal_rate_bp(tax_year: int, filing_status: str, ordinary_taxable_minor: int) -> int | None:
    """Ordinary bracket rate (basis points) that applies to the next dollar on top of
    `ordinary_taxable_minor` of ordinary taxable income; `None` without a table."""
    table = _table(tax_year, filing_status)
    if table is None:
        return None
    rate = table.brackets[0][1]
    for lower, bracket_rate in table.brackets:
        if ordinary_taxable_minor >= lower:
            rate = bracket_rate
    return rate


def preferential_rate_bp(
    tax_year: int, filing_status: str, taxable_income_minor: int
) -> int | None:
    """Long-term gain / qualified dividend rate (0, 1500 or 2000 basis points) that applies to the
    top dollar of `taxable_income_minor`; `None` without a table."""
    table = _table(tax_year, filing_status)
    if table is None:
        return None
    if taxable_income_minor <= table.ltcg_0_to:
        return 0
    return _FIFTEEN_BP if taxable_income_minor <= table.ltcg_15_to else _TWENTY_BP
