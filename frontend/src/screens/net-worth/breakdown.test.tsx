import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Breakdown, NetWorthHeadline } from "./breakdown";
import { SNAPSHOT, USERS } from "./test-utils";

describe("NetWorthHeadline", () => {
  it("shows net worth and an up change in words", () => {
    render(<NetWorthHeadline snapshot={SNAPSHOT} currency="USD" />);
    expect(screen.getByTestId("net-worth-total")).toHaveTextContent("$8,200.00");
    expect(screen.getByTestId("net-worth-change")).toHaveTextContent("Up $200.00 vs Feb 2025");
  });

  it("renders a negative net worth with a minus sign and a down change", () => {
    render(
      <NetWorthHeadline
        snapshot={{ ...SNAPSHOT, net_worth_minor: -5000, change_minor: -1000 }}
        currency="USD"
      />,
    );
    expect(screen.getByTestId("net-worth-total")).toHaveTextContent("-$50.00");
    expect(screen.getByTestId("net-worth-change")).toHaveTextContent("Down $10.00 vs Feb 2025");
  });

  it("explains when there is nothing to compare with", () => {
    render(
      <NetWorthHeadline
        snapshot={{ ...SNAPSHOT, previous_period: null, change_minor: null }}
        currency="USD"
      />,
    );
    expect(screen.getByTestId("net-worth-change")).toHaveTextContent("No earlier period");
  });
});

describe("Breakdown", () => {
  it("lists categories, accounts, totals and the carried note", () => {
    render(<Breakdown snapshot={SNAPSHOT} users={USERS} currency="USD" />);
    const card = within(screen.getByTestId("category-credit_card"));
    expect(card.getByText("-$300.00")).toBeInTheDocument();
    expect(screen.getByText("Chase checking")).toBeInTheDocument();
    expect(screen.getByText(/carried from Feb 2025/)).toBeInTheDocument();
    expect(screen.getByTestId("breakdown-net-worth")).toHaveTextContent("$8,200.00");
    // Share of assets: checking 1000 / 8500
    expect(within(screen.getByTestId("category-checking")).getByText("12%")).toBeInTheDocument();
  });

  it("adds owner names when accounts belong to several users", () => {
    render(
      <Breakdown
        snapshot={{
          ...SNAPSHOT,
          accounts: [
            ...SNAPSHOT.accounts,
            { user_id: 2, category: "checking", account_name: "Wells", balance_minor: 1, signed_minor: 1, as_of: "2025-03", carried: false },
          ],
        }}
        users={USERS}
        currency="USD"
      />,
    );
    expect(screen.getByText("Wells (Sam)")).toBeInTheDocument();
  });
});
