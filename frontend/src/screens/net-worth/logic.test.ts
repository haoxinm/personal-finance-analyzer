import { describe, expect, it } from "vitest";

import {
  accountSeriesRows,
  accountSuggestions,
  categorySeriesRows,
  changeDirection,
  formatBalanceInput,
  parseBalanceInput,
  periodLabel,
  pieSlices,
  toPeriod,
} from "./logic";
import { SNAPSHOT, SUMMARY, entry } from "./test-utils";

describe("net-worth logic", () => {
  it("parses dollar input into minor units", () => {
    expect(parseBalanceInput("1,234.5")).toBe(123450);
    expect(parseBalanceInput("$12")).toBe(1200);
    expect(parseBalanceInput("0")).toBe(0);
    expect(parseBalanceInput(".5")).toBe(50);
    expect(parseBalanceInput("1.005")).toBe(101);
    expect(parseBalanceInput("1.004")).toBe(100);
    expect(parseBalanceInput("-5")).toBeNull();
    expect(parseBalanceInput("")).toBeNull();
    expect(parseBalanceInput("abc")).toBeNull();
    expect(parseBalanceInput("1.2.3")).toBeNull();
  });

  it("formats minor units for an input", () => {
    expect(formatBalanceInput(123456)).toBe("1234.56");
  });

  it("labels periods", () => {
    expect(periodLabel("2025-03")).toBe("Mar 2025");
    expect(periodLabel("bogus")).toBe("bogus");
    expect(toPeriod(2025, 3)).toBe("2025-03");
  });

  it("suggests distinct account names, most recent first, case-insensitively", () => {
    const entries = [
      entry({ id: 1, account_name: "chase checking", month: 1 }),
      entry({ id: 2, account_name: "Chase Checking", month: 3 }),
      entry({ id: 3, account_name: "Ally", month: 2 }),
      entry({ id: 4, account_name: "Visa", category: "credit_card", month: 3 }),
    ];
    expect(accountSuggestions(entries, "checking")).toEqual(["Chase Checking", "Ally"]);
    expect(accountSuggestions(entries, "credit_card")).toEqual(["Visa"]);
  });

  it("pivots the category series into wide rows including net worth", () => {
    const rows = categorySeriesRows(SUMMARY.series);
    expect(rows[1]).toEqual({
      period: "2025-03",
      checking: 100000,
      savings: 250000,
      investment: 500000,
      credit_card: 30000,
      net_worth: 820000,
    });
  });

  it("pivots account series, flags carried points and adds owners in multi-user mode", () => {
    const single = accountSeriesRows(SUMMARY.account_series);
    expect(single.keys.map((k) => k.label)).toEqual(["Chase checking", "Ally"]);
    expect(single.rows).toHaveLength(2);
    expect(single.rows[1]!["carried:1:savings:Ally"]).toBe(true);

    const multi = accountSeriesRows(
      [...SUMMARY.account_series, { user_id: 2, category: "checking", account_name: "Wells", points: [] }],
      [{ id: 2, name: "Sam", is_default: false }],
    );
    expect(multi.keys.at(-1)!.label).toBe("Wells (Sam)");
  });

  it("builds pie slices from debit categories only", () => {
    const slices = pieSlices(SNAPSHOT);
    expect(slices.map((s) => s.category)).toEqual(["checking", "savings", "investment"]);
    expect(slices.reduce((s, x) => s + x.percent, 0)).toBeCloseTo(100);
    const zeroed = pieSlices({
      ...SNAPSHOT,
      categories: SNAPSHOT.categories.map((c) => (c.category === "savings" ? { ...c, balance_minor: 0 } : c)),
    });
    expect(zeroed.map((s) => s.category)).toEqual(["checking", "investment"]);
    expect(pieSlices(null)).toEqual([]);
  });

  it("classifies change direction", () => {
    expect(changeDirection(5)).toBe("up");
    expect(changeDirection(-5)).toBe("down");
    expect(changeDirection(0)).toBe("flat");
    expect(changeDirection(null)).toBe("none");
  });
});
