import { render, screen } from "@testing-library/react";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";

import type { AnalyticsRow } from "../logic";

// See `category-stacked-bar.test.tsx` for why the whole Recharts module is stubbed.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ data, children }: { data: unknown; children: React.ReactNode }) => (
    <div data-testid="mock-linechart" data-chart={JSON.stringify(data)}>
      {children}
    </div>
  ),
  Line: (props: { dataKey: string; name?: string; stroke?: string; isAnimationActive?: boolean }) => (
    <div
      data-testid="mock-line"
      data-key={props.dataKey}
      data-name={props.name}
      data-stroke={props.stroke}
      data-animation-active={String(props.isAnimationActive)}
    />
  ),
  CartesianGrid: () => null,
  Legend: () => null,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
}));

const { CategoryLines } = await import("./category-lines");

const row = (period: string, category: string, total: number): AnalyticsRow => ({
  period, category, currency: "USD", total_minor: total, txn_count: 1, avg_minor: total,
});

describe("CategoryLines", () => {
  it("draws one line per category with taxonomy names, animation off", () => {
    render(
      <CategoryLines
        rows={[row("2026-01", "food_dining", 100), row("2026-02", "food_dining", 200), row("2026-01", "rent", 900)]}
        currency="USD"
        labelFor={(k) => (k === "food_dining" ? "Dining" : k)}
      />,
    );
    expect(screen.getByTestId("category-lines")).toHaveAttribute("aria-label", expect.stringContaining("2 periods, 2 categories"));
    const lines = screen.getAllByTestId("mock-line");
    expect(lines.map((l) => l.getAttribute("data-name")).sort()).toEqual(["Dining", "rent"]);
    for (const line of lines) expect(line).toHaveAttribute("data-animation-active", "false");
  });

  it("merges categories beyond the top 8 into an Other line", () => {
    const rows = Array.from({ length: 10 }, (_, i) => row("2026-01", `cat${i}`, (i + 1) * 100));
    render(<CategoryLines rows={rows} currency="USD" />);
    const lines = screen.getAllByTestId("mock-line");
    expect(lines).toHaveLength(9);
    const other = lines.find((l) => l.getAttribute("data-key") === "__other__");
    expect(other).toHaveAttribute("data-name", "All other categories");
    expect(other).toHaveAttribute("data-stroke", "hsl(var(--muted-foreground))");
  });

  it("shows an empty state with no rows", () => {
    render(<CategoryLines rows={[]} currency="USD" />);
    expect(screen.getByText("No spending yet")).toBeInTheDocument();
  });
});
