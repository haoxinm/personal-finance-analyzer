import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { formatPageRanges, parsePageRanges } from "./page-ranges";
import { setSkipConfirmForIssuer } from "./storage";
import { shouldAutoConfirm } from "./use-import-queue";
import type { ImportItem } from "./types";

class MockEventSource {
  close() {
    // no-op
  }
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
function describeRequest(input: RequestInfo | URL): { url: string; method: string; request?: Request } {
  if (input instanceof Request) return { url: input.url, method: input.method, request: input };
  return { url: input.toString(), method: "GET" };
}

const USER = { id: 1, name: "Alex", is_default: true };
const ISSUER = { id: 3, name: "Chase", slug: "chase", match_terms: ["chase"], default_spec_id: null };
const STATEMENT = {
  id: 7,
  user_id: 1,
  account_id: null,
  status: "awaiting_extractor" as const,
  period_start: null,
  period_end: null,
  txn_count: null,
  reconciliation_delta_minor: null,
  detect_score: 0.5,
  shape_warnings: null,
  error_detail: null,
  created_at: "2024-02-01T00:00:00Z",
  page_count: 4,
  selected_pages: null,
};
const PAGES = [1, 2, 3, 4].map((n) => ({
  page_number: n,
  char_count: n * 100,
  snippet: `snippet ${n}`,
  selected: true,
}));

function setup(opts: { llmMode: "none" | "local" | "remote"; parserId: string; confidence: number }) {
  const extractBodies: unknown[] = [];
  const previewUrls: string[] = [];
  const fetchMock = vi.fn<typeof fetch>((input) => {
    const { url, method, request } = describeRequest(input);
    if (url.includes("/api/users")) return Promise.resolve(jsonResponse([USER]));
    if (url.includes("/api/issuers")) return Promise.resolve(jsonResponse([ISSUER]));
    if (url.includes("/api/layout-specs")) return Promise.resolve(jsonResponse([]));
    if (url.includes("/api/settings")) {
      return Promise.resolve(
        jsonResponse({
          llm: { mode: opts.llmMode, base_url: "https://api.example.com/v1" },
          ingest: { default_extractor: "auto" },
        }),
      );
    }
    if (url.includes("/llm-preview")) {
      previewUrls.push(url);
      return Promise.resolve(
        jsonResponse({
          mode: opts.llmMode,
          resolved_url: "https://api.example.com/v1",
          model: "m",
          pages: [1, 3],
          requests: ["REDACTED TEXT ONE", "REDACTED TEXT TWO"],
          total_chars: 1234,
          redactions: 3,
        }),
      );
    }
    if (url.includes("/statements/7/pages")) return Promise.resolve(jsonResponse(PAGES));
    if (url.includes("/statements/7/extract") && method === "POST") {
      return (request as Request).json().then((body: unknown) => {
        extractBodies.push(body);
        return jsonResponse({ job_id: "job-1" }, 202);
      });
    }
    if (url.includes("/api/statements") && method === "POST") {
      return Promise.resolve(
        jsonResponse(
          {
            statement: STATEMENT,
            proposal: {
              issuer_id: 3,
              parser_id: opts.parserId,
              layout_spec_id: null,
              confidence: opts.confidence,
              auto_confirmed: false,
            },
          },
          201,
        ),
      );
    }
    return Promise.resolve(jsonResponse({ detail: `unhandled request: ${method} ${url}` }, 404));
  });
  return { fetchMock, extractBodies, previewUrls };
}

async function renderAndUpload(fetchMock: typeof fetch) {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { default: ImportScreen } = await import("./index");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ImportScreen />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(screen.getByTestId("import-target")).toBeInTheDocument());
  const pdf = new File(["%PDF-1.4"], "chase.pdf", { type: "application/pdf" });
  await userEvent.upload(screen.getByLabelText("Choose statement PDFs"), pdf);
  await waitFor(() => expect(screen.getByText("Ready to import")).toBeInTheDocument());
}

describe("page ranges", () => {
  it("parses and formats", () => {
    expect(parsePageRanges("1-3,5", 5)).toEqual([1, 2, 3, 5]);
    expect(parsePageRanges("6", 5)).toBeNull();
    expect(parsePageRanges("a", 5)).toBeNull();
    expect(formatPageRanges([5, 1, 2, 3])).toBe("1-3,5");
  });
});

describe("page selection", () => {
  beforeEach(() => {
    vi.stubGlobal("EventSource", MockEventSource);
    window.localStorage.clear();
    document.head.insertAdjacentHTML("beforeend", '<meta name="pfa-token" content="tok123">');
  });
  afterEach(() => {
    document.querySelector('meta[name="pfa-token"]')?.remove();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("toggles pages, keeps the range input in sync, and sends pages in the extract request", async () => {
    const { fetchMock, extractBodies } = setup({ llmMode: "none", parserId: "layout_a_credit", confidence: 0.5 });
    await renderAndUpload(fetchMock);

    const range: HTMLInputElement = screen.getByLabelText("Page ranges");
    expect(screen.getByLabelText("Page 1")).toBeChecked();

    await userEvent.click(screen.getByLabelText("Page 2"));
    expect(screen.getByLabelText("Page 2")).not.toBeChecked();
    expect(range.value).toBe("1,3-4");

    await userEvent.clear(range);
    await userEvent.type(range, "1-2");
    expect(screen.getByLabelText("Page 1")).toBeChecked();
    expect(screen.getByLabelText("Page 2")).toBeChecked();
    expect(screen.getByLabelText("Page 3")).not.toBeChecked();

    await userEvent.click(screen.getByRole("button", { name: "None" }));
    expect(screen.getByRole("button", { name: "Import" })).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "All" }));
    await userEvent.click(screen.getByLabelText("Page 4"));
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(extractBodies).toHaveLength(1));
    expect(extractBodies[0]).toMatchObject({ pages: [1, 2, 3], parser_id: "layout_a_credit" });
  });

  it("sends pages: null when all pages are selected", async () => {
    const { fetchMock, extractBodies } = setup({ llmMode: "none", parserId: "layout_a_credit", confidence: 0.5 });
    await renderAndUpload(fetchMock);
    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(extractBodies).toHaveLength(1));
    expect(extractBodies[0]).toMatchObject({ pages: null });
  });

  it("renders thumbnails with width and token", async () => {
    const { fetchMock } = setup({ llmMode: "none", parserId: "layout_a_credit", confidence: 0.5 });
    await renderAndUpload(fetchMock);
    const img = screen.getByAltText("Page 2 preview");
    expect(img.getAttribute("src")).toContain("/api/statements/7/pages/2/image");
    expect(img.getAttribute("src")).toContain("width=160");
    expect(img.getAttribute("src")).toContain("token=tok123");
    expect(img).toHaveAttribute("loading", "lazy");
  });

  it("disables the LLM option when no LLM is configured and links to settings", async () => {
    const { fetchMock } = setup({ llmMode: "none", parserId: "layout_a_credit", confidence: 0.5 });
    await renderAndUpload(fetchMock);
    const option = await screen.findByRole("option", { name: "LLM (reads the selected pages)" });
    expect(option).toBeDisabled();
    expect(await screen.findByRole("link", { name: "Set up an LLM in Settings" })).toHaveAttribute("href", "/settings");
  });

  it("uses the LLM: sends parser_id llm, warns about remote, and previews requests", async () => {
    const { fetchMock, extractBodies, previewUrls } = setup({ llmMode: "remote", parserId: "layout_a_credit", confidence: 0.5 });
    await renderAndUpload(fetchMock);
    const option = await screen.findByRole("option", { name: "LLM (reads the selected pages)" });
    await waitFor(() => expect(option).toBeEnabled());
    await userEvent.selectOptions(screen.getByLabelText("Extractor"), "parser:llm");

    await userEvent.click(screen.getByLabelText("Page 2"));
    expect(await screen.findByText(/sent to api\.example\.com/)).toBeInTheDocument();

    // Collapsed by default: nothing is fetched until the user asks.
    const toggle = screen.getByRole("button", { name: "Show what would be sent" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(previewUrls).toHaveLength(0);
    expect(screen.queryByTestId("llm-request")).not.toBeInTheDocument();
    await userEvent.click(toggle);
    const blocks = await screen.findAllByTestId("llm-request");
    expect(blocks.map((b) => b.textContent)).toEqual(["REDACTED TEXT ONE", "REDACTED TEXT TWO"]);
    expect(screen.getByText(/1234 characters total/)).toBeInTheDocument();
    expect(screen.getByTestId("llm-redactions")).toHaveTextContent("3 personal-info matches removed");
    expect(previewUrls[0]).toContain("pages=1%2C3%2C4");

    await userEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(extractBodies).toHaveLength(1));
    expect(extractBodies[0]).toMatchObject({ parser_id: "llm", pages: [1, 3, 4] });
  });

  it("labels an llm proposal as the LLM, not an auto-detected parser", async () => {
    const { fetchMock } = setup({ llmMode: "local", parserId: "llm", confidence: 0 });
    await renderAndUpload(fetchMock);
    expect(screen.queryByText(/Auto-detected: llm/)).not.toBeInTheDocument();
    const select: HTMLSelectElement = screen.getByLabelText("Extractor");
    expect(select.value).toBe("parser:llm");
    expect(await screen.findByText(/only to your local model/)).toBeInTheDocument();
    const scope = within(select);
    expect(scope.getByRole("option", { name: "LLM (reads the selected pages)" })).toBeInTheDocument();
  });
});

describe("shouldAutoConfirm", () => {
  it("never auto-confirms the LLM extractor", () => {
    setSkipConfirmForIssuer(3, true);
    const item: ImportItem = {
      clientId: "c",
      fileName: "a.pdf",
      userId: 1,
      status: "awaiting_extractor",
      issuerId: 3,
      extractorChoice: { kind: "parser", parserId: "llm" },
      remember: true,
      proposal: { issuer_id: 3, parser_id: "llm", layout_spec_id: null, confidence: 1, auto_confirmed: false },
    };
    expect(shouldAutoConfirm(item)).toBe(false);
    expect(shouldAutoConfirm({ ...item, extractorChoice: { kind: "parser", parserId: "layout_a_credit" } })).toBe(true);
  });
});
