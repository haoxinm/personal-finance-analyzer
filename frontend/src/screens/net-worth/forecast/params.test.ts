import { describe, expect, it } from "vitest";

import { chartData } from "./chart-data";
import { project } from "./forecast-logic";
import {
  INITIAL_STATE,
  applyBuiltin,
  applySaved,
  formatPct,
  formatUsd,
  isModified,
  parseDollars,
  resolveInputs,
  toApiParams,
  type BuiltinPreset,
  type Defaults,
  type HistoryRow,
  type SavedPreset,
} from "./params";

const DEFAULTS: Defaults = {
  net_worth_minor: 5_000_000,
  net_worth_year: 2024,
  annual_income_minor: 9_000_000,
  income_year: 2024,
  effective_tax_rate_pct: 22.5,
  annual_spending_minor: 4_000_000,
  spending_year: 2024,
  spending_annualized: false,
  income_growth_pct: 4,
  spending_growth_pct: 3,
  savings_rate_pct: 40,
};

describe("resolveInputs", () => {
  it("fills every 'use the data' value from the defaults and targets ten years out", () => {
    const r = resolveInputs(INITIAL_STATE, DEFAULTS, 2026);
    expect(r.ready).toBe(true);
    expect(r.baseYear).toBe(2024);
    expect(r.targetYear).toBe(2036); // 10 years after the later of base year and current year
    expect(r.inputs).toMatchObject({
      startNetWorthMinor: 5_000_000,
      annualIncomeMinor: 9_000_000,
      taxRatePct: 22.5,
      annualSpendingMinor: 4_000_000,
      incomeGrowthPct: 3,
      returnPct: 6,
    });
  });

  it("lets typed values override the data, and clamps the target year to 60 years", () => {
    const r = resolveInputs(
      { ...INITIAL_STATE, startNetWorthMinor: 1, annualIncomeMinor: 2, taxRatePct: 0, annualSpendingMinor: 0, baseYear: 2020, targetYear: 2200 },
      DEFAULTS,
      2026,
    );
    expect(r.baseYear).toBe(2020);
    expect(r.targetYear).toBe(2080);
    expect(r.inputs).toMatchObject({ startNetWorthMinor: 1, annualIncomeMinor: 2, taxRatePct: 0, annualSpendingMinor: 0 });
  });

  it("is not ready without a starting net worth, and uses the current year as the base", () => {
    const r = resolveInputs(INITIAL_STATE, undefined, 2026);
    expect(r).toMatchObject({ ready: false, baseYear: 2026, targetYear: 2036 });
    expect(r.inputs).toMatchObject({ startNetWorthMinor: 0, annualIncomeMinor: 0, taxRatePct: 0 });
    const typed = resolveInputs({ ...INITIAL_STATE, startNetWorthMinor: 100 }, { ...DEFAULTS, net_worth_minor: null, net_worth_year: null }, 2026);
    expect(typed.ready).toBe(true);
  });

  it("produces a projection that matches a hand calculation from the defaults", () => {
    const state = { ...INITIAL_STATE, incomeGrowthPct: 0, spendingGrowthPct: 0, returnPct: 0, targetYear: 2026 };
    const rows = project(resolveInputs(state, DEFAULTS, 2024).inputs);
    // 90,000 income - 22.5% tax - 40,000 spending = 29,750 saved on top of 50,000.
    expect(rows).toHaveLength(2);
    expect(rows[1]!.netWorth).toBeCloseTo(5_000_000 + 2 * 2_975_000, 6);
  });
});

describe("API params", () => {
  const resolved = { baseYear: 2024, targetYear: 2044 };

  it("serialises relative to the base year and round-trips through applySaved", () => {
    const state = {
      ...INITIAL_STATE,
      targetYear: 2044,
      annualIncomeMinor: 1_000_000,
      retirementYear: 2040,
      events: [{ year: 2030, amountMinor: -500_000, label: "House" }],
    };
    const api = toApiParams(state, resolved);
    expect(api).toMatchObject({
      horizon_years: 20,
      annual_income_minor: 1_000_000,
      start_net_worth_minor: null,
      retirement_year: 2040,
      events: [{ year: 2030, amount_minor: -500_000, label: "House" }],
    });
    expect(applySaved(INITIAL_STATE, api, 2024)).toEqual({ ...state, baseYear: null });
  });

  it("keeps the current target year when a preset has no horizon", () => {
    const api = { ...toApiParams(INITIAL_STATE, resolved), horizon_years: null };
    expect(applySaved({ ...INITIAL_STATE, targetYear: 2050 }, api, 2024).targetYear).toBe(2050);
  });

  it("applies a built-in preset to the four rates only", () => {
    const state = { ...INITIAL_STATE, annualIncomeMinor: 7, retirementYear: 2040, events: [{ year: 2030, amountMinor: 1, label: "" }] };
    const next = applyBuiltin(state, { ...toApiParams(INITIAL_STATE, resolved), income_growth_pct: 5, return_pct: 8, spending_growth_pct: 2, inflation_pct: 2.5 });
    expect(next).toEqual({ ...state, incomeGrowthPct: 5, returnPct: 8, spendingGrowthPct: 2, inflationPct: 2.5 });
  });
});

describe("isModified", () => {
  const resolved = { baseYear: 2024, targetYear: 2034 };
  const builtin: BuiltinPreset = {
    key: "moderate",
    name: "Moderate",
    description: "",
    params: { ...toApiParams(INITIAL_STATE, resolved), horizon_years: null },
  };
  const saved = (state = INITIAL_STATE): SavedPreset => ({
    id: 1,
    user_id: null,
    name: "Mine",
    params: toApiParams(state, resolved),
    created_at: "",
    updated_at: "",
  });

  it("compares only the four rates for a built-in", () => {
    expect(isModified(INITIAL_STATE, resolved, builtin)).toBe(false);
    expect(isModified({ ...INITIAL_STATE, annualIncomeMinor: 5, targetYear: 2060 }, resolved, builtin)).toBe(false);
    expect(isModified({ ...INITIAL_STATE, returnPct: 7 }, resolved, builtin)).toBe(true);
  });

  it("compares the whole scenario for a saved preset", () => {
    expect(isModified(INITIAL_STATE, resolved, saved())).toBe(false);
    expect(isModified({ ...INITIAL_STATE, annualIncomeMinor: 5 }, resolved, saved())).toBe(true);
    expect(isModified({ ...INITIAL_STATE, events: [{ year: 2030, amountMinor: 1, label: "" }] }, resolved, saved())).toBe(true);
    expect(isModified(INITIAL_STATE, { baseYear: 2024, targetYear: 2035 }, saved())).toBe(true);
  });
});

describe("parseDollars and formatting", () => {
  it("parses dollars to cents, with an optional sign", () => {
    expect(parseDollars("1,234.56")).toBe(123_456);
    expect(parseDollars(" $12 ")).toBe(1200);
    expect(parseDollars("0")).toBe(0);
    expect(parseDollars("-500", true)).toBe(-50_000);
    expect(parseDollars("- 500", true)).toBe(-50_000);
    expect(parseDollars("-500")).toBeNull();
    expect(parseDollars("abc", true)).toBeNull();
    expect(parseDollars("", true)).toBeNull();
    expect(parseDollars("-", true)).toBeNull();
  });

  it("formats percentages and dollars", () => {
    expect(formatPct(3)).toBe("3%");
    expect(formatPct(3.456)).toBe("3.46%");
    expect(formatPct(-1.5)).toBe("-1.5%");
    expect(formatUsd(123_456_700)).toBe("$1,234,567");
  });
});

describe("chartData", () => {
  const row = (year: number, net_worth_minor: number | null): HistoryRow => ({
    year,
    net_worth_minor,
    source: net_worth_minor === null ? null : "entries",
    as_of: null,
    manual_net_worth_minor: null,
    manual_note: null,
    income_minor: null,
    tax_minor: null,
    income_basis: null,
    spending_minor: null,
    spending_months: 0,
    savings_minor: null,
  });

  it("joins history and projection at the base year and skips years without net worth", () => {
    const rows = project(resolveInputs({ ...INITIAL_STATE, targetYear: 2026 }, DEFAULTS, 2024).inputs);
    const data = chartData([row(2022, 1), row(2023, null), row(2024, 5_000_000)], rows, 2024, 5_000_000);
    expect(data.map((d) => d.year)).toEqual([2022, 2024, 2025, 2026]);
    expect(data[0]).toEqual({ year: 2022, history: 1 });
    expect(data[1]).toEqual({ year: 2024, history: 5_000_000, projection: 5_000_000 });
    expect(data[3]!.projection).toBe(rows[1]!.netWorth);
    expect(data[3]!.history).toBeUndefined();
  });
});
