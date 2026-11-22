import { describe, expect, it } from "vitest";

import type { components } from "@/api/client";

import { computeDrift, findPreviousStatement } from "./drift";

type Statement = components["schemas"]["Statement"];

function statement(overrides: Partial<Statement>): Statement {
  return {
    id: 1,
    account_id: 10,
    status: "parsed",
    period_start: null,
    period_end: null,
    txn_count: 20,
    reconciliation_delta_minor: 0,
    detect_score: 0.9,
    shape_warnings: null,
    error_detail: null,
    created_at: "2024-01-01T00:00:00Z",
    original_name: "statement.pdf",
    has_pdf: true,
    pdf_in_place: false,
    ...overrides,
  };
}

describe("computeDrift", () => {
  it("is not drifted with no signals and no previous statement", () => {
    const info = computeDrift(statement({}), null);
    expect(info.drifted).toBe(false);
    expect(info.reasons).toEqual([]);
  });

  it("flags a nonzero reconciliation delta", () => {
    const info = computeDrift(statement({ reconciliation_delta_minor: 150 }), null);
    expect(info.drifted).toBe(true);
    expect(info.reasons[0]).toContain("reconciliation delta");
  });

  it("flags shape warnings", () => {
    const info = computeDrift(statement({ shape_warnings: JSON.stringify(["missing PURCHASES section"]) }), null);
    expect(info.drifted).toBe(true);
    expect(info.reasons[0]).toContain("missing PURCHASES section");
  });

  it("flags a detect() score decay of 0.95 to 0.62, per §2d.1's own example", () => {
    const previous = statement({ id: 1, detect_score: 0.95 });
    const current = statement({ id: 2, detect_score: 0.62 });
    const info = computeDrift(current, previous);
    expect(info.drifted).toBe(true);
    expect(info.detectScoreDelta).toBeCloseTo(-0.33);
  });

  it("does not flag a small detect() score wobble", () => {
    const previous = statement({ id: 1, detect_score: 0.9 });
    const current = statement({ id: 2, detect_score: 0.85 });
    const info = computeDrift(current, previous);
    expect(info.drifted).toBe(false);
  });

  it("reports the txn count delta against the previous statement", () => {
    const previous = statement({ id: 1, txn_count: 30 });
    const current = statement({ id: 2, txn_count: 12 });
    const info = computeDrift(current, previous);
    expect(info.txnCountDelta).toBe(-18);
  });
});

describe("findPreviousStatement", () => {
  it("returns null when there is no earlier statement on the same account", () => {
    const current = statement({ id: 1, account_id: 10 });
    expect(findPreviousStatement([current], current)).toBeNull();
  });

  it("picks the most recent earlier statement on the same account", () => {
    const current = statement({ id: 3, account_id: 10, created_at: "2024-03-01T00:00:00Z" });
    const other_account = statement({ id: 4, account_id: 99, created_at: "2024-02-15T00:00:00Z" });
    const older = statement({ id: 1, account_id: 10, created_at: "2024-01-01T00:00:00Z" });
    const newer_but_still_earlier = statement({ id: 2, account_id: 10, created_at: "2024-02-01T00:00:00Z" });

    const result = findPreviousStatement([current, other_account, older, newer_but_still_earlier], current);
    expect(result?.id).toBe(2);
  });

  it("ignores statements with a null account_id", () => {
    const current = statement({ id: 2, account_id: 10, created_at: "2024-02-01T00:00:00Z" });
    const unassigned = statement({ id: 1, account_id: null, created_at: "2024-01-01T00:00:00Z" });
    expect(findPreviousStatement([current, unassigned], current)).toBeNull();
  });
});
