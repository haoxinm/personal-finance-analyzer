import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { stubResizeObserver } from "../test-utils";
import { ForecastChart } from "./forecast-chart";
import { project } from "./forecast-logic";
import type { HistoryRow } from "./params";

stubResizeObserver(vi);

const history: HistoryRow[] = [2022, 2023, 2024].map((year, i) => ({
  year,
  net_worth_minor: 1_000_000 * (i + 1),
  source: "entries",
  as_of: `${year}-12`,
  manual_net_worth_minor: null,
  manual_note: null,
  income_minor: null,
  earned_income_minor: null,
  tax_minor: null,
  income_basis: null,
  spending_minor: null,
  spending_months: 0,
  savings_minor: null,
}));

const rows = project({
  baseYear: 2024,
  targetYear: 2030,
  startNetWorthMinor: 3_000_000,
  annualIncomeMinor: 0,
  incomeGrowthPct: 0,
  taxRatePct: 0,
  annualSpendingMinor: 0,
  spendingGrowthPct: 0,
  spendingChangeMinor: 0,
  returnPct: 5,
  inflationPct: 2,
  retirementYear: null,
  retirementIncomeMinor: 0,
  events: [],
});

describe("ForecastChart", () => {
  it("describes the chart for assistive technology, including the projected value", () => {
    render(<ForecastChart history={history} rows={rows} baseYear={2024} startNetWorthMinor={3_000_000} targetYear={2030} mode="nominal" />);
    const chart = screen.getByTestId("forecast-chart");
    expect(chart).toHaveAttribute("role", "img");
    expect(chart.getAttribute("aria-label")).toMatch(/Net worth by year, 2022 to 2030; projected \$40,203 in 2030 \(nominal dollars\)\./);
  });

  it("labels starting-year-dollars mode", () => {
    render(<ForecastChart history={[]} rows={rows} baseYear={2024} startNetWorthMinor={3_000_000} targetYear={2030} mode="real" />);
    expect(screen.getByTestId("forecast-chart").getAttribute("aria-label")).toContain("2024 dollars");
  });
});
