import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StatementCard } from "./statement-card";
import type { ImportItem } from "./types";

vi.mock("@/hooks/use-job-progress", () => ({
  useJobProgress: () => ({ event: null, connectionState: "idle" }),
}));

function makeItem(overrides: Partial<ImportItem> = {}): ImportItem {
  return {
    clientId: "c1",
    fileName: "chase-january.pdf",
    userId: 1,
    status: "awaiting_extractor",
    issuerId: 3,
    extractorChoice: { kind: "parser", parserId: "layout_a_credit" },
    remember: true,
    proposal: {
      issuer_id: 3,
      parser_id: "layout_a_credit",
      layout_spec_id: null,
      confidence: 0.92,
      auto_confirmed: false,
    },
    ...overrides,
  };
}

function renderCard(item: ImportItem, overrides: Partial<Parameters<typeof StatementCard>[0]> = {}) {
  return render(
    <MemoryRouter>
      <StatementCard
        item={item}
        onJobEvent={vi.fn()}
        onRememberChange={vi.fn()}
        onConfirm={vi.fn()}
        picker={<div data-testid="picker-slot" />}
        {...overrides}
      />
    </MemoryRouter>,
  );
}

describe("StatementCard", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("shows the filename and the picker while awaiting the extractor choice", () => {
    renderCard(makeItem());
    expect(screen.getByText("chase-january.pdf")).toBeInTheDocument();
    expect(screen.getByText("Ready to import")).toBeInTheDocument();
    expect(screen.getByTestId("picker-slot")).toBeInTheDocument();
    expect(screen.getByText(/Detected confidence: 92%/)).toBeInTheDocument();
    expect(screen.getByText(/confident match/)).toBeInTheDocument();
  });

  it("disables Import until both an issuer and an extractor are chosen", () => {
    renderCard(makeItem({ issuerId: null, extractorChoice: null }));
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();
  });

  it("calls onConfirm when Import is clicked", async () => {
    const onConfirm = vi.fn();
    renderCard(makeItem(), { onConfirm });
    const { default: userEvent } = await import("@testing-library/user-event");
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("shows the no_text_layer message verbatim", () => {
    renderCard(
      makeItem({
        status: "no_text_layer",
        errorMessage: "Can't read this PDF — it looks like a scan or an image with no text layer.",
      }),
    );
    expect(
      screen.getByText("Can't read this PDF — it looks like a scan or an image with no text layer."),
    ).toBeInTheDocument();
  });

  it("shows a layout-mapper link for unsupported_layout, carrying the statement id", () => {
    renderCard(makeItem({ status: "unsupported_layout", statementId: 42 }));
    const link = screen.getByRole("link", { name: "Map this layout" });
    expect(link).toHaveAttribute("href", "/spending/layout-mapper?statement_id=42");
  });

  it("shows the reconciliation delta and shape-drift warning once parsed", () => {
    renderCard(
      makeItem({
        status: "parsed",
        statement: {
          id: 1,
          user_id: 1,
          account_id: 5,
          status: "parsed",
          period_start: "2024-01-01",
          period_end: "2024-01-31",
          txn_count: 40,
          reconciliation_delta_minor: 250,
          detect_score: 0.6,
          shape_warnings: JSON.stringify(["missing section: PAYMENTS"]),
          error_detail: null,
          created_at: "2024-02-01T00:00:00Z",
        },
        needsReviewCount: 3,
      }),
    );
    expect(screen.getByText(/40 new transactions/)).toBeInTheDocument();
    expect(screen.getByText(/3 need review/)).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Reconciliation delta:");
    expect(screen.getByText(/missing section: PAYMENTS/)).toBeInTheDocument();
  });

  const parsedStatement = {
    id: 9,
    user_id: 1,
    account_id: 5,
    status: "parsed" as const,
    period_start: "2024-01-01",
    period_end: "2024-01-31",
    txn_count: 40,
    reconciliation_delta_minor: 0,
    detect_score: 0.9,
    shape_warnings: null,
    error_detail: null,
    created_at: "2024-02-01T00:00:00Z",
  };

  it("shows 'Already imported' for a duplicate PDF instead of a count of new transactions", () => {
    renderCard(
      makeItem({ status: "parsed", statementId: 9, statement: parsedStatement, duplicate: true }),
    );
    expect(screen.getByText(/Already imported/)).toBeInTheDocument();
    expect(screen.queryByText(/new transaction/)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Review/ })).not.toBeInTheDocument();
  });

  it("shows new and already-present counts, with next-step links", () => {
    renderCard(
      makeItem({
        status: "parsed",
        statementId: 9,
        statement: parsedStatement,
        insertedCount: 31,
        skippedDuplicates: 9,
        needsReviewCount: 5,
      }),
    );
    expect(screen.getByText("31 new transactions · 9 already present · 5 need review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Review 5/ })).toHaveAttribute("href", "/spending/review");
    expect(screen.getByRole("link", { name: "View transactions" })).toHaveAttribute(
      "href",
      "/spending/transactions?statement_id=9",
    );
  });
});
