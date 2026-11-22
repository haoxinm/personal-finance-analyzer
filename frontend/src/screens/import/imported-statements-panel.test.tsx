import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { describeReimport, formatPeriod, sortStatements } from "./statement-format";

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
}

function makeFetch(world: World) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fn = vi.fn<typeof fetch>(async (input) => {
    const request = await describeRequest(input);
    calls.push(request);
    const { url, method } = request;
    if (url.includes("/reparse") && method === "POST") {
      const answer = world.reparse ?? { status: 202, body: { job_id: "job-1" } };
      return jsonResponse(answer.body, answer.status);
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

async function renderPanel(fetchMock: typeof fetch, userId: number | null = 1, onFinishImport?: (s: { id: number }) => void) {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { ImportedStatementsPanel } = await import("./imported-statements-panel");
  const { Toaster } = await import("@/components/ui/toaster");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastContextProvider>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <ImportedStatementsPanel userId={userId} onFinishImport={onFinishImport as never} />
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
        statement({ id: 8, user_id: null, status: "awaiting_extractor", original_name: "waiting.pdf", txn_count: 0, period_end: null }),
      ],
    });
    await renderPanel(fn, 1, onFinish);
    const rows = await screen.findAllByTestId("imported-statement");
    expect(rows).toHaveLength(2);
    const waiting = rows.find((r) => r.textContent?.includes("waiting.pdf"))!;
    expect(waiting).toHaveTextContent("Not imported yet");
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
