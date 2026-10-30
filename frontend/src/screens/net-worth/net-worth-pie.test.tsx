import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NetWorthPie } from "./net-worth-pie";
import { SNAPSHOT, stubResizeObserver } from "./test-utils";

stubResizeObserver(vi);

describe("NetWorthPie", () => {
  it("renders the pie with a legend of debit categories only", () => {
    render(<NetWorthPie snapshot={SNAPSHOT} currency="USD" />);
    expect(screen.getByTestId("net-worth-pie")).toBeInTheDocument();
    expect(screen.getByRole("img")).toHaveAccessibleName(/Checking 12%.*Savings 29%.*Investment 59%/);
    expect(screen.getByText("Investment")).toBeInTheDocument();
    expect(screen.queryByText("Credit card")).not.toBeInTheDocument();
  });

  it("shows an empty state when there are no assets", () => {
    render(
      <NetWorthPie
        snapshot={{
          ...SNAPSHOT,
          categories: SNAPSHOT.categories.map((c) => ({ ...c, balance_minor: 0, signed_minor: 0 })),
        }}
        currency="USD"
      />,
    );
    expect(screen.getByText("No assets to chart")).toBeInTheDocument();
    expect(screen.queryByTestId("net-worth-pie")).not.toBeInTheDocument();
  });
});
