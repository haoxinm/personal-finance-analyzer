import { render, screen } from "@testing-library/react";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";

import type { AnalyticsRow } from "../logic";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  BarChart: ({ data, children }: { data: unknown; children: React.ReactNode }) => (
    <div data-testid="mock-barchart" data-chart={JSON.stringify(data)}>
      {children}
    </div>
  ),
  Bar: (props: { dataKey: string; fill?: string; isAnimationActive?: boolean }) => (
    <div data-testid="mock-bar" data-key={props.dataKey} data-fill={props.fill} data-animation-active={String(props.isAnimationActive)} />
  ),
  CartesianGrid: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

const { TrendBars } = await import("./trend-bars");

const ROWS: AnalyticsRow[] = [
  { period: "2026-02", currency: "USD", total_minor: -500, txn_count: 2, avg_minor: -250 },
  { period: "2026-01", currency: "USD", total_minor: 15000, txn_count: 3, avg_minor: 5000 },
];

describe("TrendBars", () => {
  it("feeds one chronological bar per period, including negative totals", () => {
    render(<TrendBars rows={ROWS} currency="USD" />);
    const data = JSON.parse(screen.getByTestId("mock-barchart").getAttribute("data-chart")!) as unknown;
    expect(data).toEqual([
      { period: "2026-01", totalMinor: 15000 },
      { period: "2026-02", totalMinor: -500 },
    ]);
    const bar = screen.getByTestId("mock-bar");
    expect(bar).toHaveAttribute("data-key", "totalMinor");
    expect(bar).toHaveAttribute("data-fill", "hsl(var(--outflow))");
    expect(bar).toHaveAttribute("data-animation-active", "false");
    expect(screen.getByTestId("trend-bars")).toHaveAttribute("aria-label", expect.stringContaining("2 periods"));
  });

  it("shows an empty state with no rows", () => {
    render(<TrendBars rows={[]} currency="USD" />);
    expect(screen.getByText("No trend yet")).toBeInTheDocument();
  });
});
