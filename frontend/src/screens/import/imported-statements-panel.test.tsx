import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { attentionNotes, describeReimport, formatPeriod, sortStatements } from "./statement-format";

class MockEventSource {
  static instances: MockEventSource[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    MockEventSource.instances.push(this);
  }
  close() {
    // nothing to release
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

async function describeRequest(input: RequestInfo | URL): Promise<{ url: string; method: string; body: unknown }> {
  if (input instanceof Request) {
    const text = await input.clone().text();
    return { url: input.url, method: input.method, body: text ? (JSON.parse(text) as unknown) : undefined };
  }
  return { url: input.toString(), method: "GET", body: undefined };
}

function statement(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    user_id: 1,
    account_id: 5,
    issuer_id: 3,
    parser_id: "layout_a_credit",
    layout_spec_id: null,
    status: "parsed",
    period_start: "2026-03-01",
    period_end: "2026-03-31",
    txn_count: 42,
    reconciliation_delta_minor: 0,
    detect_score: 0.9,
    shape_warnings: null,
    error_detail: null,
    created_at: "2026-04-01T10:00:00Z",
    original_name: "chase-march.pdf",
    has_pdf: true,
    pdf_in_place: false,
    pdf_missing: false,
    ...overrides,
  };
}

interface World {
  statements: Record<string, unknown>[];
  specs?: Record<string, unknown>[];
  llm?: string;
  reparse?: { status: number; body: unknown };
  job?: Record<string, unknown>;
  jobStatus?: number;
  candidates?: Record<string, unknown>[];
  patch?: { status: number; body: unknown };
  jobs?: Record<string, Record<string, unknown>>;
  /** Reparse answers in order (the last repeats); overrides `reparse`. */
  reparseSequence?: { status: number; body: unknown }[];
}

function makeFetch(world: World) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fn = vi.fn<typeof fetch>(async (input) => {
    const request = await describeRequest(input);
    calls.push(request);
    const { url, method } = request;
    if (url.includes("/reparse") && method === "POST") {
      const seq = world.reparseSequence;
      const answer = seq
        ? (seq[Math.min(calls.filter((c) => c.url.includes("/reparse")).length - 1, seq.length - 1)] as { status: number; body: unknown })
        : (world.reparse ?? { status: 202, body: { job_id: "job-1" } });
      return jsonResponse(answer.body, answer.status);
    }
    if (url.includes("/extractors")) {
      return world.candidates ? jsonResponse(world.candidates) : jsonResponse({ detail: "no" }, 404);
    }
    if (method === "PATCH" && url.includes("/api/statements/")) {
      const answer = world.patch ?? { status: 200, body: { ...statement(), rows_moved: 42, duplicates_deleted: 0 } };
      return jsonResponse(answer.body, answer.status);
    }
    for (const [id, job] of Object.entries(world.jobs ?? {})) {
      if (url.includes(`/api/jobs/${id}`) && !url.includes("/events")) return jsonResponse(job);
    }
    if (url.includes("/api/jobs/job-2")) {
      return jsonResponse({ id: "job-2", kind: "classify", status: "done", progress: 1 });
    }
    if (url.includes("/api/jobs/job-1") && world.jobStatus) return jsonResponse({ detail: "boom" }, world.jobStatus);
    if (url.includes("/api/jobs/job-1")) {
      return jsonResponse(world.job ?? { id: "job-1", kind: "import", status: "done", progress: 1, inserted: 42, restored_edits: 3 });
    }
    if (url.includes("/api/statements/") && method === "DELETE") return new Response(null, { status: 204 });
    if (url.includes("/api/statements")) {
      const owner = new URL(url).searchParams.get("user_id");
      return jsonResponse(owner === null ? world.statements : world.statements.filter((r) => String(r.user_id) === owner));
    }
    if (url.includes("/api/accounts")) {
      return jsonResponse([{ id: 5, user_id: 1, issuer_id: 3, account_type: "credit", currency: "USD" }]);
    }
    if (url.includes("/api/issuers")) return jsonResponse([{ id: 3, name: "Chase", slug: "chase", match_terms: [] }]);
    if (url.includes("/api/layout-specs")) return jsonResponse(world.specs ?? []);
    if (url.includes("/api/users")) {
      return jsonResponse([{ id: 1, name: "Alex", is_default: true }, { id: 2, name: "Sam", is_default: false }]);
    }
    if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: world.llm ?? "none" } });
    return jsonResponse({ detail: `unhandled: ${method} ${url}` }, 404);
  });
  return { fn, calls };
}

interface PanelExtras {
  request?: { statementId: number; nonce: number } | null;
  onRequestHandled?: (nonce: number) => void;
  queryClient?: QueryClient;
}

async function renderPanel(
  fetchMock: typeof fetch,
  userId: number | null = 1,
  onFinishImport?: (s: { id: number }) => void,
  extras: PanelExtras = {},
) {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { ImportedStatementsPanel } = await import("./imported-statements-panel");
  const { Toaster } = await import("@/components/ui/toaster");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const queryClient = extras.queryClient ?? new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastContextProvider>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <ImportedStatementsPanel
            userId={userId}
            onFinishImport={onFinishImport as never}
            request={extras.request}
            onRequestHandled={extras.onRequestHandled}
          />
        </MemoryRouter>
        <Toaster />
      </ToastContextProvider>
    </QueryClientProvider>,
  );
}

describe("statement helpers", () => {
  it("formats a period and sorts newest first", () => {
    expect(formatPeriod("2026-03-01", "2026-03-31")).toMatch(/Mar 1 – Mar 31, 2026/);
    expect(formatPeriod(null, null)).toBe("Period unknown");
    const sorted = sortStatements([
      statement({ id: 1, period_end: "2026-01-31" }),
      statement({ id: 2, period_end: "2026-03-31" }),
      statement({ id: 3, period_end: null }),
    ] as never);
    expect(sorted.map((s) => s.id)).toEqual([2, 1, 3]);
  });

  it("describes a re-import result", () => {
    const text = describeReimport({
      inserted: 42,
      restored_edits: 3,
      unmatched_edits: 2,
      discarded_edits: 0,
    } as never);
    expect(text.headline).toBe("Re-imported: 42 transactions. 3 edits kept.");
    expect(text.notes).toEqual(["2 edits could not be matched to a transaction and were dropped."]);
    expect(describeReimport({ inserted: 1, discarded_edits: 4 } as never).notes).toEqual([
      "4 edits discarded, as you asked.",
    ]);
  });
});

describe("ImportedStatementsPanel", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists statements with account, period and count, and flags totals that do not reconcile", async () => {
    const { fn } = makeFetch({
      statements: [statement(), statement({ id: 2, original_name: "old.pdf", period_end: "2026-02-28", reconciliation_delta_minor: 1250 })],
    });
    await renderPanel(fn);
    const rows = await screen.findAllByTestId("imported-statement");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("chase-march.pdf")).toBeInTheDocument();
    await waitFor(() => expect(within(rows[0]!).getByText("Chase credit")).toBeInTheDocument());
    expect(within(rows[0]!).getByText("42 transactions")).toBeInTheDocument();
    expect(within(rows[0]!).queryByText(/reconcile/)).not.toBeInTheDocument();
    expect(within(rows[1]!).getByText(/Totals don.t reconcile/)).toHaveTextContent("$12.50");
    expect(within(rows[0]!).getByRole("link", { name: "View transactions" })).toHaveAttribute(
      "href",
      "/spending/transactions?statement_id=1&include_non_spend=true",
    );
  });

  it("shows the empty state", async () => {
    const { fn } = makeFetch({ statements: [] });
    await renderPanel(fn);
    expect(await screen.findByText("No statements imported yet.")).toBeInTheDocument();
  });

  it("names the owner when All users is selected", async () => {
    const { fn } = makeFetch({ statements: [statement({ user_id: 2 })] });
    await renderPanel(fn, null);
    expect(await screen.findByText("Sam")).toBeInTheDocument();
  });

  it("disables Re-import without a stored PDF and says why; Delete stays available", async () => {
    const { fn } = makeFetch({ statements: [statement({ has_pdf: false })] });
    await renderPanel(fn);
    const button = await screen.findByRole("button", { name: "Re-import chase-march.pdf" });
    expect(button).toBeDisabled();
    expect(screen.getByText(/No copy of the PDF was kept/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete chase-march.pdf" })).toBeEnabled();
  });

  it("Start re-import posts the same-extractor request, then reports edits kept", async () => {
    const { fn, calls } = makeFetch({ statements: [statement()] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    const panel = screen.getByRole("region", { name: /Re-import chase-march.pdf/ });
    expect(within(panel).getByRole("option", { name: "Same as before (built-in parser layout a credit)" })).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Start re-import" }));

    await waitFor(() => expect(calls.some((c) => c.url.includes("/statements/1/reparse"))).toBe(true));
    const post = calls.find((c) => c.url.includes("/reparse"))!;
    expect(post.body).toEqual({ discard_edits: false, remember: false, allow_empty: false });

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
    });
    const result = await screen.findByTestId("reimport-result");
    expect(result).toHaveTextContent("Re-imported: 42 transactions. 3 edits kept.");
  });

  it("sends the chosen layout spec and the discard option, and reports unmatched edits", async () => {
    const { fn, calls } = makeFetch({
      statements: [statement()],
      specs: [
        { id: 9, name: "chase-v2", version: 2, issuer_id: 3, approved: true, source: "user" },
        { id: 10, name: "other-bank", version: 1, issuer_id: 4, approved: true, source: "user" },
        { id: 11, name: "draft", version: 1, issuer_id: 3, approved: false, source: "user" },
      ],
      job: { id: "job-1", kind: "import", status: "done", progress: 1, inserted: 40, restored_edits: 1, unmatched_edits: 2, discarded_edits: 0 },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    const panel = screen.getByRole("region", { name: /Re-import chase-march.pdf/ });
    await waitFor(() => expect(within(panel).getByRole("option", { name: "chase-v2 (v2)" })).toBeInTheDocument());
    expect(within(panel).queryByRole("option", { name: /other-bank/ })).not.toBeInTheDocument();
    expect(within(panel).queryByRole("option", { name: /draft/ })).not.toBeInTheDocument();
    await userEvent.selectOptions(within(panel).getByRole("combobox"), "spec:9");
    await userEvent.click(within(panel).getByRole("checkbox", { name: /Discard my edits/ }));
    await userEvent.click(within(panel).getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/reparse"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/reparse"))!.body).toEqual({
      layout_spec_id: 9,
      discard_edits: true,
      allow_empty: false,
      remember: false,
    });
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
    });
    expect((await screen.findAllByText(/2 edits could not be matched/)).length).toBeGreaterThan(0);
  });

  it("offers the LLM extractor, with a privacy warning, only when an LLM is configured", async () => {
    const off = makeFetch({ statements: [statement()] });
    const view = await renderPanel(off.fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    expect(screen.queryByRole("option", { name: "LLM extractor" })).not.toBeInTheDocument();
    view.unmount();

    const on = makeFetch({ statements: [statement()], llm: "cloud" });
    await renderPanel(on.fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await waitFor(() => expect(screen.getByRole("option", { name: "LLM extractor" })).toBeInTheDocument());
    await userEvent.selectOptions(screen.getByRole("combobox"), "llm");
    expect(screen.getByText(/Sends this statement.s page text \(after redaction\)/)).toBeInTheDocument();
  });

  it("shows the server's sentence when the re-import is refused", async () => {
    const { fn } = makeFetch({
      statements: [statement()],
      reparse: { status: 409, body: { detail: "no copy of this statement's PDF was kept; delete it and import the PDF again" } },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no copy of this statement's PDF was kept");
    expect(screen.getByRole("button", { name: "Start re-import" })).toBeEnabled();
  });

  it("shows a failed job's detail", async () => {
    const { fn } = makeFetch({ statements: [statement()] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({
        data: JSON.stringify({ status: "error", progress: 1, error_detail: "extractor crashed" }),
      });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("extractor crashed");
  });

  it("Cancel closes the re-import panel", async () => {
    const { fn } = makeFetch({ statements: [statement()] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByTestId("reimport-panel")).not.toBeInTheDocument();
  });

  it("Delete asks first and removes the PDF file only when ticked (for a PDF kept in place)", async () => {
    const { fn, calls } = makeFetch({ statements: [statement({ pdf_in_place: true })] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Delete chase-march.pdf" }));
    const confirm = screen.getByRole("alertdialog");
    expect(confirm).toHaveTextContent("Delete this statement and its 42 transactions?");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(confirm).getByRole("checkbox", { name: "Also delete the PDF file" }));
    await userEvent.click(within(confirm).getByRole("button", { name: "Delete statement" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(calls.find((c) => c.method === "DELETE")!.url).toContain("delete_pdf=true");
    expect(await screen.findByText("Statement deleted")).toBeInTheDocument();
  });

  it("sends delete_pdf=false by default and hides the checkbox when the PDF is the app's own copy", async () => {
    const { fn, calls } = makeFetch({ statements: [statement()] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Delete chase-march.pdf" }));
    const confirm = screen.getByRole("alertdialog");
    expect(within(confirm).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(confirm).toHaveTextContent(/The app.s copy of the PDF is removed\./);
    await userEvent.click(within(confirm).getByRole("button", { name: "Delete statement" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE")).toBe(true));
    expect(calls.find((c) => c.method === "DELETE")!.url).toContain("delete_pdf=false");
  });

  it("shows the first ten and expands with Show all", async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      statement({ id: i + 1, original_name: `s${i + 1}.pdf`, period_end: `2026-01-${String(i + 10).padStart(2, "0")}` }),
    );
    const { fn } = makeFetch({ statements: many });
    await renderPanel(fn);
    expect(await screen.findAllByTestId("imported-statement")).toHaveLength(10);
    await userEvent.click(screen.getByRole("button", { name: "Show all (12)" }));
    expect(screen.getAllByTestId("imported-statement")).toHaveLength(12);
  });

  it("never sticks when the finished job's result cannot be read, refreshes, and lets the user retry", async () => {
    MockEventSource.instances = [];
    const { fn, calls } = makeFetch({ statements: [statement()], jobStatus: 500 });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    const before = calls.filter((c) => c.url.includes("/api/statements") && c.method === "GET").length;
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("result could not be read");
    expect(screen.getByRole("button", { name: "Start re-import" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeEnabled();
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("/api/statements") && c.method === "GET").length).toBeGreaterThan(before),
    );
  });

  it("refreshes again when the classification job it spawned finishes", async () => {
    MockEventSource.instances = [];
    const { fn, calls } = makeFetch({
      statements: [statement()],
      job: { id: "job-1", kind: "import", status: "done", progress: 1, inserted: 4, classify_job_id: "job-2" },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
    });
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(2));
    expect(MockEventSource.instances[1]!.url).toContain("/api/jobs/job-2/events");
    expect(await screen.findByText(/being categorized/)).toBeInTheDocument();
    const before = calls.filter((c) => c.url.includes("/api/statements") && c.method === "GET").length;
    act(() => {
      MockEventSource.instances[1]!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
    });
    await waitFor(() =>
      expect(calls.filter((c) => c.url.includes("/api/statements") && c.method === "GET").length).toBeGreaterThan(before),
    );
    expect(await screen.findByText(/have been categorized/)).toBeInTheDocument();
  });

  it("lists unassigned statements (no owner) with their status, Finish import and Delete", async () => {
    const onFinish = vi.fn();
    const { fn } = makeFetch({
      statements: [
        statement(),
        statement({ id: 8, user_id: 1, status: "awaiting_extractor", original_name: "waiting.pdf", txn_count: 0, period_end: null }),
      ],
    });
    await renderPanel(fn, 1, onFinish);
    const rows = await screen.findAllByTestId("imported-statement");
    expect(rows).toHaveLength(2);
    const waiting = rows.find((r) => r.textContent?.includes("waiting.pdf"))!;
    expect(waiting).toHaveTextContent("Not imported yet");
    expect(waiting).not.toHaveTextContent("No person yet");
    expect(within(waiting).getByRole("button", { name: "Delete waiting.pdf" })).toBeInTheDocument();
    await userEvent.click(within(waiting).getByRole("button", { name: "Finish import of waiting.pdf" }));
    expect(onFinish).toHaveBeenCalledWith(expect.objectContaining({ id: 8 }));
  });

  it("moves focus into the panels, closes them with Escape and returns focus to the trigger", async () => {
    const { fn } = makeFetch({ statements: [statement()] });
    await renderPanel(fn);
    const trigger = await screen.findByRole("button", { name: "Re-import chase-march.pdf" });
    await userEvent.click(trigger);
    expect(screen.getByRole("heading", { name: /Re-import chase-march.pdf/ })).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByTestId("reimport-panel")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Re-import chase-march.pdf" })).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: "Delete chase-march.pdf" }));
    expect(screen.getByRole("alertdialog")).toHaveFocus();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("button", { name: "Delete chase-march.pdf" })).toHaveFocus();
  });

  it("warns that the LLM is used when the recorded extractor is the LLM, and says it in words", async () => {
    const { fn } = makeFetch({ statements: [statement({ parser_id: "llm" })], llm: "cloud" });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    expect(screen.getByRole("option", { name: "Same as before (LLM extractor)" })).toBeInTheDocument();
    expect(screen.getByText(/Sends this statement.s page text \(after redaction\)/)).toBeInTheDocument();
    expect(screen.getByText(/corrections\s+to dates, amounts and descriptions are kept/)).toBeInTheDocument();
  });
});

const ZERO_ROW = {
  id: 4,
  original_name: "empty.pdf",
  txn_count: 0,
  parsed_row_count: 0,
  attention: ["no_transactions", "layout_drift"],
  shape_warnings: JSON.stringify(["No transactions were found in the selected pages."]),
  parser_id: null,
  layout_spec_id: 12,
};

const CANDIDATES = [
  { kind: "spec", parser_id: null, layout_spec_id: 12, label: "Wrong B (v1)", score: 0.4, recorded: true, remembered: false },
  { kind: "parser", parser_id: "layout_b_credit", layout_spec_id: null, label: "Layout B credit", score: 0.95, recorded: false, remembered: false },
  { kind: "llm", parser_id: "llm", layout_spec_id: null, label: "LLM extractor", score: null, recorded: false, remembered: false },
];

describe("zero-row and attention display", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("flags a zero-row statement instead of calling it imported, with a primary Re-import", async () => {
    const { fn } = makeFetch({ statements: [statement(ZERO_ROW)] });
    await renderPanel(fn);
    const row = await screen.findByTestId("imported-statement");
    expect(within(row).getByText("No transactions found")).toBeInTheDocument();
    expect(within(row).queryByText("Imported")).not.toBeInTheDocument();
    expect(within(row).getByTestId("attention-no_transactions")).toHaveTextContent(
      "No transactions were found. The wrong extractor or pages were probably used.",
    );
    // The server's own shape warning is not repeated next to it.
    expect(within(row).queryByText("No transactions were found in the selected pages.")).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Re-import empty.pdf" })).toBeEnabled();
  });

  it("keeps 'Imported' and adds an info line when everything was already on file", async () => {
    const { fn } = makeFetch({
      statements: [statement({ txn_count: 0, parsed_row_count: 12, attention: ["all_duplicates"] })],
    });
    await renderPanel(fn);
    const row = await screen.findByTestId("imported-statement");
    expect(within(row).getByText("Imported")).toBeInTheDocument();
    expect(within(row).getByTestId("attention-all_duplicates")).toHaveTextContent(
      "All 12 transactions were already on file from another statement.",
    );
  });

  it("lists each layout-drift warning", () => {
    const notes = attentionNotes(
      statement({ attention: ["layout_drift"], shape_warnings: JSON.stringify(["missing section: PAYMENTS", "extra column"]) }) as never,
    );
    expect(notes.map((n) => n.text)).toEqual(["missing section: PAYMENTS", "extra column"]);
  });

  it("shows a currency badge only when it differs from the default currency", async () => {
    const world = { statements: [statement({ currency: "EUR" }), statement({ id: 2, currency: "USD", original_name: "b.pdf", period_end: "2026-02-28" })] };
    const { fn } = makeFetch(world);
    const inner = fn.getMockImplementation()!;
    fn.mockImplementation(async (input, init) => {
      const request = await describeRequest(input);
      if (request.url.includes("/api/settings")) return jsonResponse({ llm: { mode: "none" }, ingest: { default_currency: "USD" } });
      return inner(input, init);
    });
    await renderPanel(fn);
    const rows = await screen.findAllByTestId("imported-statement");
    await waitFor(() => expect(within(rows[0]!).getByText("EUR")).toBeInTheDocument());
    expect(within(rows[1]!).queryByText("USD")).not.toBeInTheDocument();
  });

  it("disables Re-import with the reason when the PDF is gone from the documents folder", async () => {
    const { fn } = makeFetch({ statements: [statement({ pdf_missing: true, attention: ["pdf_missing"] })] });
    await renderPanel(fn);
    const button = await screen.findByRole("button", { name: "Re-import chase-march.pdf" });
    expect(button).toBeDisabled();
    expect(screen.getByText(/PDF not found in your documents folder/)).toBeInTheDocument();
    expect(button).toHaveAccessibleDescription(/Put it back/);
  });
});

describe("re-import panel: extractors, pages and data-loss protection", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("preselects the best other extractor for a zero-row statement and sends it", async () => {
    const { fn, calls } = makeFetch({ statements: [statement(ZERO_ROW)], candidates: CANDIDATES, llm: "cloud" });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import empty.pdf" }));
    const panel = screen.getByRole("region", { name: /Re-import empty.pdf/ });
    const select = await within(panel).findByRole("combobox", { name: /Extractor/ });
    await waitFor(() => expect(select).toHaveValue("parser:layout_b_credit"));
    expect(within(panel).getByRole("option", { name: "Wrong B (v1) (40% match) · recorded" })).toBeInTheDocument();
    expect(within(panel).getByRole("option", { name: "Layout B credit (95% match)" })).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "Map a new layout" })).toHaveAttribute(
      "href",
      "/spending/layout-mapper?statement_id=4",
    );
    // Nothing to keep, so no "discard my edits".
    expect(within(panel).queryByRole("checkbox", { name: /Discard my edits/ })).not.toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/reparse"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/reparse"))!.body).toEqual({
      parser_id: "layout_b_credit",
      discard_edits: false,
      remember: false,
      allow_empty: false,
    });
  });

  it("keeps the recorded extractor preselected when it read rows, and warns before the LLM", async () => {
    const { fn } = makeFetch({
      statements: [statement({ layout_spec_id: 12, parser_id: null })],
      candidates: CANDIDATES,
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    const select = await screen.findByRole("combobox", { name: /Extractor/ });
    await waitFor(() => expect(select).toHaveValue("spec:12"));
    await userEvent.selectOptions(select, "llm");
    expect(screen.getByText(/Sends this statement.s page text/)).toBeInTheDocument();
  });

  it("validates the pages and sends them only when changed", async () => {
    const { fn, calls } = makeFetch({ statements: [statement({ page_count: 5, selected_pages: [1, 2] })] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    const pages = await screen.findByRole("textbox", { name: /Pages/ });
    expect(pages).toHaveValue("1-2");
    expect(screen.getByText(/of 5/)).toBeInTheDocument();
    const start = screen.getByRole("button", { name: "Start re-import" });

    await userEvent.clear(pages);
    expect(start).toBeDisabled();
    await userEvent.type(pages, "9");
    expect(start).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(/between 1 and 5/);
    await userEvent.clear(pages);
    await userEvent.type(pages, "1-2");
    expect(start).toBeEnabled();
    await userEvent.click(start);
    await waitFor(() => expect(calls.some((c) => c.url.includes("/reparse"))).toBe(true));
    expect(calls.find((c) => c.url.includes("/reparse"))!.body).not.toHaveProperty("pages");
  });

  it("sends changed pages", async () => {
    const { fn, calls } = makeFetch({ statements: [statement({ page_count: 5, selected_pages: [1, 2] })] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    const pages = await screen.findByRole("textbox", { name: /Pages/ });
    await userEvent.clear(pages);
    await userEvent.type(pages, "2-4");
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/reparse"))).toBe(true));
    expect((calls.find((c) => c.url.includes("/reparse"))!.body as { pages: number[] }).pages).toEqual([2, 3, 4]);
  });

  it("never wipes silently: an empty result asks first and only then resends with allow_empty", async () => {
    const { fn, calls } = makeFetch({
      statements: [statement({ txn_count: 31 })],
      reparseSequence: [
        { status: 202, body: { job_id: "job-1" } },
        { status: 202, body: { job_id: "job-3" } },
      ],
      jobs: {
        "job-1": {
          id: "job-1",
          kind: "import",
          status: "error",
          progress: 1,
          error_code: "empty_result",
          error_detail: "The re-import found no transactions, so nothing was changed. Your 31 transactions are kept.",
        },
        "job-3": { id: "job-3", kind: "import", status: "done", progress: 1, inserted: 0, parsed_count: 0, previous_txn_count: 31 },
      },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({
        data: JSON.stringify({ status: "error", progress: 1, error_detail: "The re-import found no transactions" }),
      });
    });
    const confirm = await screen.findByTestId("empty-result-confirm");
    expect(confirm).toHaveFocus();
    expect(confirm).toHaveTextContent("The re-import found no transactions. Your 31 transactions are kept.");
    expect(confirm).toHaveTextContent("Replace them with nothing anyway?");
    // Not shown as a plain error, and nothing was resent yet.
    expect(calls.filter((c) => c.url.includes("/reparse"))).toHaveLength(1);

    await userEvent.click(within(confirm).getByRole("button", { name: "Re-import anyway (removes 31 transactions)" }));
    await waitFor(() => expect(calls.filter((c) => c.url.includes("/reparse"))).toHaveLength(2));
    expect(calls.filter((c) => c.url.includes("/reparse"))[1]!.body).toMatchObject({ allow_empty: true });
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(2));
    act(() => {
      MockEventSource.instances[1]!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
    });
    const result = await screen.findByTestId("reimport-result");
    expect(result).toHaveTextContent("Re-imported: 0 transactions (was 31).");
    expect(result).toHaveTextContent("No transactions were found.");
  });

  it("'Keep my transactions' backs out of the empty-result confirmation", async () => {
    const { fn, calls } = makeFetch({
      statements: [statement({ txn_count: 3 })],
      jobs: {
        "job-1": { id: "job-1", kind: "import", status: "error", progress: 1, error_code: "empty_result", error_detail: "kept" },
      },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    act(() => {
      MockEventSource.instances[0]!.onmessage?.({ data: JSON.stringify({ status: "error", progress: 1 }) });
    });
    await userEvent.click(await screen.findByRole("button", { name: "Keep my transactions" }));
    expect(screen.queryByTestId("empty-result-confirm")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start re-import" })).toBeEnabled();
    expect(calls.filter((c) => c.url.includes("/reparse"))).toHaveLength(1);
  });

  it("shows the PDF-missing sentence for a synchronous 409 pdf_not_found", async () => {
    const { fn } = makeFetch({
      statements: [statement()],
      reparse: { status: 409, body: { detail: { code: "pdf_not_found", message: "raw server text" } } },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/PDF not found in your documents folder/);
  });

  it("shows the server's sentence for a synchronous 400 (bad pages)", async () => {
    const { fn } = makeFetch({
      statements: [statement()],
      reparse: { status: 400, body: { detail: "page 99 is out of range" } },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("page 99 is out of range");
  });

  it("keeps following the job after the panel is closed while the request is still pending", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const { fn, calls } = makeFetch({
      statements: [statement()],
      jobs: { "job-1": { id: "job-1", kind: "import", status: "done", progress: 1, inserted: 3, classify_job_id: "job-2" } },
    });
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (release = resolve));
    const inner = fn.getMockImplementation()!;
    let requested = false;
    fn.mockImplementation(async (input, init) => {
      if (input instanceof Request && input.url.includes("/reparse")) {
        requested = true;
        await gate;
      }
      return inner(input, init);
    });
    const view = await renderPanel(fn, 1, undefined, { queryClient });
    await userEvent.click(await screen.findByRole("button", { name: "Re-import chase-march.pdf" }));
    await userEvent.click(screen.getByRole("button", { name: "Start re-import" }));
    await waitFor(() => expect(requested).toBe(true));
    view.unmount();
    invalidate.mockClear();
    release();
    await waitFor(() => expect(calls.some((c) => c.url.includes("/api/jobs/job-2"))).toBe(true));
    await waitFor(() => expect(invalidate.mock.calls.length).toBeGreaterThanOrEqual(2));
  });
});

describe("followInBackground", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("follows the import job and then its classify job, refreshing after each, even from a pending promise", async () => {
    const { fn, calls } = makeFetch({
      statements: [],
      jobs: {
        "job-1": { id: "job-1", kind: "import", status: "done", progress: 1, classify_job_id: "job-2" },
        "job-2": { id: "job-2", kind: "classify", status: "done", progress: 1 },
      },
    });
    vi.stubGlobal("fetch", fn);
    vi.resetModules();
    const { followInBackground: follow } = await import("./imported-statements-api");
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await follow(client, Promise.resolve("job-1"));
    expect(calls.filter((c) => c.url.includes("/api/jobs/")).map((c) => c.url.split("/api/jobs/")[1])).toEqual(["job-1", "job-2"]);
    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it("polls until the job ends and never throws on a failed request", async () => {
    vi.useFakeTimers();
    let polls = 0;
    const fn = vi.fn<typeof fetch>(() => {
      polls += 1;
      return Promise.resolve(jsonResponse({ id: "j", kind: "import", status: polls < 3 ? "running" : "done", progress: 0.5 }));
    });
    vi.stubGlobal("fetch", fn);
    vi.resetModules();
    const { followInBackground: follow } = await import("./imported-statements-api");
    const client = new QueryClient();
    const done = follow(client, "j");
    await vi.advanceTimersByTimeAsync(3000);
    await done;
    expect(polls).toBe(3);
    vi.useRealTimers();
    await expect(follow(client, Promise.reject(new Error("request failed")))).resolves.toBeUndefined();
  });
});

describe("open requests, person and delete warnings", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("opens a requested row once and does not reopen it when the row remounts", async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      statement({ id: i + 1, original_name: `s${i + 1}.pdf`, period_end: `2026-01-${String(i + 10).padStart(2, "0")}` }),
    );
    const handled = vi.fn();
    const { fn } = makeFetch({ statements: many });
    await renderPanel(fn, 1, undefined, { request: { statementId: 2, nonce: 1 }, onRequestHandled: handled });
    // s2.pdf is beyond the first ten: the panel expands to reach it and opens its Re-import.
    expect(await screen.findByRole("region", { name: /Re-import s2.pdf/ })).toBeInTheDocument();
    expect(handled).toHaveBeenCalledTimes(1);
    expect(handled).toHaveBeenCalledWith(1);

    await userEvent.click(within(screen.getByRole("region", { name: /Re-import s2.pdf/ })).getByRole("button", { name: "Cancel" }));
    // Collapse and expand: the row remounts, but the (still present) request is not taken again.
    await userEvent.click(screen.getByRole("button", { name: "Show fewer" }));
    await userEvent.click(screen.getByRole("button", { name: /Show all/ }));
    expect(screen.queryByTestId("reimport-panel")).not.toBeInTheDocument();
    expect(handled).toHaveBeenCalledTimes(1);
  });

  it("lists only the selected person's statements and never badges an uploaded-for statement", async () => {
    const { fn } = makeFetch({
      statements: [
        statement(),
        statement({ id: 8, user_id: 2, status: "awaiting_extractor", original_name: "sams-waiting.pdf", account_id: null, txn_count: null }),
        statement({ id: 9, user_id: null, status: "error", original_name: "orphan.pdf", account_id: null, txn_count: null }),
      ],
    });
    await renderPanel(fn, 1);
    const rows = await screen.findAllByTestId("imported-statement");
    expect(rows).toHaveLength(1);
    expect(screen.queryByText("sams-waiting.pdf")).not.toBeInTheDocument();
    expect(screen.queryByText("No person yet")).not.toBeInTheDocument();
    // With All users selected the person-less one is the only one badged.
    cleanup();
    await renderPanel(fn, null);
    const all = await screen.findAllByTestId("imported-statement");
    expect(all).toHaveLength(3);
    expect(screen.getAllByText("No person yet")).toHaveLength(1);
  });

  it("changes the person with PATCH {user_id} and reports what moved", async () => {
    const { fn, calls } = makeFetch({
      statements: [statement()],
      patch: { status: 200, body: { ...statement({ user_id: 2 }), rows_moved: 42, duplicates_deleted: 3 } },
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Change person for chase-march.pdf" }));
    const panel = screen.getByRole("region", { name: "Change person for chase-march.pdf" });
    expect(panel).toHaveFocus();
    const select = within(panel).getByRole("combobox", { name: /Move to/ });
    // Only the other people are offered.
    expect(within(select).queryByRole("option", { name: "Alex" })).not.toBeInTheDocument();
    await userEvent.selectOptions(select, "2");
    await userEvent.click(within(panel).getByRole("button", { name: "Move statement" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toContain("/api/statements/1");
    expect(patch.body).toEqual({ user_id: 2 });
    expect(await screen.findByText("Moved to Sam: 42 transactions (3 duplicates merged)")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: /Change person for/ })).not.toBeInTheDocument();
  });

  it("offers Change person only to statements that are imported, and only with more than one person", async () => {
    const { fn } = makeFetch({
      statements: [statement({ id: 8, status: "error", original_name: "bad.pdf", txn_count: null })],
    });
    await renderPanel(fn);
    await screen.findByText("bad.pdf");
    expect(screen.queryByRole("button", { name: /Change person/ })).not.toBeInTheDocument();
  });

  it("names overlapping statements of the same account in the delete confirmation, and only those", async () => {
    const { fn } = makeFetch({
      statements: [
        statement({ id: 1, original_name: "march.pdf", period_start: "2026-03-01", period_end: "2026-03-31" }),
        statement({ id: 2, original_name: "march-b.pdf", period_start: "2026-03-15", period_end: "2026-04-14", created_at: "2026-04-02T10:00:00Z" }),
        statement({ id: 3, original_name: "other-account.pdf", account_id: 6, period_start: "2026-03-01", period_end: "2026-03-31" }),
        statement({ id: 4, original_name: "january.pdf", period_start: "2026-01-01", period_end: "2026-01-31" }),
      ],
    });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Delete march.pdf" }));
    const note = within(screen.getByRole("alertdialog")).getByTestId("delete-overlap-note");
    expect(note).toHaveTextContent("shares with march-b.pdf are stored only once");
    expect(note).not.toHaveTextContent("other-account.pdf");
    expect(note).not.toHaveTextContent("january.pdf");
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Cancel" }));
    await userEvent.click(screen.getByRole("button", { name: "Delete january.pdf" }));
    expect(within(screen.getByRole("alertdialog")).queryByTestId("delete-overlap-note")).not.toBeInTheDocument();
  });
});
