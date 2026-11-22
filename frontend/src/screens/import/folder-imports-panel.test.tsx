import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function describeRequest(input: RequestInfo | URL): { url: string; method: string } {
  if (input instanceof Request) return { url: input.url, method: input.method };
  return { url: input.toString(), method: "GET" };
}

const BASE_STATUS = {
  enabled: true,
  interval_seconds: 60,
  documents_dir: "/pfa/documents",
  running: false,
  next_scan_at: "2026-11-09T12:01:00+00:00",
  last_scan: {
    started_at: "2026-11-09T12:00:00+00:00",
    finished_at: "2026-11-09T12:00:02+00:00",
    scanned: 12,
    imported: 2,
    known: 0,
    failed: 1,
    ignored: 0,
    waiting: 0,
    busy: false,
    error: null,
  },
  problem: null,
};

const IMPORTED = {
  id: 1,
  path: "Alex/Statements/Chase/jan.pdf",
  outcome: "imported" as const,
  code: null,
  message: "Imported 40 transaction(s).",
  hint: null,
  kind: "statement" as const,
  folder_person: "Alex",
  user_id: 1,
  statement_id: 7,
  tax_document_id: null,
  warnings: [],
  first_seen: "2026-11-09T12:00:01+00:00",
  last_seen: "2026-11-09T12:00:01+00:00",
};
const TAX = {
  ...IMPORTED,
  id: 2,
  path: "Alex/Tax/2021/W-2.pdf",
  kind: "tax" as const,
  statement_id: null,
  tax_document_id: 4,
  message: "Imported W-2 for 2024.",
  warnings: ["The form is for tax year 2024, but it is in the 2021 folder."],
};
const PROBLEM = {
  id: 3,
  path: "Taylor/Statements/jan.pdf",
  outcome: "failed" as const,
  code: "unknown_person",
  message: 'No person is named "Taylor".',
  hint: 'Create a person named "Taylor" in the app, or rename the folder to an existing person.',
  kind: "statement" as const,
  folder_person: "Taylor",
  user_id: null,
  statement_id: null,
  tax_document_id: null,
  warnings: [],
  first_seen: "2026-11-09T12:00:01+00:00",
  last_seen: "2026-11-09T12:00:01+00:00",
};

interface Fixture {
  status: Record<string, unknown>;
  recent: unknown[];
  problems: unknown[];
  dismissed?: unknown[];
  /** Force an answer (status code) for any POST whose URL contains the key. */
  failPost?: Record<string, number>;
}

function makeFetch(fixture: Fixture) {
  fixture.dismissed ??= [];
  const calls: { url: string; method: string }[] = [];
  const move = (from: unknown[], to: unknown[], id: number | null) => {
    const moving = from.filter((f) => id === null || (f as { id: number }).id === id);
    from.splice(0, from.length, ...from.filter((f) => !moving.includes(f)));
    to.push(...moving);
    return moving.length;
  };
  const fn = vi.fn<typeof fetch>((input) => {
    const { url, method } = describeRequest(input);
    calls.push({ url, method });
    if (method === "POST") {
      for (const [key, status] of Object.entries(fixture.failPost ?? {})) {
        if (url.includes(key)) return Promise.resolve(jsonResponse({ detail: "the server said no" }, status));
      }
    }
    if (url.includes("/api/watch/status")) return Promise.resolve(jsonResponse(fixture.status));
    if (url.includes("/api/watch/files/dismiss-all") && method === "POST") {
      const n = move(fixture.problems, fixture.dismissed!, null);
      return Promise.resolve(jsonResponse({ dismissed: n }));
    }
    const action = /\/api\/watch\/files\/(\d+)\/(dismiss|undismiss|retry)/.exec(url);
    if (action && method === "POST") {
      const id = Number(action[1]);
      if (action[2] === "retry") fixture.problems = [];
      if (action[2] === "dismiss") move(fixture.problems, fixture.dismissed!, id);
      if (action[2] === "undismiss") move(fixture.dismissed!, fixture.problems, id);
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (url.includes("/api/watch/files")) {
      const items = url.includes("view=problems")
        ? fixture.problems
        : url.includes("view=dismissed")
          ? fixture.dismissed!
          : fixture.recent;
      return Promise.resolve(jsonResponse({ items, total: items.length }));
    }
    if (url.includes("/api/watch/scan") && method === "POST") {
      fixture.status = { ...fixture.status, running: true };
      return Promise.resolve(jsonResponse({ started: true, status: fixture.status }, 202));
    }
    return Promise.resolve(jsonResponse({ detail: `unhandled: ${method} ${url}` }, 404));
  });
  return { fn, calls };
}

async function renderPanel(fetchMock: typeof fetch, onReimportStatement?: (id: number) => void) {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { FolderImportsPanel } = await import("./folder-imports-panel");
  // Same module graph as the panel (resetModules above), or the toast context would differ.
  const { Toaster } = await import("@/components/ui/toaster");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <ToastContextProvider>
        <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
          <FolderImportsPanel onReimportStatement={onReimportStatement} />
        </MemoryRouter>
        <Toaster />
      </ToastContextProvider>
    </QueryClientProvider>,
  );
}

describe("FolderImportsPanel", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows whether watching is on, the last and next scan, and what was imported", async () => {
    const { fn } = makeFetch({ status: BASE_STATUS, recent: [IMPORTED, TAX], problems: [] });
    await renderPanel(fn);

    const panel = await screen.findByRole("region", { name: "Folder imports" });
    await waitFor(() => expect(within(panel).getByTestId("folder-imports-state")).toHaveTextContent("On"));
    const last = within(panel).getByTestId("folder-imports-last-scan");
    expect(last).toHaveTextContent("2 imported, 1 needs attention (12 PDFs in the folder).");
    expect(last).toHaveTextContent("Next scan:");
    expect(within(panel).getByText("/pfa/documents")).toBeInTheDocument();

    const rows = await within(panel).findAllByTestId("folder-import-recent");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText("Alex/Statements/Chase/jan.pdf")).toBeInTheDocument();
    expect(within(rows[0]!).getByRole("link", { name: "View transactions" })).toHaveAttribute(
      "href",
      "/spending/transactions?statement_id=7&include_non_spend=true",
    );
    expect(within(rows[1]!).getByRole("link", { name: "View tax documents" })).toHaveAttribute("href", "/tax/documents");
    expect(within(rows[1]!).getByText(/in the 2021 folder/)).toBeInTheDocument();
    expect(within(panel).getByText("Nothing needs attention.")).toBeInTheDocument();
  });

  it("says it is off and points at Settings", async () => {
    const { fn } = makeFetch({
      status: { ...BASE_STATUS, enabled: false, next_scan_at: null, last_scan: null },
      recent: [],
      problems: [],
    });
    await renderPanel(fn);
    const panel = await screen.findByRole("region", { name: "Folder imports" });
    await waitFor(() => expect(within(panel).getByTestId("folder-imports-state")).toHaveTextContent("Off"));
    expect(within(panel).getByRole("link", { name: "Turn on in Settings" })).toHaveAttribute(
      "href",
      "/settings#folder-imports",
    );
    expect(within(panel).getByTestId("folder-imports-last-scan")).toHaveTextContent("No scan has run yet.");
    expect(within(panel).getByText("Nothing has been imported from the folder yet.")).toBeInTheDocument();
  });

  it("lists problems with the reason and hint, and Retry clears one", async () => {
    const fixture: Fixture = { status: BASE_STATUS, recent: [], problems: [PROBLEM] };
    const { fn, calls } = makeFetch(fixture);
    await renderPanel(fn);

    const row = await screen.findByTestId("folder-import-problem");
    expect(within(row).getByText("Taylor/Statements/jan.pdf")).toBeInTheDocument();
    expect(within(row).getByText('No person is named "Taylor".')).toBeInTheDocument();
    expect(within(row).getByText(/Create a person named "Taylor"/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Needs attention (1)" })).toBeInTheDocument();

    await userEvent.click(within(row).getByRole("button", { name: "Retry Taylor/Statements/jan.pdf" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/api/watch/files/3/retry"))).toBe(true),
    );
    await waitFor(() => expect(screen.queryByTestId("folder-import-problem")).not.toBeInTheDocument());
    expect(screen.getByText("Nothing needs attention.")).toBeInTheDocument();
  });

  it("offers Re-import, not Retry, for a statement that was read but had no transactions", async () => {
    const empty = {
      ...PROBLEM,
      id: 6,
      path: "Alex/Statements/Chase/empty.pdf",
      code: "no_transactions",
      message: "The statement was read, but no transactions were found.",
      hint: "Re-import it with another extractor or other pages.",
      statement_id: 21,
    };
    const { fn, calls } = makeFetch({ status: BASE_STATUS, recent: [], problems: [empty] });
    const onReimport = vi.fn();
    await renderPanel(fn, onReimport);
    const row = await screen.findByTestId("folder-import-problem");
    expect(within(row).queryByRole("button", { name: /^Retry / })).not.toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Re-import Alex/Statements/Chase/empty.pdf" }));
    expect(onReimport).toHaveBeenCalledWith(21);
    expect(calls.some((c) => c.url.includes("/retry"))).toBe(false);
    expect(within(row).getByRole("button", { name: "Dismiss Alex/Statements/Chase/empty.pdf" })).toBeInTheDocument();
  });

  it("offers Import again for a file whose document was deleted in the app", async () => {
    const deleted = {
      ...PROBLEM,
      id: 5,
      path: "Alex/Statements/Chase/jan.pdf",
      outcome: "ignored" as const,
      code: "deleted_in_app",
      message: "You deleted this document in the app; the PDF file was left where it is.",
      hint: "It is not imported again unless the file changes.",
    };
    const { fn, calls } = makeFetch({ status: BASE_STATUS, recent: [], problems: [deleted] });
    await renderPanel(fn);
    const row = await screen.findByTestId("folder-import-problem");
    expect(within(row).queryByRole("button", { name: /^Retry / })).not.toBeInTheDocument();
    await userEvent.click(
      within(row).getByRole("button", { name: "Import again Alex/Statements/Chase/jan.pdf" }),
    );
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/api/watch/files/5/retry"))).toBe(true),
    );
  });

  it("Scan now starts a scan and shows it running", async () => {
    const { fn, calls } = makeFetch({ status: BASE_STATUS, recent: [], problems: [] });
    await renderPanel(fn);
    const button = await screen.findByRole("button", { name: "Scan now" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/api/watch/scan"))).toBe(true));
    const scanning = await screen.findByRole("button", { name: "Scanning…" });
    expect(scanning).toBeDisabled();
    expect(scanning).toHaveAttribute("aria-busy", "true");
  });

  it("reports a problem that stops scans (for example an old database)", async () => {
    const { fn } = makeFetch({
      status: { ...BASE_STATUS, problem: "The database is not up to date: run `pfa migrate`, then restart." },
      recent: [],
      problems: [],
    });
    await renderPanel(fn);
    expect(await screen.findByRole("alert")).toHaveTextContent("pfa migrate");
  });

  it("shows an error instead of crashing when the status cannot be loaded", async () => {
    const fn = vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse({ detail: "boom" }, 500)));
    await renderPanel(fn);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load the folder-import status");
  });

  it("Dismiss hides a failed file, offers Undo, and the Dismissed list can bring it back", async () => {
    const fixture: Fixture = { status: BASE_STATUS, recent: [], problems: [PROBLEM] };
    const { fn, calls } = makeFetch(fixture);
    await renderPanel(fn);

    const row = await screen.findByTestId("folder-import-problem");
    await userEvent.click(within(row).getByRole("button", { name: "Dismiss Taylor/Statements/jan.pdf" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/api/watch/files/3/dismiss"))).toBe(true),
    );
    await waitFor(() => expect(screen.queryByTestId("folder-import-problem")).not.toBeInTheDocument());
    expect(screen.getByText("Nothing needs attention.")).toBeInTheDocument();
    expect(await screen.findByText("It will show again if the file changes.")).toBeInTheDocument();

    // The collapsed "Dismissed" list holds it.
    const dismissed = await screen.findByTestId("folder-imports-dismissed");
    expect(dismissed).not.toHaveAttribute("open");
    expect(within(dismissed).getByText("Dismissed (1)")).toBeInTheDocument();

    // Undo from the toast puts it back.
    await userEvent.click(screen.getByRole("button", { name: "Undo" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/api/watch/files/3/undismiss"))).toBe(true),
    );
    expect(await screen.findByTestId("folder-import-problem")).toBeInTheDocument();
    expect(screen.queryByTestId("folder-imports-dismissed")).not.toBeInTheDocument();
  });

  it("Show again restores a dismissed file", async () => {
    const fixture: Fixture = { status: BASE_STATUS, recent: [], problems: [], dismissed: [PROBLEM] };
    const { fn, calls } = makeFetch(fixture);
    await renderPanel(fn);
    const dismissed = await screen.findByTestId("folder-imports-dismissed");
    await userEvent.click(within(dismissed).getByText("Dismissed (1)"));
    await userEvent.click(within(dismissed).getByRole("button", { name: "Show again Taylor/Statements/jan.pdf" }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "POST" && c.url.includes("/api/watch/files/3/undismiss"))).toBe(true),
    );
    expect(await screen.findByTestId("folder-import-problem")).toBeInTheDocument();
  });

  it("Dismiss all asks first, then dismisses every problem", async () => {
    const second = { ...PROBLEM, id: 4, path: "Taylor/Statements/feb.pdf" };
    const fixture: Fixture = { status: BASE_STATUS, recent: [], problems: [PROBLEM, second] };
    const { fn, calls } = makeFetch(fixture);
    await renderPanel(fn);
    await screen.findAllByTestId("folder-import-problem");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss all" }));
    const confirm = screen.getByRole("alertdialog", { name: "Confirm dismiss all" });
    expect(confirm).toHaveTextContent("Dismiss 2 files? They show again if they change.");
    expect(calls.some((c) => c.url.includes("dismiss-all"))).toBe(false);

    await userEvent.click(within(confirm).getByRole("button", { name: "Dismiss all" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.includes("dismiss-all"))).toBe(true));
    await waitFor(() => expect(screen.queryByTestId("folder-import-problem")).not.toBeInTheDocument());
    expect(await screen.findByText("Dismissed 2 files")).toBeInTheDocument();
    expect(await screen.findByText("Dismissed (2)")).toBeInTheDocument();
  });

  it("does not offer Dismiss all for a single problem, and Cancel closes the confirmation", async () => {
    const second = { ...PROBLEM, id: 4, path: "Taylor/Statements/feb.pdf" };
    const single = makeFetch({ status: BASE_STATUS, recent: [], problems: [PROBLEM] });
    const view = await renderPanel(single.fn);
    await screen.findByTestId("folder-import-problem");
    expect(screen.queryByRole("button", { name: "Dismiss all" })).not.toBeInTheDocument();
    view.unmount();

    const two = makeFetch({ status: BASE_STATUS, recent: [], problems: [PROBLEM, second] });
    await renderPanel(two.fn);
    await screen.findAllByTestId("folder-import-problem");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss all" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(two.calls.some((c) => c.url.includes("dismiss-all"))).toBe(false);
  });

  it("shows an error toast when Retry fails and keeps the row", async () => {
    const { fn } = makeFetch({
      status: BASE_STATUS,
      recent: [],
      problems: [PROBLEM],
      failPost: { "/retry": 500 },
    });
    await renderPanel(fn);
    const row = await screen.findByTestId("folder-import-problem");
    await userEvent.click(within(row).getByRole("button", { name: "Retry Taylor/Statements/jan.pdf" }));
    expect(await screen.findByText("Could not retry")).toBeInTheDocument();
    expect(screen.getByText("the server said no")).toBeInTheDocument();
    expect(screen.getByTestId("folder-import-problem")).toBeInTheDocument();
  });

  it("shows an error toast when Dismiss or Scan now fails", async () => {
    const { fn } = makeFetch({
      status: BASE_STATUS,
      recent: [],
      problems: [PROBLEM],
      failPost: { "/dismiss": 404, "/watch/scan": 500 },
    });
    await renderPanel(fn);
    const row = await screen.findByTestId("folder-import-problem");
    await userEvent.click(within(row).getByRole("button", { name: /^Dismiss / }));
    expect(await screen.findByText("Could not dismiss")).toBeInTheDocument();
    expect(screen.getByTestId("folder-import-problem")).toBeInTheDocument();

    const scanButton = await screen.findByRole("button", { name: "Scan now" });
    await userEvent.click(scanButton);
    expect(await screen.findByText("Could not start a scan")).toBeInTheDocument();
  });

  it("disables only the Retry/Dismiss of the row that is in flight", async () => {
    const second = { ...PROBLEM, id: 4, path: "Taylor/Statements/feb.pdf" };
    const fixture: Fixture = { status: BASE_STATUS, recent: [], problems: [PROBLEM, second] };
    const { fn } = makeFetch(fixture);
    // Hold the retry answer so the mutation stays pending.
    let release: () => void = () => undefined;
    const base = fn.getMockImplementation()!;
    fn.mockImplementation((input, init) => {
      const { url, method } = describeRequest(input);
      if (method === "POST" && url.includes("/files/3/retry")) {
        return new Promise<Response>((resolve) => {
          release = () => resolve(new Response(null, { status: 204 }));
        });
      }
      return base(input, init);
    });
    await renderPanel(fn);
    const rows = await screen.findAllByTestId("folder-import-problem");
    await userEvent.click(within(rows[0]!).getByRole("button", { name: /^Retry / }));
    await waitFor(() => expect(within(rows[0]!).getByRole("button", { name: /^Retry / })).toBeDisabled());
    expect(within(rows[1]!).getByRole("button", { name: /^Retry / })).toBeEnabled();
    expect(within(rows[1]!).getByRole("button", { name: /^Dismiss / })).toBeEnabled();
    release();
  });

  it("confirms Dismiss all with focus, Escape cancels back to the trigger, and names the file on a single dismiss", async () => {
    const second = { ...PROBLEM, id: 5, path: "Taylor/Statements/feb.pdf" };
    const { fn } = makeFetch({ status: BASE_STATUS, recent: [], problems: [PROBLEM, second] });
    await renderPanel(fn);
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss all" }));
    expect(screen.getByRole("alertdialog")).toHaveFocus();
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Dismiss 2 files?");
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss all" })).toHaveFocus();

    await userEvent.click(screen.getByRole("button", { name: `Dismiss ${PROBLEM.path}` }));
    expect(await screen.findByText(`Dismissed ${PROBLEM.path}`)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: `Dismiss ${second.path}` })).toHaveFocus());
  });

  it("remembers whether the Dismissed list was open and can show everything again", async () => {
    window.localStorage.removeItem("pfa:import:dismissed-open");
    const { fn, calls } = makeFetch({ status: BASE_STATUS, recent: [], problems: [], dismissed: [PROBLEM, { ...PROBLEM, id: 6 }] });
    const view = await renderPanel(fn);
    const details = await screen.findByTestId("folder-imports-dismissed");
    expect(details).not.toHaveAttribute("open");
    await userEvent.click(within(details).getByText(/Dismissed \(2\)/));
    await waitFor(() => expect(window.localStorage.getItem("pfa:import:dismissed-open")).toBe("1"));
    view.unmount();
    await renderPanel(fn);
    expect(await screen.findByTestId("folder-imports-dismissed")).toHaveAttribute("open");
    await userEvent.click(screen.getByRole("button", { name: "Show all again" }));
    await waitFor(() => expect(calls.filter((c) => c.url.includes("/undismiss"))).toHaveLength(2));
  });
});
