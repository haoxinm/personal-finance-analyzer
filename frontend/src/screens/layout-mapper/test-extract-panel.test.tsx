import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { components } from "@/api/client";
import type { JobEvent } from "@/hooks/use-job-progress";

import { TestExtractPanel } from "./test-extract-panel";

type Statement = components["schemas"]["Statement"];

let jobEvent: JobEvent | null = null;

vi.mock("@/hooks/use-job-progress", () => ({
  useJobProgress: () => ({ event: jobEvent, connectionState: "open" }),
}));

vi.mock("./api", () => ({
  useTestLayoutSpec: () => ({ isPending: false, mutateAsync: vi.fn() }),
}));

vi.mock("@/components/ui/use-toast", () => ({ useToast: () => ({ toast: vi.fn() }) }));

const STATEMENT = { id: 3, status: "parsed", txn_count: 12 } as Statement;

function renderPanel() {
  render(<TestExtractPanel statement={STATEMENT} layoutSpecId={1} issuerId={1} />);
}

describe("TestExtractPanel re-parse edit report", () => {
  beforeEach(() => {
    jobEvent = null;
  });

  it("warns when hand edits could not be matched to a rebuilt row", () => {
    jobEvent = { status: "done", progress: 1, restored_edits: 1, unmatched_edits: 2 };
    renderPanel();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "2 of your hand edits (category, kind or notes) could not be matched",
    );
  });

  it("says how many edits were kept when none were lost", () => {
    jobEvent = { status: "done", progress: 1, restored_edits: 3, unmatched_edits: 0 };
    renderPanel();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Kept 3 hand edits on the rebuilt rows.")).toBeInTheDocument();
  });

  it("says nothing about edits before the job is done", () => {
    jobEvent = { status: "running", progress: 0.5, unmatched_edits: 2 };
    renderPanel();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
