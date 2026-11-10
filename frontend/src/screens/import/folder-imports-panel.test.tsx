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
}

function makeFetch(fixture: Fixture) {
  const calls: { url: string; method: string }[] = [];
  const fn = vi.fn<typeof fetch>((input) => {
    const { url, method } = describeRequest(input);
    calls.push({ url, method });
    if (url.includes("/api/watch/status")) return Promise.resolve(jsonResponse(fixture.status));
    if (url.includes("/api/watch/files") && url.includes("retry") && method === "POST") {
      fixture.problems = [];
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (url.includes("/api/watch/files")) {
      const items = url.includes("view=problems") ? fixture.problems : fixture.recent;
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

async function renderPanel(fetchMock: typeof fetch) {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { FolderImportsPanel } = await import("./folder-imports-panel");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <FolderImportsPanel />
      </MemoryRouter>
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
      "/spending/transactions?statement_id=7",
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
});
