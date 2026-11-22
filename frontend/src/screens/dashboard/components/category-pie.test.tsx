import { render, screen, within } from "@testing-library/react";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";

import type { AnalyticsRow } from "../logic";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  PieChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Pie: ({ data, children }: { data: unknown; children: React.ReactNode }) => (
    <div data-testid="mock-pie" data-slices={JSON.stringify(data)}>
      {children}
    </div>
  ),
  Cell: ({ fill }: { fill?: string }) => <div data-testid="mock-cell" data-fill={fill} />,
  Tooltip: () => null,
}));

const { CategoryPie } = await import("./category-pie");

const row = (period: string, category: string, total: number): AnalyticsRow => ({
  period, category, currency: "USD", total_minor: total, txn_count: 1, avg_minor: total,
});

const labelFor = (k: string) => ({ groceries: "Groceries", food_dining: "Dining", refunds: "Refunds" })[k] ?? k;

describe("CategoryPie", () => {
  it("sums across periods, labels with taxonomy names and summarises in the aria-label", () => {
    render(
      <CategoryPie
        rows={[row("2026-01", "groceries", 6000), row("2026-02", "groceries", 2000), row("2026-01", "food_dining", 2000)]}
        currency="USD"
        labelFor={labelFor}
      />,
    );
    const pie = screen.getByTestId("category-pie");
    expect(within(pie).getByRole("img")).toHaveAttribute(
      "aria-label",
      "Spending by category: Groceries 80%, Dining 20%",
    );
    const list = within(pie).getByRole("list");
    expect(within(list).getByText("Groceries")).toBeInTheDocument();
    expect(within(list).getByText("Dining")).toBeInTheDocument();
    expect(within(list).queryByText("food_dining")).not.toBeInTheDocument();
    expect(within(list).getByText("80%")).toBeInTheDocument();
    expect(screen.getAllByTestId("mock-cell")).toHaveLength(2);
  });

  it("lists categories whose refunds exceed spending instead of drawing them", () => {
    render(
      <CategoryPie
        rows={[row("2026-01", "groceries", 5000), row("2026-01", "refunds", -300)]}
        currency="USD"
        labelFor={labelFor}
      />,
    );
    expect(screen.getByText("Not shown (refunds exceed spending): Refunds")).toBeInTheDocument();
    expect(screen.getAllByTestId("mock-cell")).toHaveLength(1);
  });

  it("merges small categories into Other above 8 slices", () => {
    const rows = Array.from({ length: 10 }, (_, i) => row("2026-01", `cat${i}`, (i + 1) * 100));
    render(<CategoryPie rows={rows} currency="USD" />);
    expect(screen.getAllByTestId("mock-cell")).toHaveLength(9);
    expect(within(screen.getByRole("list")).getByText("Other")).toBeInTheDocument();
  });

  it("shows an empty state when nothing is positive", () => {
    render(<CategoryPie rows={[row("2026-01", "refunds", -300)]} currency="USD" labelFor={labelFor} />);
    expect(screen.getByText("No spending to chart")).toBeInTheDocument();
    expect(screen.getByText(/Refunds/)).toBeInTheDocument();
  });
});
