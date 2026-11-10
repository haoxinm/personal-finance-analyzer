import { render as rtlRender, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { SummaryBar } from "./summary-bar";
import type { ImportItem } from "./types";

/** The bar links to Review and Transactions, so it needs a router. */
function render(ui: ReactElement) {
  return rtlRender(<MemoryRouter>{ui}</MemoryRouter>);
}

function makeParsedItem(overrides: Partial<ImportItem> = {}): ImportItem {
  return {
    clientId: `c-${Math.random()}`,
    fileName: "statement.pdf",
    userId: 1,
    status: "parsed",
    issuerId: 3,
    extractorChoice: null,
    remember: true,
    statement: {
      id: 1,
      user_id: 1,
      account_id: 5,
      status: "parsed",
      period_start: "2024-01-01",
      period_end: "2024-01-31",
      txn_count: 100,
      reconciliation_delta_minor: null,
      detect_score: 0.9,
      shape_warnings: null,
      error_detail: null,
      created_at: "2024-02-01T00:00:00Z",
    },
    ...overrides,
  };
}

describe("SummaryBar", () => {
  it("renders nothing until at least one statement has been imported", () => {
    render(
      <SummaryBar
        items={[
          { ...makeParsedItem(), status: "uploading" },
          { ...makeParsedItem(), status: "awaiting_extractor" },
        ]}
      />,
    );
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("sums statements and new transactions across parsed items, omitting need-review until known for all", () => {
    render(
      <SummaryBar
        items={[
          makeParsedItem({ statement: { ...makeParsedItem().statement!, txn_count: 300 } }),
          makeParsedItem({ statement: { ...makeParsedItem().statement!, txn_count: 112 } }),
        ]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "2 statements imported · 412 new transactions",
    );
  });

  it("adds the need-review clause once every parsed item has a known count", () => {
    render(
      <SummaryBar
        items={[
          makeParsedItem({
            statement: { ...makeParsedItem().statement!, txn_count: 300 },
            needsReviewCount: 30,
          }),
          makeParsedItem({
            statement: { ...makeParsedItem().statement!, txn_count: 112 },
            needsReviewCount: 8,
          }),
        ]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "2 statements imported · 412 new transactions · 38 need review",
    );
  });

  it("uses singular wording for exactly one statement/transaction", () => {
    render(<SummaryBar items={[makeParsedItem({ statement: { ...makeParsedItem().statement!, txn_count: 1 } })]} />);
    expect(screen.getByRole("status")).toHaveTextContent("1 statement imported · 1 new transaction");
  });

  it("separates rows an overlapping statement already had, and a file dropped twice", () => {
    render(
      <SummaryBar
        items={[
          makeParsedItem({ insertedCount: 90, skippedDuplicates: 10, needsReviewCount: 4 }),
          makeParsedItem({ needsReviewCount: 0, duplicate: true }),
        ]}
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "1 statement imported · 90 new transactions · 10 already present · 1 already imported · 4 need review",
    );
  });

  it("says only 'already imported' when every file was a duplicate, and offers no review link", () => {
    render(<SummaryBar items={[makeParsedItem({ duplicate: true })]} />);
    expect(screen.getByRole("status")).toHaveTextContent("1 already imported");
    expect(screen.getByRole("status")).not.toHaveTextContent("new transaction");
    expect(screen.queryByRole("link", { name: /Review/ })).not.toBeInTheDocument();
  });

  it("links on to Review when something needs it, and always to the transactions", () => {
    render(<SummaryBar items={[makeParsedItem({ needsReviewCount: 38 })]} />);
    expect(screen.getByRole("link", { name: /Review 38/ })).toHaveAttribute("href", "/spending/review");
    expect(screen.getByRole("link", { name: "View transactions" })).toHaveAttribute(
      "href",
      "/spending/transactions",
    );
  });
});
