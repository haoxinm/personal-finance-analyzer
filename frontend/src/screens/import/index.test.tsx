import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  close() {
    // no-op: nothing to assert on in these tests.
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** `openapi-fetch` always calls `fetch(request: Request, ...)`, never a bare URL string, so a
 * mock keyed on request shape reads `.url`/`.method` off the `Request` object itself. */
function describeRequest(input: RequestInfo | URL): { url: string; method: string } {
  if (input instanceof Request) return { url: input.url, method: input.method };
  return { url: input.toString(), method: "GET" };
}

const USER = { id: 1, name: "Alex", is_default: true };
const ISSUER = { id: 3, name: "Chase", slug: "chase", match_terms: ["chase"], default_spec_id: null };
const STATEMENT_AWAITING = {
  id: 7,
  user_id: 1,
  account_id: null,
  status: "awaiting_extractor" as const,
  period_start: null,
  period_end: null,
  txn_count: null,
  reconciliation_delta_minor: null,
  detect_score: 0.92,
  shape_warnings: null,
  error_detail: null,
  created_at: "2024-02-01T00:00:00Z",
};
const STATEMENT_PARSED = {
  ...STATEMENT_AWAITING,
  status: "parsed" as const,
  account_id: 5,
  period_start: "2024-01-01",
  period_end: "2024-01-31",
  txn_count: 40,
  reconciliation_delta_minor: 0,
};

/**
 * `apiClient` (`@/api/client`) captures `globalThis.fetch` once, at module load, exactly like
 * `client.test.ts` documents — so each test resets the module registry and stubs `fetch` *before*
 * dynamically importing the screen, rather than importing it (and its transitive `apiClient`)
 * once at this file's top, which would freeze in whichever `fetch` happened to be global first.
 */
async function renderScreen(fetchMock: typeof fetch, entry = "/") {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { default: ImportScreen } = await import("./index");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ImportScreen />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("screenMeta", () => {
  it("matches the P3 screen contract", async () => {
    vi.resetModules();
    const { screenMeta } = await import("./index");
    expect(screenMeta).toEqual({ path: "/spending/import", title: "Import" });
  });
});

describe("ImportScreen", () => {
  beforeEach(() => {
    MockEventSource.instances = [];
    vi.stubGlobal("EventSource", MockEventSource);
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the Imported statements panel above the folder imports", async () => {
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const { url } = describeRequest(input);
      if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
      if (url.includes("/api/settings")) return Promise.resolve(jsonResponse({ llm: { mode: "none" } }));
      return Promise.resolve(jsonResponse([]));
    });
    await renderScreen(fetchMock);
    const imported = await screen.findByRole("region", { name: "Imported statements" });
    expect(await screen.findByText("No statements imported yet.")).toBeInTheDocument();
    const folder = await screen.findByTestId("folder-imports-panel");
    expect(imported.compareDocumentPosition(folder) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it.each([
    ["#imported-statements", "imported-statements-title"],
    ["#folder-imports", "folder-imports-title"],
  ])("focuses the card title named by the %s hash once the card is there", async (hash, titleId) => {
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const { url } = describeRequest(input);
      if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
      if (url.includes("/api/settings")) return Promise.resolve(jsonResponse({ llm: { mode: "none" } }));
      return Promise.resolve(jsonResponse([]));
    });
    await renderScreen(fetchMock, `/spending/import${hash}`);
    await waitFor(() => expect(document.getElementById(titleId)).toHaveFocus());
  });

  it("leaves focus alone without a hash", async () => {
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const { url } = describeRequest(input);
      if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
      if (url.includes("/api/settings")) return Promise.resolve(jsonResponse({ llm: { mode: "none" } }));
      return Promise.resolve(jsonResponse([]));
    });
    await renderScreen(fetchMock);
    await screen.findByRole("region", { name: "Imported statements" });
    expect(document.getElementById("imported-statements-title")).not.toHaveFocus();
  });

  it("defaults the user selector to the default user and lets the user drop a file through to a confident, pre-selected proposal", async () => {
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
      if (url.includes("/api/issuers")) return Promise.resolve(jsonResponse([ISSUER]));
      if (url.includes("/api/layout-specs")) return Promise.resolve(jsonResponse([]));
      if (url.includes("/api/statements") && method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              statement: STATEMENT_AWAITING,
              proposal: {
                issuer_id: 3,
                parser_id: "layout_a_credit",
                layout_spec_id: null,
                confidence: 0.92,
                auto_confirmed: false,
              },
            },
            201,
          ),
        );
      }
      return Promise.resolve(jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404));
    });
    await renderScreen(fetchMock);

    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    const pdf = new File(["%PDF-1.4"], "chase.pdf", { type: "application/pdf" });
    const input = screen.getByLabelText("Choose statement PDFs");
    await userEvent.upload(input, pdf);

    await waitFor(() => expect(screen.getByText("chase.pdf")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("Ready to import")).toBeInTheDocument());
    expect(screen.getByText(/Detected confidence: 92%/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
  });

  it("takes a no_text_layer upload straight to that failure message, with no extractor step", async () => {
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
      if (url.includes("/api/issuers")) return Promise.resolve(jsonResponse([ISSUER]));
      if (url.includes("/api/layout-specs")) return Promise.resolve(jsonResponse([]));
      if (url.includes("/api/statements") && method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              statement: {
                ...STATEMENT_AWAITING,
                status: "no_text_layer",
                error_detail: "Can't read this PDF — it looks like a scan or an image.",
              },
              proposal: {
                issuer_id: null,
                parser_id: null,
                layout_spec_id: null,
                confidence: 0,
                auto_confirmed: false,
              },
            },
            201,
          ),
        );
      }
      return Promise.resolve(jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404));
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    const pdf = new File(["%PDF-1.4"], "scanned.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), pdf);

    await waitFor(() =>
      expect(
        screen.getByText("Can't read this PDF — it looks like a scan or an image."),
      ).toBeInTheDocument(),
    );
    expect(screen.getByText("Can't read this PDF")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
  });

  it("shows the running summary once a statement finishes parsing", async () => {
    let extractCalled = false;
    const fetchMock = vi.fn<typeof fetch>((input) => {
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
      if (url.includes("/api/issuers")) return Promise.resolve(jsonResponse([ISSUER]));
      if (url.includes("/api/layout-specs")) return Promise.resolve(jsonResponse([]));
      if (url.includes("/api/statements/7/extract") && method === "POST") {
        extractCalled = true;
        return Promise.resolve(jsonResponse({ job_id: "job-1" }, 202));
      }
      if (url.includes("/api/statements/7") && method === "GET") {
        return Promise.resolve(jsonResponse(STATEMENT_PARSED));
      }
      if (url.includes("/api/statements") && method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              statement: STATEMENT_AWAITING,
              proposal: {
                issuer_id: 3,
                parser_id: "layout_a_credit",
                layout_spec_id: null,
                confidence: 0.92,
                auto_confirmed: false,
              },
            },
            201,
          ),
        );
      }
      if (url.includes("/api/transactions")) {
        return Promise.resolve(jsonResponse({ items: [], total: 0 }));
      }
      return Promise.resolve(jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404));
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    const pdf = new File(["%PDF-1.4"], "chase.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), pdf);
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeEnabled());

    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(extractCalled).toBe(true);
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));

    const source = MockEventSource.instances[0];
    if (!source) throw new Error("expected an EventSource instance");
    source.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("1 statement imported · 40 new transactions"),
    );
  });

  it("carries every file of a multi-file drop through upload and auto-confirmed extraction, not just the last", async () => {
    // "Don't ask again" for issuer 3 makes a confident proposal import with no click, so all
    // three uploads and all three extracts are in flight on the screen's two shared mutations.
    window.localStorage.setItem("pfa:import:skip-confirm-issuer-ids", JSON.stringify([3]));
    let uploads = 0;
    const extractedStatementIds: number[] = [];
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return jsonResponse([USER]);
      if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
      if (url.includes("/api/layout-specs")) return jsonResponse([]);
      if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: "none" } });
      const extractMatch = /\/api\/statements\/(\d+)\/extract/.exec(url);
      if (extractMatch && method === "POST") {
        extractedStatementIds.push(Number(extractMatch[1]));
        return jsonResponse({ job_id: `job-${extractMatch[1]}` }, 202);
      }
      if (url.endsWith("/api/statements") && method === "POST") {
        const n = (uploads += 1);
        // Stagger the replies so the three requests overlap on the one shared mutation.
        await new Promise((resolve) => setTimeout(resolve, 40 - n * 10));
        return jsonResponse(
          {
            statement: { ...STATEMENT_AWAITING, id: 100 + n },
            proposal: {
              issuer_id: 3,
              parser_id: "layout_a_credit",
              layout_spec_id: null,
              confidence: 0.92,
              auto_confirmed: false,
            },
          },
          201,
        );
      }
      return jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404);
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    const files = ["a.pdf", "b.pdf", "c.pdf"].map(
      (name) => new File(["%PDF-1.4"], name, { type: "application/pdf" }),
    );
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), files);

    await waitFor(() => expect(screen.getAllByText("Importing…")).toHaveLength(3));
    expect(screen.queryByText("Uploading…")).not.toBeInTheDocument();
    expect(extractedStatementIds.sort()).toEqual([101, 102, 103]);
  });

  it("moves every file of a multi-file drop to its next state when each upload needs a click", async () => {
    let uploads = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return jsonResponse([USER]);
      if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
      if (url.includes("/api/layout-specs")) return jsonResponse([]);
      if (url.endsWith("/api/statements") && method === "POST") {
        const n = (uploads += 1);
        // The first upload is the slowest, so it settles after a later one started.
        await new Promise((resolve) => setTimeout(resolve, 40 - n * 10));
        return n === 2
          ? jsonResponse({ detail: "boom" }, 500)
          : jsonResponse(
              {
                statement: { ...STATEMENT_AWAITING, id: 100 + n },
                proposal: {
                  issuer_id: 3,
                  parser_id: "layout_a_credit",
                  layout_spec_id: null,
                  confidence: 0.92,
                  auto_confirmed: false,
                },
              },
              201,
            );
      }
      return jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404);
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    const files = ["a.pdf", "b.pdf", "c.pdf"].map(
      (name) => new File(["%PDF-1.4"], name, { type: "application/pdf" }),
    );
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), files);

    await waitFor(() => expect(screen.getAllByText("Ready to import")).toHaveLength(2));
    expect(screen.getByText("Upload failed")).toBeInTheDocument();
    expect(screen.queryByText("Uploading…")).not.toBeInTheDocument();
  });

  it("reports a re-dropped, already imported PDF as 'Already imported' without offering to import it again", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      await Promise.resolve();
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return jsonResponse([USER]);
      if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
      if (url.includes("/api/layout-specs")) return jsonResponse([]);
      if (url.endsWith("/api/statements") && method === "POST") {
        return jsonResponse(
          {
            statement: STATEMENT_PARSED,
            proposal: {
              issuer_id: 3,
              parser_id: "layout_a_credit",
              layout_spec_id: null,
              confidence: 0.92,
              auto_confirmed: false,
            },
            duplicate: true,
          },
          201,
        );
      }
      return jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404);
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    await userEvent.upload(
      screen.getByLabelText("Choose statement PDFs"),
      new File(["%PDF-1.4"], "chase.pdf", { type: "application/pdf" }),
    );

    expect(await screen.findByText(/Already imported — 40 transactions on file/)).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("1 already imported");
    expect(screen.queryByRole("button", { name: "Import" })).not.toBeInTheDocument();
  });

  it("splits an import's new rows from the ones an overlapping statement already had", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      await Promise.resolve();
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return jsonResponse([USER]);
      if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
      if (url.includes("/api/layout-specs")) return jsonResponse([]);
      if (url.includes("/api/statements/7/extract") && method === "POST") {
        return jsonResponse({ job_id: "job-1" }, 202);
      }
      if (url.includes("/api/jobs/job-1")) {
        return jsonResponse({
          id: "job-1",
          kind: "import",
          status: "done",
          progress: 1,
          inserted: 31,
          skipped_duplicates: 9,
          classify_job_id: null,
        });
      }
      if (url.includes("/api/statements/7") && method === "GET") {
        return jsonResponse({ ...STATEMENT_PARSED, txn_count: 31 });
      }
      if (url.endsWith("/api/statements") && method === "POST") {
        return jsonResponse(
          {
            statement: STATEMENT_AWAITING,
            proposal: {
              issuer_id: 3,
              parser_id: "layout_a_credit",
              layout_spec_id: null,
              confidence: 0.92,
              auto_confirmed: false,
            },
            duplicate: false,
          },
          201,
        );
      }
      if (url.includes("/api/transactions")) return jsonResponse({ items: [], total: 5 });
      return jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404);
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());
    await userEvent.upload(
      screen.getByLabelText("Choose statement PDFs"),
      new File(["%PDF-1.4"], "chase.pdf", { type: "application/pdf" }),
    );
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    MockEventSource.instances[0]?.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "1 statement imported · 31 new transactions · 9 already present · 5 need review",
      ),
    );
    // One next-step link on the card, one on the summary.
    const reviewLinks = screen.getAllByRole("link", { name: /Review 5/ });
    expect(reviewLinks).toHaveLength(2);
    expect(reviewLinks[0]).toHaveAttribute("href", "/spending/review");
  });

  it("shows a failed built-in parse as a warning, keeps the picker, preselects the LLM, and retries with parser_id llm", async () => {
    const extractBodies: unknown[] = [];
    let statementGets = 0;
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return jsonResponse([USER]);
      if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
      if (url.includes("/api/layout-specs")) return jsonResponse([]);
      if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: "remote", base_url: "https://x" } });
      if (url.includes("/api/statements/7/extract") && method === "POST") {
        const request = input as Request;
        extractBodies.push(await request.clone().json());
        return jsonResponse({ job_id: `job-${extractBodies.length}` }, 202);
      }
      if (url.includes("/api/statements/7/") ) return jsonResponse({ detail: "n/a" }, 404);
      if (url.includes("/api/statements/7") && method === "GET") {
        statementGets += 1;
        return jsonResponse(
          statementGets === 1
            ? {
                ...STATEMENT_AWAITING,
                status: "error",
                error_detail: "layout_a_credit: could not locate the column header row",
              }
            : STATEMENT_PARSED,
        );
      }
      if (url.includes("/api/statements") && method === "POST") {
        return jsonResponse(
          {
            statement: STATEMENT_AWAITING,
            proposal: {
              issuer_id: 3,
              parser_id: "layout_a_credit",
              layout_spec_id: null,
              confidence: 0.92,
              auto_confirmed: false,
            },
          },
          201,
        );
      }
      if (url.includes("/api/transactions")) return jsonResponse({ items: [], total: 0 });
      return jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404);
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());

    const pdf = new File(["%PDF-1.4"], "fees.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), pdf);
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(extractBodies[0]).toMatchObject({ parser_id: "layout_a_credit" });

    await waitFor(() => expect(MockEventSource.instances).toHaveLength(1));
    MockEventSource.instances[0]?.onmessage?.({ data: JSON.stringify({ status: "error", progress: 1 }) });

    const warning = await screen.findByTestId("parser-warning");
    expect(warning).toHaveTextContent(
      "The built-in parser couldn't read this statement: layout_a_credit: could not locate the column header row. Choose the LLM parser (or another extractor) and import again.",
    );
    expect(screen.getByRole("link", { name: "Map this layout" })).toBeInTheDocument();
    // Picker and Import stay available, with the LLM preselected (but nothing sent yet).
    await waitFor(() => expect(screen.getByLabelText(/Extractor/)).toHaveValue("parser:llm"));
    expect(extractBodies).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();

    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(extractBodies).toHaveLength(2);
    expect(extractBodies[1]).toMatchObject({ parser_id: "llm", layout_spec_id: null, issuer_id: 3 });
  });

  it("shows a re-uploaded, previously failed statement as an informational warning with the picker open", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      await Promise.resolve();
      const { url, method } = describeRequest(input);
      if (url.includes("/api/users")) return jsonResponse([USER]);
      if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
      if (url.includes("/api/layout-specs")) return jsonResponse([]);
      if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: "remote", base_url: "https://x" } });
      if (url.includes("/api/statements") && method === "POST") {
        return jsonResponse(
          {
            statement: { ...STATEMENT_AWAITING, error_detail: "The LLM could not parse this statement: timed out" },
            proposal: { issuer_id: 3, parser_id: "layout_a_credit", layout_spec_id: null, confidence: 0.92, auto_confirmed: false },
          },
          201,
        );
      }
      return jsonResponse({ detail: "n/a" }, 404);
    });
    await renderScreen(fetchMock);
    await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());
    await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), new File(["%PDF-1.4"], "again.pdf", { type: "application/pdf" }));

    expect(await screen.findByTestId("parser-warning")).toHaveTextContent(
      "A previous attempt to import this statement failed: The LLM could not parse this statement: timed out",
    );
    expect(screen.getByRole("button", { name: "Import" })).toBeEnabled();
    // Re-uploading never auto-runs the extractor that failed.
    expect(fetchMock.mock.calls.some(([i]) => describeRequest(i as Request).url.includes("/extract"))).toBe(false);
  });

  describe("remembering an extractor in a batch", () => {
    const REMEMBERED = {
      issuer_id: 3,
      parser_id: null as string | null,
      layout_spec_id: 9 as number | null,
      confidence: 0.9,
      auto_confirmed: false,
      remembered: true,
    };

    /** Three files of one bank: the first is a clear match, the other two matched nothing. */
    async function dropBatch(opts: { proposeWith: typeof REMEMBERED; skipConfirm?: boolean; proposeStatus?: number }) {
      if (opts.skipConfirm) window.localStorage.setItem("pfa:import:skip-confirm-issuer-ids", JSON.stringify([3]));
      const extracted: number[] = [];
      const proposed: number[] = [];
      const fetchMock = vi.fn<typeof fetch>((input) => Promise.resolve(((): Response => {
        const { url, method } = describeRequest(input);
        if (url.includes("/api/users")) return jsonResponse([USER]);
        if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
        if (url.includes("/api/layout-specs")) {
          return jsonResponse([{ id: 9, name: "Chase layout", version: 2, issuer_id: 3, approved: true, source: "user" }]);
        }
        if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: "cloud" } });
        const extract = /\/api\/statements\/(\d+)\/extract/.exec(url);
        if (extract && method === "POST") {
          extracted.push(Number(extract[1]));
          return jsonResponse({ job_id: `job-${extract[1]}` }, 202);
        }
        const propose = /\/api\/statements\/(\d+)\/propose/.exec(url);
        if (propose && method === "POST") {
          proposed.push(Number(propose[1]));
          if (opts.proposeStatus && opts.proposeStatus >= 400) return jsonResponse({ detail: "already parsed" }, opts.proposeStatus);
          return jsonResponse(
            { statement: { ...STATEMENT_AWAITING, id: Number(propose[1]) }, proposal: opts.proposeWith, duplicate: false },
            200,
          );
        }
        if (url.endsWith("/api/statements") && method === "POST") {
          return jsonResponse(
            {
              statement: { ...STATEMENT_AWAITING, id: 100 + (uploadCount += 1) },
              proposal:
                uploadCount === 1
                  ? { issuer_id: 3, parser_id: "layout_a_credit", layout_spec_id: null, confidence: 0.5, auto_confirmed: false, remembered: false }
                  : { issuer_id: null, parser_id: null, layout_spec_id: null, confidence: 0, auto_confirmed: false, remembered: false },
            },
            201,
          );
        }
        if (url.includes("/api/jobs/job-101")) {
          return jsonResponse({ id: "job-101", kind: "import", status: "done", progress: 1, inserted: 5, remembered: true });
        }
        if (/\/api\/statements\/101$/.test(url)) return jsonResponse({ ...STATEMENT_PARSED, id: 101 });
        if (url.includes("/api/transactions")) return jsonResponse({ items: [], total: 0, page: 1, page_size: 1 });
        if (url.includes("/api/statements")) return jsonResponse([]);
        return jsonResponse([]);
      })()));
      let uploadCount = 0;
      await renderScreen(fetchMock);
      await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());
      const files = ["a.pdf", "b.pdf", "c.pdf"].map((name) => new File(["%PDF-1.4"], name, { type: "application/pdf" }));
      await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), files);
      await waitFor(() => expect(screen.getAllByText("Ready to import")).toHaveLength(3));
      // Import the first one (remember is on by default).
      const cards = screen.getAllByRole("button", { name: "Import" });
      await userEvent.click(cards[0]!);
      await waitFor(() => expect(extracted).toContain(101));
      await waitFor(() => expect(MockEventSource.instances.some((i) => i.url.includes("job-101"))).toBe(true));
      act(() => {
        MockEventSource.instances.find((i) => i.url.includes("job-101"))!.onmessage?.({
          data: JSON.stringify({ status: "done", progress: 1 }),
        });
      });
      return { extracted, proposed };
    }

    it("re-proposes the other pending files and shows what they would now be offered", async () => {
      const { proposed, extracted } = await dropBatch({ proposeWith: REMEMBERED });
      await waitFor(() => expect(proposed.sort()).toEqual([102, 103]));
      await waitFor(() => expect(screen.getAllByText(/Detected confidence: 90%/)).toHaveLength(2));
      expect(screen.getAllByText(/confident match/).length).toBeGreaterThanOrEqual(2);
      // Not a skip-confirm issuer: nothing is imported without a click.
      expect(extracted).toEqual([101]);
    });

    it("imports the peers on its own where a fresh upload would (skip-confirm issuer, confident proposal)", async () => {
      const { extracted } = await dropBatch({ proposeWith: REMEMBERED, skipConfirm: true });
      await waitFor(() => expect([...extracted].sort()).toEqual([101, 102, 103]));
    });

    it("never confirms a file twice: a peer refresh leaves an upload whose import request is still in flight alone", async () => {
      window.localStorage.setItem("pfa:import:skip-confirm-issuer-ids", JSON.stringify([3]));
      const extracted: number[] = [];
      let release: () => void = () => undefined;
      const held = new Promise<void>((resolve) => {
        release = resolve;
      });
      const confident = { issuer_id: 3, parser_id: "layout_a_credit", layout_spec_id: null, confidence: 0.9, auto_confirmed: false, remembered: false };
      const unsure = { ...confident, confidence: 0.5 };
      let uploads = 0;
      const fetchMock = vi.fn<typeof fetch>(async (input) => {
        const { url, method } = describeRequest(input);
        if (url.includes("/api/users")) return jsonResponse([USER]);
        if (url.includes("/api/issuers")) return jsonResponse([ISSUER]);
        if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: "none" } });
        const extract = /\/api\/statements\/(\d+)\/extract/.exec(url);
        if (extract && method === "POST") {
          extracted.push(Number(extract[1]));
          if (extract[1] === "202") await held;
          return jsonResponse({ job_id: `job-${extract[1]}` }, 202);
        }
        const propose = /\/api\/statements\/(\d+)\/propose/.exec(url);
        if (propose) {
          return jsonResponse({ statement: { ...STATEMENT_AWAITING, id: Number(propose[1]) }, proposal: { ...confident, remembered: true }, duplicate: false });
        }
        if (url.endsWith("/api/statements") && method === "POST") {
          uploads += 1;
          return jsonResponse({ statement: { ...STATEMENT_AWAITING, id: 200 + uploads }, proposal: uploads === 1 ? unsure : confident }, 201);
        }
        if (url.includes("/api/jobs/job-201")) {
          return jsonResponse({ id: "job-201", kind: "import", status: "done", progress: 1, inserted: 5, remembered: true });
        }
        if (/\/api\/statements\/201$/.test(url)) return jsonResponse({ ...STATEMENT_PARSED, id: 201 });
        return jsonResponse([]);
      });
      await renderScreen(fetchMock);
      await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());
      await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), [new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" })]);
      await userEvent.click(await screen.findByRole("button", { name: "Import" }));
      await waitFor(() => expect(MockEventSource.instances.some((i) => i.url.includes("job-201"))).toBe(true));
      // b is auto-imported at upload; its request is held in flight.
      await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), [new File(["%PDF-1.4"], "b.pdf", { type: "application/pdf" })]);
      await waitFor(() => expect(extracted).toEqual([201, 202]));
      // a finishes and remembers its extractor: the peer refresh must not confirm b again.
      act(() => {
        MockEventSource.instances.find((i) => i.url.includes("job-201"))!.onmessage?.({ data: JSON.stringify({ status: "done", progress: 1 }) });
      });
      const sawUrl = (part: string) => fetchMock.mock.calls.some(([i]) => describeRequest(i).url.includes(part));
      // The refresh after a's job has run (its needs-review count was asked for)...
      await waitFor(() => expect(sawUrl("/api/transactions?statement_id=201")).toBe(true));
      // ...and b, whose request is still in flight, was neither re-proposed nor re-confirmed.
      expect(sawUrl("/propose")).toBe(false);
      await new Promise((resolve) => setTimeout(resolve, 100));
      release();
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(extracted).toEqual([201, 202]);
    });

    it("explains what the remember box keeps and what it would replace", async () => {
      const fetchMock = vi.fn<typeof fetch>((input) => Promise.resolve(((): Response => {
        const { url, method } = describeRequest(input);
        if (url.includes("/api/users")) return jsonResponse([USER]);
        if (url.includes("/api/issuers")) return jsonResponse([{ ...ISSUER, default_parser_id: "llm" }]);
        if (url.includes("/api/settings")) return jsonResponse({ llm: { mode: "none" } });
        if (url.endsWith("/api/statements") && method === "POST") {
          return jsonResponse({
            statement: { ...STATEMENT_AWAITING, id: 301 },
            proposal: { issuer_id: 3, parser_id: "layout_a_credit", layout_spec_id: null, confidence: 0.5, auto_confirmed: false, remembered: false },
          }, 201);
        }
        return jsonResponse([]);
      })()));
      await renderScreen(fetchMock);
      await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());
      await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), [new File(["%PDF-1.4"], "a.pdf", { type: "application/pdf" })]);
      expect(await screen.findByLabelText("Use layout a credit for future Chase statements")).toBeChecked();
      expect(screen.getByTestId("remember-note")).toHaveTextContent(
        "Chase currently remembers the LLM. Importing will replace it with layout a credit",
      );
      await userEvent.click(screen.getByLabelText("Use layout a credit for future Chase statements"));
      expect(screen.getByTestId("remember-note")).toHaveTextContent("it will stay that way");
    });

    it("quietly leaves a peer alone when propose answers 409 (already parsed)", async () => {
      const { extracted } = await dropBatch({ proposeWith: REMEMBERED, proposeStatus: 409 });
      await new Promise((resolve) => setTimeout(resolve, 60));
      expect(extracted).toEqual([101]);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });

    it("never imports a peer with the LLM on its own", async () => {
      const { proposed, extracted } = await dropBatch({
        proposeWith: { ...REMEMBERED, parser_id: "llm", layout_spec_id: null },
        skipConfirm: true,
      });
      await waitFor(() => expect(proposed.sort()).toEqual([102, 103]));
      await new Promise((resolve) => setTimeout(resolve, 60));
      expect(extracted).toEqual([101]);
    });
  });
});
