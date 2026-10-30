import type { components } from "@/api/client";

import { parseBalanceInput } from "../logic";
import { MAX_HORIZON_YEARS, clampTargetYear, type ForecastEvent, type ForecastInputs } from "./forecast-logic";

export type ApiParams = components["schemas"]["ForecastParams"];
export type BuiltinPreset = components["schemas"]["BuiltinForecastPreset"];
export type SavedPreset = components["schemas"]["ForecastPreset"];
export type Baseline = components["schemas"]["ForecastBaseline"];
export type HistoryRow = components["schemas"]["ForecastHistoryRow"];
export type Defaults = components["schemas"]["ForecastDefaults"];

/** Default projection length when no target year is chosen. */
export const DEFAULT_HORIZON_YEARS = 10;
/** The API bounds on one-off events and the other year fields. */
export const MIN_YEAR = 1900;
export const MAX_YEAR = 2300;
export const MAX_EVENTS = 50;

/**
 * Everything the user can adjust. A `null` money/year value means "use the figure derived from
 * history" (shown beside the input as the data default); the rates always have a value.
 */
export interface ForecastState {
  /** Year of the starting net worth; `null` = the latest year with data, else the current year. */
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
}

/** The state with every "use the data" value filled in from `defaults` (0 where there is none). */
export function resolveInputs(state: ForecastState, defaults: Defaults | undefined, currentYear: number): Resolved {
  const baseYear = state.baseYear ?? defaults?.net_worth_year ?? currentYear;
  const targetYear = clampTargetYear(
    baseYear,
    state.targetYear ?? Math.max(baseYear, currentYear) + DEFAULT_HORIZON_YEARS,
  );
  const start = state.startNetWorthMinor ?? defaults?.net_worth_minor ?? null;
  return {
    baseYear,
    targetYear,
    ready: start !== null,
    inputs: {
      baseYear,
      targetYear,
      startNetWorthMinor: start ?? 0,
      annualIncomeMinor: state.annualIncomeMinor ?? defaults?.annual_income_minor ?? 0,
      incomeGrowthPct: state.incomeGrowthPct,
      taxRatePct: state.taxRatePct ?? defaults?.effective_tax_rate_pct ?? 0,
      annualSpendingMinor: state.annualSpendingMinor ?? defaults?.annual_spending_minor ?? 0,
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

/** Applies a saved preset: every parameter is replaced; a missing horizon keeps the target year. */
export function applySaved(state: ForecastState, p: ApiParams, baseYear: number): ForecastState {
  return {
    ...state,
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

/** Dollars typed by the user ("1,234.56", "$12", "-500") to minor units; null if unparsable or,
 * unless `allowNegative`, below zero. */
export function parseDollars(raw: string, allowNegative = false): number | null {
  const text = raw.trim();
  const negative = text.startsWith("-");
  const minor = parseBalanceInput(negative ? text.slice(1).trim() : text);
  if (minor === null) return null;
  if (negative) return allowNegative ? -minor : null;
  return minor;
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
