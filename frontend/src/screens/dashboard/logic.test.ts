import { describe, expect, it } from "vitest";

import {
  OTHER_KEY,
  computeMonthOverMonthDelta,
  defaultCategoryLabel,
  detectOtherCurrencies,
  distinctCategories,
  humanizeMerchantKey,
  pieSlicesFromTotals,
  pivotByCategory,
  toTrendPoints,
  topCategoriesWithOther,
  totalsByCategory,
  totalsByUser,
  type AnalyticsRow,
} from "./logic";

function row(overrides: Partial<AnalyticsRow>): AnalyticsRow {
  return {
    period: null,
    category: null,
    subcategory: null,
    user: null,
    account: null,
    merchant: null,
    currency: "USD",
    total_minor: 0,
    txn_count: 0,
    avg_minor: 0,
    ...overrides,
  };
}

describe("pivotByCategory", () => {
  it("groups rows by period, summing each category within a period", () => {
    const rows = [
      row({ period: "2025-01", category: "groceries", total_minor: 1000 }),
      row({ period: "2025-01", category: "dining", total_minor: 500 }),
      row({ period: "2025-01", category: "groceries", total_minor: 200 }),
      row({ period: "2025-02", category: "groceries", total_minor: 300 }),
    ];

    const points = pivotByCategory(rows);

    expect(points).toEqual([
      { period: "2025-01", categories: { groceries: 1200, dining: 500 }, totalMinor: 1700 },
      { period: "2025-02", categories: { groceries: 300 }, totalMinor: 300 },
    ]);
  });

  it("sorts periods chronologically (lexical order on zero-padded period keys)", () => {
    const rows = [
      row({ period: "2025-03", category: "a", total_minor: 1 }),
      row({ period: "2025-01", category: "a", total_minor: 1 }),
      row({ period: "2025-02", category: "a", total_minor: 1 }),
    ];

    expect(pivotByCategory(rows).map((p) => p.period)).toEqual(["2025-01", "2025-02", "2025-03"]);
  });

  it("falls back to 'uncategorized' rather than dropping a row with no category", () => {
    const rows = [row({ period: "2025-01", category: null, total_minor: 500 })];
    const points = pivotByCategory(rows);
    expect(points[0]?.categories).toEqual({ uncategorized: 500 });
  });

  it("returns an empty array for no rows", () => {
    expect(pivotByCategory([])).toEqual([]);
  });
});

describe("distinctCategories", () => {
  it("collects every category key across points, sorted", () => {
    const points = pivotByCategory([
      row({ period: "2025-01", category: "dining", total_minor: 1 }),
      row({ period: "2025-02", category: "groceries", total_minor: 1 }),
    ]);
    expect(distinctCategories(points)).toEqual(["dining", "groceries"]);
  });
});

describe("toTrendPoints", () => {
  it("sums total_minor per period, ignoring other group keys, sorted chronologically", () => {
    const rows = [
      row({ period: "2025-02", total_minor: 100 }),
      row({ period: "2025-01", total_minor: 200 }),
      row({ period: "2025-01", total_minor: 50 }),
    ];
    expect(toTrendPoints(rows)).toEqual([
      { period: "2025-01", totalMinor: 250 },
      { period: "2025-02", totalMinor: 100 },
    ]);
  });
});

describe("computeMonthOverMonthDelta", () => {
  it("returns null with fewer than two periods", () => {
    expect(computeMonthOverMonthDelta([])).toBeNull();
    expect(computeMonthOverMonthDelta([{ period: "2025-01", totalMinor: 100 }])).toBeNull();
  });

  it("computes the delta between the two most recent periods", () => {
    const delta = computeMonthOverMonthDelta([
      { period: "2025-01", totalMinor: 100 },
      { period: "2025-02", totalMinor: 150 },
    ]);
    expect(delta).toEqual({
      currentPeriod: "2025-02",
      previousPeriod: "2025-01",
      currentMinor: 150,
      previousMinor: 100,
      deltaMinor: 50,
      deltaPct: 0.5,
    });
  });

  it("handles a decrease", () => {
    const delta = computeMonthOverMonthDelta([
      { period: "2025-01", totalMinor: 200 },
      { period: "2025-02", totalMinor: 100 },
    ]);
    expect(delta?.deltaMinor).toBe(-100);
    expect(delta?.deltaPct).toBeCloseTo(-0.5);
  });

  it("returns a null percentage when the previous period totalled zero", () => {
    const delta = computeMonthOverMonthDelta([
      { period: "2025-01", totalMinor: 0 },
      { period: "2025-02", totalMinor: 100 },
    ]);
    expect(delta?.deltaPct).toBeNull();
    expect(delta?.deltaMinor).toBe(100);
  });

  it("sorts out-of-order input before picking the two most recent", () => {
    const delta = computeMonthOverMonthDelta([
      { period: "2025-03", totalMinor: 300 },
      { period: "2025-01", totalMinor: 100 },
      { period: "2025-02", totalMinor: 200 },
    ]);
    expect(delta?.currentPeriod).toBe("2025-03");
    expect(delta?.previousPeriod).toBe("2025-02");
  });
});

describe("totalsByUser", () => {
  it("sums total_minor per user, ranked highest first", () => {
    const rows = [
      row({ user: 1, total_minor: 100 }),
      row({ user: 2, total_minor: 500 }),
      row({ user: 1, total_minor: 50 }),
    ];
    expect(totalsByUser(rows)).toEqual([
      { userId: 2, totalMinor: 500 },
      { userId: 1, totalMinor: 150 },
    ]);
  });

  it("drops rows with no user", () => {
    const rows = [row({ user: null, total_minor: 100 }), row({ user: 1, total_minor: 50 })];
    expect(totalsByUser(rows)).toEqual([{ userId: 1, totalMinor: 50 }]);
  });
});

describe("humanizeMerchantKey", () => {
  it("title-cases a lowercase, multi-word merchant key", () => {
    expect(humanizeMerchantKey("hardware supply co")).toBe("Hardware Supply Co");
  });

  it("leaves a single word capitalized", () => {
    expect(humanizeMerchantKey("netflix")).toBe("Netflix");
  });

  it("collapses repeated whitespace", () => {
    expect(humanizeMerchantKey("garden   center")).toBe("Garden Center");
  });

  it("handles an empty string", () => {
    expect(humanizeMerchantKey("")).toBe("");
  });
});

describe("detectOtherCurrencies", () => {
  it("returns currencies other than the selected one, sorted, deduplicated", () => {
    const accounts = [{ currency: "USD" }, { currency: "EUR" }, { currency: "EUR" }, { currency: "GBP" }];
    expect(detectOtherCurrencies(accounts, "USD")).toEqual(["EUR", "GBP"]);
  });

  it("returns an empty array when every account matches the selected currency", () => {
    expect(detectOtherCurrencies([{ currency: "USD" }, { currency: "USD" }], "USD")).toEqual([]);
  });

  it("returns an empty array with no accounts", () => {
    expect(detectOtherCurrencies([], "USD")).toEqual([]);
  });
});

describe("chart-type helpers", () => {
  const row = (period: string, category: string | null, total: number) =>
    ({ period, category, currency: "USD", total_minor: total, txn_count: 1, avg_minor: total }) as AnalyticsRow;

  it("totalsByCategory sums across periods, buckets null as uncategorized, sorts descending", () => {
    const totals = totalsByCategory([row("2026-01", "food", 100), row("2026-02", "food", 50), row("2026-01", null, 400), row("2026-01", "rent", 10)]);
    expect(totals).toEqual([
      { key: "uncategorized", totalMinor: 400 },
      { key: "food", totalMinor: 150 },
      { key: "rent", totalMinor: 10 },
    ]);
  });

  it("pieSlicesFromTotals excludes non-positive totals and lists them", () => {
    const result = pieSlicesFromTotals([
      { key: "a", totalMinor: 300 },
      { key: "b", totalMinor: 100 },
      { key: "refunds", totalMinor: -50 },
      { key: "zero", totalMinor: 0 },
    ]);
    expect(result.excluded).toEqual(["refunds", "zero"]);
    expect(result.totalMinor).toBe(400);
    expect(result.slices.map((s) => s.key)).toEqual(["a", "b"]);
    expect(result.slices[0]!.percent).toBeCloseTo(75);
  });

  it("pieSlicesFromTotals merges the tail above maxSlices into Other", () => {
    const totals = Array.from({ length: 11 }, (_, i) => ({ key: `c${i}`, totalMinor: (11 - i) * 10 }));
    const result = pieSlicesFromTotals(totals, 8);
    expect(result.slices).toHaveLength(9);
    expect(result.slices[8]).toMatchObject({ key: OTHER_KEY, totalMinor: 30 + 20 + 10 });
    expect(result.slices.reduce((sum, s) => sum + s.percent, 0)).toBeCloseTo(100);
  });

  it("pieSlicesFromTotals handles empty input", () => {
    expect(pieSlicesFromTotals([])).toEqual({ slices: [], excluded: [], totalMinor: 0 });
  });

  it("topCategoriesWithOther keeps the top n by absolute total and sums the rest, filling gaps with 0", () => {
    const points = pivotByCategory([
      row("2026-01", "big", 1000),
      row("2026-02", "big", 900),
      row("2026-01", "mid", 500),
      row("2026-02", "neg", -700),
      row("2026-01", "tiny1", 5),
      row("2026-02", "tiny2", 7),
    ]);
    const { keys, data } = topCategoriesWithOther(points, distinctCategories(points), 3);
    expect(keys).toEqual(["big", "neg", "mid", OTHER_KEY]);
    expect(data).toEqual([
      { period: "2026-01", big: 1000, neg: 0, mid: 500, [OTHER_KEY]: 5 },
      { period: "2026-02", big: 900, neg: -700, mid: 0, [OTHER_KEY]: 7 },
    ]);
  });

  it("topCategoriesWithOther adds no Other series when everything fits", () => {
    const points = pivotByCategory([row("2026-01", "a", 1)]);
    expect(topCategoriesWithOther(points, ["a"], 8).keys).toEqual(["a"]);
  });
});

describe("merged bucket label", () => {
  it("is not confusable with the taxonomy's Others category", () => {
    expect(defaultCategoryLabel(OTHER_KEY)).toBe("All other categories");
  });
  it("records which categories were merged", () => {
    const totals = Array.from({ length: 10 }, (_, i) => ({ key: `c${i}`, totalMinor: (10 - i) * 100 }));
    const merged = pieSlicesFromTotals(totals, 8).slices.at(-1)!;
    expect(merged.key).toBe(OTHER_KEY);
    expect(merged.members).toEqual(["c8", "c9"]);
  });
});
