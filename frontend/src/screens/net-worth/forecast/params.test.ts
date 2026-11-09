import { describe, expect, it } from "vitest";

import { chartData } from "./chart-data";
import { project } from "./forecast-logic";
import {
  INITIAL_STATE,
  applyBuiltin,
  applySaved,
  checkDollars,
  formatPct,
  formatUsd,
  isModified,
  parseDollars,
  rebaseAmount,
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

describe("rebasing the data defaults to the starting year", () => {
  it("rebaseAmount grows or shrinks compound per year, in either direction", () => {
    expect(rebaseAmount(1_000_000, 2023, 2023, 3)).toBe(1_000_000);
    expect(rebaseAmount(1_000_000, 2023, 2025, 10)).toBe(1_210_000);
    expect(rebaseAmount(1_210_000, 2025, 2023, 10)).toBe(1_000_000); // earlier start: deflate
    expect(rebaseAmount(1_000_000, 2023, 2024, -50)).toBe(500_000);
    expect(rebaseAmount(10 ** 12, 1900, 2300, 50)).toBeLessThan(10 ** 13); // clamped, never Infinity
  });

  it("brings income from an older tax year up to the starting year at the income growth rate", () => {
    const defaults = { ...DEFAULTS, annual_income_minor: 10_000_000, income_year: 2022 };
    const r = resolveInputs({ ...INITIAL_STATE, incomeGrowthPct: 5 }, defaults, 2026);
    expect(r.baseYear).toBe(2024); // latest net worth year
    // 100,000 in 2022 -> 2023 -> 2024 at 5%: 110,250. Not 100,000, which would lose two years.
    expect(r.data.incomeMinor).toBe(11_025_000);
    expect(r.inputs.annualIncomeMinor).toBe(11_025_000);
    // Spending is from the same year as the start: untouched.
    expect(r.data.spendingMinor).toBe(4_000_000);
  });

  it("deflates spending measured after the starting year, and leaves typed values alone", () => {
    const defaults = { ...DEFAULTS, annual_spending_minor: 4_100_000, spending_year: 2026 };
    const r = resolveInputs({ ...INITIAL_STATE, spendingGrowthPct: 10 }, defaults, 2026);
    expect(r.data.spendingMinor).toBe(3_388_430); // 41,000 / 1.1^2 = 33,884.30
    const typed = resolveInputs({ ...INITIAL_STATE, annualSpendingMinor: 5_000_000 }, defaults, 2026);
    expect(typed.inputs.annualSpendingMinor).toBe(5_000_000);
  });

  it("follows a chosen starting year, and has no data to rebase when there is none", () => {
    const defaults = { ...DEFAULTS, annual_income_minor: 9_000_000, income_year: 2024 };
    const r = resolveInputs({ ...INITIAL_STATE, baseYear: 2026, incomeGrowthPct: 10 }, defaults, 2026);
    expect(r.data.incomeMinor).toBe(10_890_000);
    const none = resolveInputs(INITIAL_STATE, undefined, 2026);
    expect(none.data).toEqual({ incomeMinor: null, spendingMinor: null });
  });
});

describe("saved presets keep the starting year", () => {
  const resolved = { baseYear: 2020, targetYear: 2040 };

  it("round-trips an explicit starting year, so the target does not shift on reload", () => {
    const state = { ...INITIAL_STATE, baseYear: 2020, targetYear: 2040 };
    const api = toApiParams(state, resolved);
    expect(api).toMatchObject({ base_year: 2020, horizon_years: 20 });
    // The data's latest year (2024) is not what the preset's horizon is relative to.
    const loaded = applySaved(INITIAL_STATE, api, 2024);
    expect(loaded.baseYear).toBe(2020);
    expect(loaded.targetYear).toBe(2040);
  });

  it("a preset saved on 'latest year with data' goes back to it", () => {
    const api = toApiParams({ ...INITIAL_STATE, targetYear: 2034 }, { baseYear: 2024, targetYear: 2034 });
    expect(api.base_year).toBeNull();
    const loaded = applySaved({ ...INITIAL_STATE, baseYear: 2010 }, api, 2025);
    expect(loaded.baseYear).toBeNull();
    expect(loaded.targetYear).toBe(2035);
  });

  it("counts a changed starting year as a modification", () => {
    const preset: SavedPreset = {
      id: 1,
      user_id: null,
      name: "P",
      params: toApiParams({ ...INITIAL_STATE, baseYear: 2020 }, resolved),
      created_at: "",
      updated_at: "",
    };
    expect(isModified({ ...INITIAL_STATE, baseYear: 2020, targetYear: 2040 }, resolved, preset)).toBe(false);
    expect(isModified({ ...INITIAL_STATE, baseYear: 2021, targetYear: 2040 }, resolved, preset)).toBe(true);
  });
});

describe("checkDollars", () => {
  it("rejects amounts the API would refuse, saying why", () => {
    expect(checkDollars("99,999,999,999.99")).toEqual({ minor: 9_999_999_999_999 });
    expect(checkDollars("100,000,000,000")).toEqual({ problem: "range" });
    expect(checkDollars("-100,000,000,000", true)).toEqual({ problem: "range" });
    expect(checkDollars("99999999999999999999999")).toEqual({ problem: "range" }); // beyond exact integers
    expect(checkDollars("abc")).toEqual({ problem: "format" });
    expect(checkDollars("-5")).toEqual({ problem: "format" });
    expect(parseDollars("100000000000")).toBeNull();
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
    earned_income_minor: null,
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
