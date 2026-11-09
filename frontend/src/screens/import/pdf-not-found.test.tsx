import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/api/client";

import { isPdfNotFound } from "./api";
import type { ImportItem } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const ITEM = {
  clientId: "c1",
  fileName: "jan.pdf",
  userId: 1,
  status: "awaiting_extractor",
  statementId: 7,
  statement: { id: 7, page_count: 3, user_id: 1 },
  issuerId: null,
  extractorChoice: { kind: "parser", parserId: "llm" },
  remember: false,
  selectedPages: null,
} as unknown as ImportItem;

/** `openapi-fetch` binds `fetch` when the client module loads, so stub it first and import after. */
async function renderSelector(fetchMock: typeof fetch) {
  vi.resetModules();
  vi.stubGlobal("fetch", fetchMock);
  const { PageSelector } = await import("./page-selector");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <PageSelector item={ITEM} dispatch={vi.fn()} />
    </QueryClientProvider>,
  );
}

describe("PDF not found", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("recognizes the server's pdf_not_found answer", () => {
    expect(isPdfNotFound(new ApiError("x", { code: "pdf_not_found", message: "x" }))).toBe(true);
    expect(isPdfNotFound(new ApiError("x", { code: "pdf_not_kept", message: "x" }))).toBe(false);
    expect(isPdfNotFound(new ApiError("x", "plain string"))).toBe(false);
    expect(isPdfNotFound(new Error("x"))).toBe(false);
    expect(isPdfNotFound(undefined)).toBe(false);
  });

  it("shows a clear state instead of an error when the kept PDF was moved out or deleted", async () => {
    await renderSelector(
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          jsonResponse({ detail: { code: "pdf_not_found", message: "PDF not found: the copy is gone." } }, 404),
        ),
      ),
    );
    const note = await screen.findByTestId("pdf-not-found");
    expect(note).toHaveTextContent(/PDF not found.*no longer in your documents folder/s);
    expect(note).toHaveAttribute("role", "alert");
  });

  it("shows the page picker as usual when the PDF is there", async () => {
    await renderSelector(
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          jsonResponse([1, 2, 3].map((n) => ({ page_number: n, char_count: 10 * n, snippet: `s${n}`, selected: true }))),
        ),
      ),
    );
    await screen.findByLabelText("Page 2");
    expect(screen.queryByTestId("pdf-not-found")).not.toBeInTheDocument();
  });
});
