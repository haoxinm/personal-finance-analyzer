import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { NetWorthTrends } from "./net-worth-trends";
import { SUMMARY, USERS, stubResizeObserver } from "./test-utils";

stubResizeObserver(vi);

describe("NetWorthTrends", () => {
  it("toggles between category and account charts", async () => {
    render(<NetWorthTrends summary={SUMMARY} users={USERS} currency="USD" />);
    expect(screen.getByTestId("net-worth-trend-category")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "By category" })).toHaveAttribute("aria-checked", "true");

    await userEvent.click(screen.getByRole("radio", { name: "By account" }));
    expect(screen.getByTestId("net-worth-trend-account")).toBeInTheDocument();
    expect(screen.queryByTestId("net-worth-trend-category")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "By account" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText(/Hollow dots/)).toBeInTheDocument();
  });

  it("shows an empty state without data", () => {
    render(<NetWorthTrends summary={{ ...SUMMARY, series: [], account_series: [] }} users={USERS} currency="USD" />);
    expect(screen.getByText("No trend yet")).toBeInTheDocument();
  });

  it("hints to add another month when there is a single period", () => {
    render(
      <NetWorthTrends
        summary={{ ...SUMMARY, series: SUMMARY.series.slice(0, 1) }}
        users={USERS}
        currency="USD"
      />,
    );
    expect(screen.getByText(/Add another month/)).toBeInTheDocument();
  });
});
