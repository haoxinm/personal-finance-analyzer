import type { components } from "@/api/client";

import { parseBalanceInput } from "../logic";
import { MAX_HORIZON_YEARS, clampTargetYear, type ForecastEvent, type ForecastInputs } from "./forecast-logic";

export type ApiParams = components["schemas"]["ForecastParams"];
export type BuiltinPreset = components["schemas"]["BuiltinForecastPreset"];
export type SavedPreset = components["schemas"]["ForecastPreset"];
export type InvalidPreset = components["schemas"]["InvalidForecastPreset"];
export type Baseline = components["schemas"]["ForecastBaseline"];
export type HistoryRow = components["schemas"]["ForecastHistoryRow"];
export type Defaults = components["schemas"]["ForecastDefaults"];

/** Default projection length when no target year is chosen. */
export const DEFAULT_HORIZON_YEARS = 10;
/** The API bounds on one-off events and the other year fields. */
export const MIN_YEAR = 1900;
export const MAX_YEAR = 2300;
export const MAX_EVENTS = 50;
/** The API's exclusive bound on any money amount (minor units): $100 billion. */
export const MAX_AMOUNT_MINOR = 10 ** 13;

/**
 * Everything the user can adjust. A `null` money/year value means "use the figure derived from
 * history" (shown beside the input as the data default); the rates always have a value.
 */
export interface ForecastState {
  /** Year of the starting net worth; `null` = the latest year with data, else the current year.
   * Saved with a preset, so the target year does not move when the preset is reloaded. */
  baseYear: number | null;
  /** `null` = ten years after the later of the base year and the current year. */
  targetYear: number | null;
  startNetWorthMinor: number | null;
  annualIncomeMinor: number | null;
  incomeGrowthPct: number;
  taxRatePct: number | null;
  annualSpendingMinor: number | null;
  spendingGrowthPct: number;
  spendingChangeMinor: number;
  returnPct: number;
  inflationPct: number;
  retirementYear: number | null;
  retirementIncomeMinor: number;
  events: ForecastEvent[];
}

/** The "Moderate" built-in's rates, so the screen opens on a sensible scenario. */
export const INITIAL_STATE: ForecastState = {
  baseYear: null,
  targetYear: null,
  startNetWorthMinor: null,
  annualIncomeMinor: null,
  incomeGrowthPct: 3,
  taxRatePct: null,
  annualSpendingMinor: null,
  spendingGrowthPct: 2.5,
  spendingChangeMinor: 0,
  returnPct: 6,
  inflationPct: 2.5,
  retirementYear: null,
  retirementIncomeMinor: 0,
  events: [],
};

/** The four rates a built-in preset sets (applying one leaves the rest of the scenario alone). */
export const RATE_KEYS = ["incomeGrowthPct", "returnPct", "spendingGrowthPct", "inflationPct"] as const;

export interface Resolved {
  inputs: ForecastInputs;
  /** False until a starting net worth exists (from history or typed in). */
  ready: boolean;
  baseYear: number;
  targetYear: number;
  /**
   * The history-derived income and spending, rebased to `baseYear`. The data is measured in its
   * own year (`defaults.income_year`, `spending_year`), which can differ from the starting year,
   * so it is grown (or shrunk) from that year to the starting year at the scenario's income or
   * spending growth rate. `null` when there is no such data.
   */
  data: { incomeMinor: number | null; spendingMinor: number | null };
}

/** `amountMinor` measured in `fromYear`, expressed in `toYear` at `growthPct` per year (compound,
 * either direction); unchanged when the years match. Clamped below the API's amount bound. */
export function rebaseAmount(amountMinor: number, fromYear: number, toYear: number, growthPct: number): number {
  if (fromYear === toYear) return amountMinor;
  const scaled = Math.round(amountMinor * Math.pow(1 + growthPct / 100, toYear - fromYear));
  if (!Number.isFinite(scaled)) return amountMinor;
  return Math.min(Math.max(scaled, 0), MAX_AMOUNT_MINOR - 1);
}

function rebased(amount: number | null | undefined, year: number | null | undefined, toYear: number, pct: number) {
  if (amount == null) return null;
  return year == null ? amount : rebaseAmount(amount, year, toYear, pct);
}

/** The state with every "use the data" value filled in from `defaults` (0 where there is none). */
export function resolveInputs(state: ForecastState, defaults: Defaults | undefined, currentYear: number): Resolved {
  const baseYear = state.baseYear ?? defaults?.net_worth_year ?? currentYear;
  const targetYear = clampTargetYear(
    baseYear,
    state.targetYear ?? Math.max(baseYear, currentYear) + DEFAULT_HORIZON_YEARS,
  );
  const start = state.startNetWorthMinor ?? defaults?.net_worth_minor ?? null;
  const dataIncome = rebased(defaults?.annual_income_minor, defaults?.income_year, baseYear, state.incomeGrowthPct);
  const dataSpending = rebased(
    defaults?.annual_spending_minor,
    defaults?.spending_year,
    baseYear,
    state.spendingGrowthPct,
  );
  return {
    baseYear,
    targetYear,
    data: { incomeMinor: dataIncome, spendingMinor: dataSpending },
    ready: start !== null,
    inputs: {
      baseYear,
      targetYear,
      startNetWorthMinor: start ?? 0,
      annualIncomeMinor: state.annualIncomeMinor ?? dataIncome ?? 0,
      incomeGrowthPct: state.incomeGrowthPct,
      taxRatePct: state.taxRatePct ?? defaults?.effective_tax_rate_pct ?? 0,
      annualSpendingMinor: state.annualSpendingMinor ?? dataSpending ?? 0,
      spendingGrowthPct: state.spendingGrowthPct,
      spendingChangeMinor: state.spendingChangeMinor,
      returnPct: state.returnPct,
      inflationPct: state.inflationPct,
      retirementYear: state.retirementYear,
      retirementIncomeMinor: state.retirementIncomeMinor,
      events: state.events,
    },
  };
}

/** The state as the API stores it; `horizon_years` is relative to the resolved base year. */
export function toApiParams(state: ForecastState, resolved: Pick<Resolved, "baseYear" | "targetYear">): ApiParams {
  return {
    horizon_years: Math.min(Math.max(resolved.targetYear - resolved.baseYear, 1), MAX_HORIZON_YEARS),
    base_year: state.baseYear,
    start_net_worth_minor: state.startNetWorthMinor,
    annual_income_minor: state.annualIncomeMinor,
    income_growth_pct: state.incomeGrowthPct,
    tax_rate_pct: state.taxRatePct,
    annual_spending_minor: state.annualSpendingMinor,
    spending_growth_pct: state.spendingGrowthPct,
    spending_change_minor: state.spendingChangeMinor,
    return_pct: state.returnPct,
    inflation_pct: state.inflationPct,
    retirement_year: state.retirementYear,
    retirement_income_minor: state.retirementIncomeMinor,
    events: state.events.map((e) => ({ year: e.year, amount_minor: e.amountMinor, label: e.label })),
  };
}

/**
 * Applies a saved preset: every parameter is replaced, including the starting year (a preset saved
 * with "latest year with data" goes back to that, not to whatever year is set now). A missing
 * horizon keeps the target year. `dataBaseYear` is the year the data suggests (the latest net
 * worth year, else the current year), which the horizon is relative to when the preset has no
 * explicit starting year.
 */
export function applySaved(state: ForecastState, p: ApiParams, dataBaseYear: number): ForecastState {
  const baseYear = p.base_year ?? dataBaseYear;
  return {
    ...state,
    baseYear: p.base_year ?? null,
    targetYear: p.horizon_years != null ? baseYear + p.horizon_years : state.targetYear,
    startNetWorthMinor: p.start_net_worth_minor ?? null,
    annualIncomeMinor: p.annual_income_minor ?? null,
    incomeGrowthPct: p.income_growth_pct,
    taxRatePct: p.tax_rate_pct ?? null,
    annualSpendingMinor: p.annual_spending_minor ?? null,
    spendingGrowthPct: p.spending_growth_pct,
    spendingChangeMinor: p.spending_change_minor,
    returnPct: p.return_pct,
    inflationPct: p.inflation_pct,
    retirementYear: p.retirement_year ?? null,
    retirementIncomeMinor: p.retirement_income_minor,
    events: (p.events ?? []).map((e) => ({ year: e.year, amountMinor: e.amount_minor, label: e.label ?? "" })),
  };
}

/** Applies a built-in preset: only the four rates change. */
export function applyBuiltin(state: ForecastState, p: ApiParams): ForecastState {
  return {
    ...state,
    incomeGrowthPct: p.income_growth_pct,
    returnPct: p.return_pct,
    spendingGrowthPct: p.spending_growth_pct,
    inflationPct: p.inflation_pct,
  };
}

export type PresetRef = { kind: "builtin"; key: string } | { kind: "saved"; id: number };

/** True when the scenario differs from the preset it was started from. */
export function isModified(
  state: ForecastState,
  resolved: Pick<Resolved, "baseYear" | "targetYear">,
  preset: BuiltinPreset | SavedPreset,
): boolean {
  const p = preset.params;
  if ("key" in preset) {
    return (
      state.incomeGrowthPct !== p.income_growth_pct ||
      state.returnPct !== p.return_pct ||
      state.spendingGrowthPct !== p.spending_growth_pct ||
      state.inflationPct !== p.inflation_pct
    );
  }
  return canonical(toApiParams(state, resolved)) !== canonical(p);
}

/** A key-order-independent string of the API params, with nullish and missing values equal. */
function canonical(p: ApiParams): string {
  return JSON.stringify({
    h: p.horizon_years ?? null,
    b: p.base_year ?? null,
    s: p.start_net_worth_minor ?? null,
    i: p.annual_income_minor ?? null,
    ig: p.income_growth_pct,
    t: p.tax_rate_pct ?? null,
    sp: p.annual_spending_minor ?? null,
    sg: p.spending_growth_pct,
    sc: p.spending_change_minor,
    r: p.return_pct,
    f: p.inflation_pct,
    ry: p.retirement_year ?? null,
    ri: p.retirement_income_minor,
    e: (p.events ?? []).map((e) => [e.year, e.amount_minor, e.label ?? ""]),
  });
}

export type DollarsProblem = "format" | "range";

/** Dollars typed by the user ("1,234.56", "$12", "-500") to minor units, or why it is rejected:
 * `format` (unparsable or, unless `allowNegative`, below zero) or `range` (the API's amount bound). */
export function checkDollars(raw: string, allowNegative = false): { minor: number } | { problem: DollarsProblem } {
  const text = raw.trim();
  const negative = text.startsWith("-");
  const minor = parseBalanceInput(negative ? text.slice(1).trim() : text);
  if (minor === null) {
    // A figure too large to represent exactly is out of range, not malformed.
    const digits = (negative ? text.slice(1) : text).trim().replace(/^\$/, "").replace(/,/g, "");
    return { problem: /^\d*\.?\d+$|^\d+\.$/.test(digits) && (!negative || allowNegative) ? "range" : "format" };
  }
  if (negative && !allowNegative) return { problem: "format" };
  if (minor >= MAX_AMOUNT_MINOR) return { problem: "range" };
  return { minor: negative ? -minor : minor };
}

/** `checkDollars` as a plain number: null when rejected. */
export function parseDollars(raw: string, allowNegative = false): number | null {
  const r = checkDollars(raw, allowNegative);
  return "minor" in r ? r.minor : null;
}

/** Minor units as a plain two-decimal dollar string for an input ("-1234.50"). */
export function formatDollarsInput(minor: number): string {
  return (minor / 100).toFixed(2);
}

/** "3.2%" / "-1%"; at most two decimals. */
export function formatPct(value: number): string {
  return `${Number(value.toFixed(2))}%`;
}

/** Compact USD for chart axes ("$1.2M"). */
export function formatCompactUsd(minor: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(minor / 100);
}

/** Whole-dollar USD ("$1,234,567") for table cells and cards. */
export function formatUsd(minor: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(
    minor / 100,
  );
}

/**
 * What to say under "Investment growth". A share of the total change only means something when
 * saving and growth push the same way; with opposite signs (a drawdown that returns partly offset,
 * or losses against savings) a share can read 1000%, so say what offsets what instead.
 */
export function growthBreakdown(contributions: number, growth: number): string {
  const same = contributions === 0 || growth === 0 || contributions > 0 === growth > 0;
  if (same) {
    const total = contributions + growth;
    return total !== 0 ? `${Math.round((growth / total) * 100)}% of the total change` : "Total over the forecast";
  }
  const usd = (n: number) => formatUsd(Math.round(Math.abs(n)));
  return growth > 0
    ? `Offsets ${usd(contributions)} of net withdrawals over the forecast`
    : `Losses offset ${usd(growth)} of the ${usd(contributions)} saved`;
}
