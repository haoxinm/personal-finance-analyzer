import { describe, expect, it } from "vitest";

import {
  MAX_HORIZON_YEARS,
  clampTargetYear,
  deflator,
  project,
  rowsIn,
  summarize,
  toReal,
  yearsToGoal,
  type ForecastInputs,
} from "./forecast-logic";

/** A flat, zero-rate scenario: $100k net worth, $80k income, 25% tax, $40k spending. */
function inputs(over: Partial<ForecastInputs> = {}): ForecastInputs {
  return {
    baseYear: 2025,
    targetYear: 2030,
    startNetWorthMinor: 10_000_000,
    annualIncomeMinor: 8_000_000,
    incomeGrowthPct: 0,
    taxRatePct: 25,
    annualSpendingMinor: 4_000_000,
    spendingGrowthPct: 0,
    spendingChangeMinor: 0,
    returnPct: 0,
    inflationPct: 0,
    retirementYear: null,
    retirementIncomeMinor: 0,
    events: [],
    ...over,
  };
}

describe("project: zero-growth sanity", () => {
  it("adds the same savings every year (hand calc)", () => {
    // savings = 80,000 - 25% x 80,000 - 40,000 = 20,000 a year
    const rows = project(inputs());
    expect(rows.map((r) => r.year)).toEqual([2026, 2027, 2028, 2029, 2030]);
    for (const r of rows) {
      expect(r.income).toBe(8_000_000);
      expect(r.tax).toBe(2_000_000);
      expect(r.spending).toBe(4_000_000);
      expect(r.savings).toBe(2_000_000);
      expect(r.investmentReturn).toBe(0);
    }
    expect(rows.map((r) => r.netWorth)).toEqual([12_000_000, 14_000_000, 16_000_000, 18_000_000, 20_000_000]);
  });

  it("projects a single year when the target is not after the base year", () => {
    expect(project(inputs({ targetYear: 2025 }))).toHaveLength(1);
    expect(project(inputs({ targetYear: 1990 }))).toHaveLength(1);
  });
});

describe("project: compounding", () => {
  it("compounds the return on the start balance with mid-year savings", () => {
    // Year 1: base = 100,000 + 20,000 / 2 = 110,000; return 7% = 7,700; NW = 127,700.
    // Year 2: base = 127,700 + 10,000 = 137,700; return = 9,639; NW = 127,700 + 20,000 + 9,639 = 157,339.
    const rows = project(inputs({ returnPct: 7, targetYear: 2027 }));
    expect(rows[0]!.investmentReturn).toBeCloseTo(770_000, 6);
    expect(rows[0]!.netWorth).toBeCloseTo(12_770_000, 6);
    expect(rows[1]!.investmentReturn).toBeCloseTo(963_900, 6);
    expect(rows[1]!.netWorth).toBeCloseTo(15_733_900, 6);
  });

  it("with no savings, grows exactly like compound interest", () => {
    const rows = project(inputs({ annualIncomeMinor: 0, annualSpendingMinor: 0, returnPct: 5, targetYear: 2035 }));
    expect(rows[rows.length - 1]!.netWorth).toBeCloseTo(10_000_000 * Math.pow(1.05, 10), 4);
  });

  it("grows income and spending geometrically, and adds the fixed spending change", () => {
    const rows = project(
      inputs({ incomeGrowthPct: 10, spendingGrowthPct: 5, spendingChangeMinor: 100_000, targetYear: 2027 }),
    );
    expect(rows[0]!.income).toBeCloseTo(8_800_000, 6);
    expect(rows[1]!.income).toBeCloseTo(9_680_000, 6);
    // spending_1 = 40,000 x 1.05 + 1,000 = 43,000; spending_2 = 43,000 x 1.05 + 1,000 = 46,150
    expect(rows[0]!.spending).toBeCloseTo(4_300_000, 6);
    expect(rows[1]!.spending).toBeCloseTo(4_615_000, 6);
    expect(rows[1]!.tax).toBeCloseTo(9_680_000 * 0.25, 6);
  });

  it("applies a fixed spending change alone, and never lets spending go below zero", () => {
    const flat = project(inputs({ spendingChangeMinor: 500_000, targetYear: 2027 }));
    expect(flat.map((r) => r.spending)).toEqual([4_500_000, 5_000_000]);
    const shrinking = project(inputs({ spendingChangeMinor: -3_000_000, targetYear: 2028 }));
    expect(shrinking.map((r) => r.spending)).toEqual([1_000_000, 0, 0]);
  });
});

describe("project: drawdown and negative balances", () => {
  it("draws net worth down in a negative-savings year", () => {
    // Spending 100,000 vs 60,000 after tax: savings -40,000 a year.
    const rows = project(inputs({ annualSpendingMinor: 10_000_000, targetYear: 2028 }));
    expect(rows.map((r) => r.savings)).toEqual([-4_000_000, -4_000_000, -4_000_000]);
    expect(rows.map((r) => r.netWorth)).toEqual([6_000_000, 2_000_000, -2_000_000]);
  });

  it("earns a return on the mid-year balance of a negative-savings year", () => {
    // base = 100,000 - 40,000 / 2 = 80,000; 10% = 8,000.
    const [row] = project(inputs({ annualSpendingMinor: 10_000_000, returnPct: 10, targetYear: 2026 }));
    expect(row!.investmentReturn).toBeCloseTo(800_000, 6);
    expect(row!.netWorth).toBeCloseTo(10_000_000 - 4_000_000 + 800_000, 6);
  });

  it("neither earns nor accrues interest on a negative balance", () => {
    const rows = project(
      inputs({ startNetWorthMinor: -5_000_000, annualIncomeMinor: 0, annualSpendingMinor: 0, returnPct: 10, targetYear: 2027 }),
    );
    expect(rows.map((r) => r.investmentReturn)).toEqual([0, 0]);
    expect(rows.map((r) => r.netWorth)).toEqual([-5_000_000, -5_000_000]);
  });

  it("climbs out of a negative balance once savings turn it positive", () => {
    const [row] = project(inputs({ startNetWorthMinor: -1_000_000, returnPct: 10, targetYear: 2026 }));
    // savings 20,000: base = -10,000 + 10,000 = 0, so no return; NW = 10,000.
    expect(row!.investmentReturn).toBe(0);
    expect(row!.netWorth).toBe(1_000_000);
  });
});

describe("project: retirement", () => {
  it("stops salary after the last working year and falls back to retirement income", () => {
    const rows = project(inputs({ retirementYear: 2027, retirementIncomeMinor: 0, targetYear: 2030 }));
    expect(rows.map((r) => r.income)).toEqual([8_000_000, 8_000_000, 0, 0, 0]);
    expect(rows.map((r) => r.tax)).toEqual([2_000_000, 2_000_000, 0, 0, 0]);
    expect(rows.map((r) => r.savings)).toEqual([2_000_000, 2_000_000, -4_000_000, -4_000_000, -4_000_000]);
    expect(rows[4]!.netWorth).toBe(10_000_000 + 4_000_000 - 12_000_000);
  });

  it("indexes retirement income to inflation and taxes it at the effective rate", () => {
    const rows = project(
      inputs({ retirementYear: 2026, retirementIncomeMinor: 3_000_000, inflationPct: 10, targetYear: 2028 }),
    );
    expect(rows[0]!.income).toBe(8_000_000); // still working in 2026
    expect(rows[1]!.income).toBeCloseTo(3_000_000 * 1.21, 6); // 2027: n = 2
    expect(rows[2]!.income).toBeCloseTo(3_000_000 * 1.331, 6);
    expect(rows[2]!.tax).toBeCloseTo(3_000_000 * 1.331 * 0.25, 6);
  });

  it("works through the whole horizon when the retirement year is past the target", () => {
    const rows = project(inputs({ retirementYear: 2060 }));
    expect(rows.every((r) => r.income === 8_000_000)).toBe(true);
  });

  it("never retires when the year is null", () => {
    expect(project(inputs({ retirementYear: null, targetYear: 2040 })).every((r) => r.income > 0)).toBe(true);
  });
});

describe("project: one-off events", () => {
  it("adds events to the year they fall in, after that year's return", () => {
    const rows = project(
      inputs({
        returnPct: 10,
        targetYear: 2027,
        events: [
          { year: 2026, amountMinor: -3_000_000, label: "House" },
          { year: 2027, amountMinor: 5_000_000, label: "Inheritance" },
        ],
      }),
    );
    // 2026: base 110,000; return 11,000; NW = 100,000 + 20,000 + 11,000 - 30,000 = 101,000
    expect(rows[0]!.events).toBe(-3_000_000);
    expect(rows[0]!.investmentReturn).toBeCloseTo(1_100_000, 6);
    expect(rows[0]!.netWorth).toBeCloseTo(10_100_000, 6);
    // The event money earns from the next year on: 2027 base = 101,000 + 10,000 = 111,000.
    expect(rows[1]!.investmentReturn).toBeCloseTo(1_110_000, 6);
    expect(rows[1]!.events).toBe(5_000_000);
  });

  it("sums several events in one year and ignores events outside the projection", () => {
    const rows = project(
      inputs({
        targetYear: 2027,
        events: [
          { year: 2026, amountMinor: -1_000_000, label: "a" },
          { year: 2026, amountMinor: 400_000, label: "b" },
          { year: 2025, amountMinor: 9_999_999, label: "base year: already in the start balance" },
          { year: 2040, amountMinor: 9_999_999, label: "beyond the target" },
        ],
      }),
    );
    expect(rows.map((r) => r.events)).toEqual([-600_000, 0]);
    expect(rows[1]!.netWorth).toBe(10_000_000 + 2 * 2_000_000 - 600_000);
  });
});

describe("project: target year bounds", () => {
  it("clamps to between one year and 60 years after the base year", () => {
    expect(clampTargetYear(2025, 2020)).toBe(2026);
    expect(clampTargetYear(2025, 2025)).toBe(2026);
    expect(clampTargetYear(2025, 2026)).toBe(2026);
    expect(clampTargetYear(2025, 2085)).toBe(2085);
    expect(clampTargetYear(2025, 2086)).toBe(2085);
    expect(clampTargetYear(2025, 2030.4)).toBe(2030);
    expect(MAX_HORIZON_YEARS).toBe(60);
  });

  it("projects exactly 60 years at the limit and no more beyond it", () => {
    expect(project(inputs({ targetYear: 2085 }))).toHaveLength(60);
    expect(project(inputs({ targetYear: 3000 }))).toHaveLength(60);
  });
});

describe("real (today's dollars) values", () => {
  it("deflates by (1 + inflation)^years from the base year", () => {
    expect(deflator(0, 30)).toBe(1);
    expect(deflator(3, 2)).toBeCloseTo(1.0609, 10);
    const rows = project(inputs({ inflationPct: 10, targetYear: 2027 }));
    const real = toReal(rows[1]!, 2025, 10);
    expect(real.netWorth).toBeCloseTo(rows[1]!.netWorth / 1.21, 6);
    expect(real.income).toBeCloseTo(8_000_000 / 1.21, 6);
    expect(real.year).toBe(2027);
  });

  it("equals nominal at zero inflation", () => {
    const rows = project(inputs());
    expect(rowsIn("real", rows, 2025, 0)).toEqual(rows);
    expect(rowsIn("nominal", rows, 2025, 10)).toEqual(rows);
  });

  it("makes inflation-matching growth flat in real terms", () => {
    // 5% return, 5% inflation, nothing else: real net worth stays at the start.
    const rows = project(inputs({ annualIncomeMinor: 0, annualSpendingMinor: 0, returnPct: 5, inflationPct: 5, targetYear: 2045 }));
    const last = toReal(rows[rows.length - 1]!, 2025, 5);
    expect(last.netWorth).toBeCloseTo(10_000_000, 4);
  });
});

describe("summarize", () => {
  it("splits growth into contributions and investment return that add up", () => {
    const i = inputs({ returnPct: 6, incomeGrowthPct: 3, spendingGrowthPct: 2, targetYear: 2040, events: [{ year: 2030, amountMinor: -2_500_000, label: "x" }] });
    const rows = project(i);
    const s = summarize(i, rows);
    expect(s.targetYear).toBe(2040);
    expect(s.netWorthNominal).toBe(rows[rows.length - 1]!.netWorth);
    expect(s.startNetWorth + s.contributions + s.growth).toBeCloseTo(s.netWorthNominal, 4);
    expect(s.growth).toBeGreaterThan(0);
  });

  it("reports real net worth at the target and uses the start with no rows", () => {
    const i = inputs({ inflationPct: 10, targetYear: 2027 });
    const s = summarize(i, project(i));
    expect(s.netWorthReal).toBeCloseTo(s.netWorthNominal / 1.21, 6);
    const empty = summarize(i, []);
    expect(empty).toMatchObject({ targetYear: 2025, netWorthNominal: 10_000_000, netWorthReal: 10_000_000, contributions: 0, growth: 0 });
  });
});

describe("yearsToGoal", () => {
  it("finds the first year the goal is met", () => {
    const i = inputs({ targetYear: 2040 });
    const rows = project(i); // 120k, 140k, 160k ...
    expect(yearsToGoal(i, rows, 15_000_000, "nominal")).toEqual({ year: 2028, years: 3 });
    expect(yearsToGoal(i, rows, 16_000_000, "nominal")).toEqual({ year: 2028, years: 3 });
  });

  it("is year zero when the start already meets the goal, null when never reached", () => {
    const i = inputs();
    const rows = project(i);
    expect(yearsToGoal(i, rows, 10_000_000, "nominal")).toEqual({ year: 2025, years: 0 });
    expect(yearsToGoal(i, rows, 99_000_000, "nominal")).toBeNull();
  });

  it("compares real dollars in real mode, which takes longer under inflation", () => {
    const i = inputs({ inflationPct: 10, targetYear: 2060 });
    const rows = project(i);
    const nominal = yearsToGoal(i, rows, 20_000_000, "nominal");
    const real = yearsToGoal(i, rows, 20_000_000, "real");
    expect(nominal).not.toBeNull();
    // Real dollars shrink 10% a year while savings stay flat in nominal terms: never catches up.
    expect(real).toBeNull();
  });
});
