/**
 * The net worth projection: a pure, deterministic function of the inputs, so it recomputes
 * instantly as a slider moves. Amounts are USD minor units (cents) as plain numbers (kept
 * fractional internally, rounded only when displayed), rates are percent per year (3 = 3%).
 * Documented in `docs/FORECAST.md`.
 *
 * Each projected year `t` (from `baseYear + 1` to `targetYear`), with `n = t - baseYear`:
 *
 *   income_t    = salary_0 * (1 + income growth)^n                  while t <= retirementYear
 *               = retirementIncome * (1 + inflation)^n              after it (base-year dollars, indexed)
 *   tax_t       = income_t * tax rate                               one flat effective rate
 *   spending_t  = max(0, spending_{t-1} * (1 + spending growth) + fixed annual change)
 *   savings_t   = income_t - tax_t - spending_t                     negative = drawdown
 *   return_t    = return rate * max(0, netWorth_{t-1} + savings_t / 2)   mid-year convention
 *   netWorth_t  = netWorth_{t-1} + savings_t + return_t + events_t
 *
 * `salary_0` and `spending_0` are the base year's levels, so the first projected year is already
 * one step of growth ahead. A negative balance neither earns nor accrues interest. One-off events
 * land at year end and earn nothing in their own year. "Real" values divide by
 * `(1 + inflation)^n`, i.e. they are in base-year dollars.
 */

export interface ForecastEvent {
  year: number;
  /** Signed: negative = money out (a down payment), positive = money in (an inheritance). */
  amountMinor: number;
  label: string;
}

export interface ForecastInputs {
  /** Year of `startNetWorthMinor`; the first projected year is `baseYear + 1`. */
  baseYear: number;
  targetYear: number;
  startNetWorthMinor: number;
  /** Gross income in the base year. */
  annualIncomeMinor: number;
  incomeGrowthPct: number;
  /** Flat effective rate on all income, 0-100. */
  taxRatePct: number;
  /** Spending in the base year. */
  annualSpendingMinor: number;
  spendingGrowthPct: number;
  /** Added to spending every year on top of the percentage growth; may be negative. */
  spendingChangeMinor: number;
  returnPct: number;
  inflationPct: number;
  /** Last year with salary; `null` = never retire. */
  retirementYear: number | null;
  /** Pre-tax annual income after `retirementYear`, in base-year dollars. */
  retirementIncomeMinor: number;
  events: readonly ForecastEvent[];
}

/** One projected year, nominal dollars. `events` is the net of that year's one-off events. */
export interface ForecastRow {
  year: number;
  income: number;
  tax: number;
  spending: number;
  savings: number;
  investmentReturn: number;
  events: number;
  netWorth: number;
}

export type DollarMode = "nominal" | "real";

/** Longest projection (years after the base year); matches the API's `horizon_years` bound. */
export const MAX_HORIZON_YEARS = 60;

/** Clamps a target year to `baseYear + 1 .. baseYear + MAX_HORIZON_YEARS`. */
export function clampTargetYear(baseYear: number, targetYear: number): number {
  return Math.min(Math.max(Math.round(targetYear), baseYear + 1), baseYear + MAX_HORIZON_YEARS);
}

/** Inflation divisor for `years` after the base year: `(1 + inflation)^years`. */
export function deflator(inflationPct: number, years: number): number {
  return Math.pow(1 + inflationPct / 100, years);
}

/** The projection, one row per year from `baseYear + 1` to the (clamped) target year. */
export function project(inputs: ForecastInputs): ForecastRow[] {
  const { baseYear } = inputs;
  const last = clampTargetYear(baseYear, inputs.targetYear);
  const eventsByYear = new Map<number, number>();
  for (const e of inputs.events) {
    eventsByYear.set(e.year, (eventsByYear.get(e.year) ?? 0) + e.amountMinor);
  }

  const rows: ForecastRow[] = [];
  let netWorth = inputs.startNetWorthMinor;
  let spending = inputs.annualSpendingMinor;
  for (let year = baseYear + 1; year <= last; year++) {
    const n = year - baseYear;
    const retired = inputs.retirementYear !== null && year > inputs.retirementYear;
    const income = retired
      ? inputs.retirementIncomeMinor * deflator(inputs.inflationPct, n)
      : inputs.annualIncomeMinor * Math.pow(1 + inputs.incomeGrowthPct / 100, n);
    const tax = (income * inputs.taxRatePct) / 100;
    spending = Math.max(0, spending * (1 + inputs.spendingGrowthPct / 100) + inputs.spendingChangeMinor);
    const savings = income - tax - spending;
    const investmentReturn = (inputs.returnPct / 100) * Math.max(0, netWorth + savings / 2);
    const events = eventsByYear.get(year) ?? 0;
    netWorth = netWorth + savings + investmentReturn + events;
    rows.push({ year, income, tax, spending, savings, investmentReturn, events, netWorth });
  }
  return rows;
}

/** Every money column of `row` in base-year dollars. */
export function toReal(row: ForecastRow, baseYear: number, inflationPct: number): ForecastRow {
  const d = deflator(inflationPct, row.year - baseYear);
  return {
    year: row.year,
    income: row.income / d,
    tax: row.tax / d,
    spending: row.spending / d,
    savings: row.savings / d,
    investmentReturn: row.investmentReturn / d,
    events: row.events / d,
    netWorth: row.netWorth / d,
  };
}

/** `rows` in the requested dollars (the same array for nominal). */
export function rowsIn(
  mode: DollarMode,
  rows: readonly ForecastRow[],
  baseYear: number,
  inflationPct: number,
): ForecastRow[] {
  return mode === "nominal" ? [...rows] : rows.map((r) => toReal(r, baseYear, inflationPct));
}

export interface ForecastSummary {
  targetYear: number;
  netWorthNominal: number;
  netWorthReal: number;
  /** Savings plus one-off events over the horizon (nominal). */
  contributions: number;
  /** Investment return over the horizon (nominal). */
  growth: number;
  /** start + contributions + growth = netWorthNominal. */
  startNetWorth: number;
}

/** Headline figures for the last projected year; with no projected years, the start value. */
export function summarize(inputs: ForecastInputs, rows: readonly ForecastRow[]): ForecastSummary {
  const last = rows[rows.length - 1];
  const contributions = rows.reduce((sum, r) => sum + r.savings + r.events, 0);
  const growth = rows.reduce((sum, r) => sum + r.investmentReturn, 0);
  return {
    targetYear: last?.year ?? inputs.baseYear,
    netWorthNominal: last?.netWorth ?? inputs.startNetWorthMinor,
    netWorthReal: last ? last.netWorth / deflator(inputs.inflationPct, last.year - inputs.baseYear) : inputs.startNetWorthMinor,
    contributions,
    growth,
    startNetWorth: inputs.startNetWorthMinor,
  };
}

export interface GoalResult {
  /** First year the goal is met; the base year when the start already meets it. */
  year: number;
  /** Years after the base year. */
  years: number;
}

/**
 * The first year net worth reaches `goalMinor`, in nominal or base-year ("real") dollars, or
 * `null` when it is not reached by the target year. Net worth already at the goal at the start
 * counts as year 0.
 */
export function yearsToGoal(
  inputs: ForecastInputs,
  rows: readonly ForecastRow[],
  goalMinor: number,
  mode: DollarMode,
): GoalResult | null {
  if (inputs.startNetWorthMinor >= goalMinor) return { year: inputs.baseYear, years: 0 };
  for (const row of rows) {
    const value = mode === "real" ? row.netWorth / deflator(inputs.inflationPct, row.year - inputs.baseYear) : row.netWorth;
    if (value >= goalMinor) return { year: row.year, years: row.year - inputs.baseYear };
  }
  return null;
}
