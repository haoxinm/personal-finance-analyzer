import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ImportItem, Issuer, LayoutSpec } from "./types";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function describeRequest(input: RequestInfo | URL): { url: string; method: string } {
  if (input instanceof Request) return { url: input.url, method: input.method };
  return { url: input.toString(), method: "GET" };
}

const ISSUERS: Issuer[] = [
  { id: 3, name: "Chase", slug: "chase", match_terms: ["chase"], default_spec_id: null },
  { id: 4, name: "Amex", slug: "amex", match_terms: ["amex"], default_spec_id: null },
];
const SPECS: LayoutSpec[] = [
  { id: 10, name: "Chase custom", version: 1, issuer_id: 3, approved: true, source: "user" },
];

async function renderPicker(
  itemOverrides: Partial<ImportItem> = {},
  createResponse?: Response,
) {
  vi.resetModules();
  const fetchMock = vi.fn<typeof fetch>((input) => {
    const { url, method } = describeRequest(input);
    if (url.includes("/api/issuers") && method === "POST") {
      if (createResponse) return Promise.resolve(createResponse.clone());
      return Promise.resolve(
        jsonResponse({ id: 99, name: "New Bank", slug: "new-bank", match_terms: ["new bank"], default_spec_id: null }, 201),
      );
    }
    if (url.includes("/api/issuers")) return Promise.resolve(jsonResponse(ISSUERS));
    if (url.includes("/api/layout-specs")) return Promise.resolve(jsonResponse(SPECS));
    return Promise.resolve(jsonResponse({ detail: "unhandled" }, 404));
  });
  vi.stubGlobal("fetch", fetchMock);

  const { IssuerExtractorPicker } = await import("./issuer-extractor-picker");
  const onIssuerChange = vi.fn();
  const onExtractorChange = vi.fn();
  const onIssuerCreated = vi.fn();
  const item: ImportItem = {
    clientId: "c1",
    fileName: "statement.pdf",
    userId: 1,
    status: "awaiting_extractor" as const,
    issuerId: 3,
    extractorChoice: { kind: "parser" as const, parserId: "layout_a_credit" },
    remember: true,
    proposal: {
      issuer_id: 3,
      parser_id: "layout_a_credit",
      layout_spec_id: null,
      confidence: 0.9,
      auto_confirmed: false,
      remembered: false,
    },
    ...itemOverrides,
  };

  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <IssuerExtractorPicker item={item} onIssuerChange={onIssuerChange} onExtractorChange={onExtractorChange}
        onIssuerCreated={onIssuerCreated}
      />
    </QueryClientProvider>,
  );
  return { onIssuerChange, onExtractorChange, onIssuerCreated, fetchMock };
}

describe("IssuerExtractorPicker", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists issuers and preselects the proposed one", async () => {
    await renderPicker();
    await waitFor(() => expect(screen.getByRole("option", { name: "Chase" })).toBeInTheDocument());
    const select: HTMLSelectElement = screen.getByLabelText("Issuer");
    expect(select.value).toBe("3");
  });

  it("offers the issuer's approved layout spec alongside the built-in parser guess", async () => {
    await renderPicker();
    await waitFor(() => expect(screen.getByText(/Chase custom v1/)).toBeInTheDocument());
    expect(screen.getByText(/Auto-detected: layout_a_credit/)).toBeInTheDocument();
  });

  it("calls onIssuerChange with the new issuer's own spec when the user switches issuer", async () => {
    const { onIssuerChange } = await renderPicker();
    await waitFor(() => expect(screen.getByRole("option", { name: "Amex" })).toBeInTheDocument());

    await userEvent.selectOptions(screen.getByLabelText("Issuer"), "4");

    expect(onIssuerChange).toHaveBeenCalledWith(4, { kind: "parser", parserId: "layout_a_credit" });
  });

  it("creates a new issuer inline and selects it", async () => {
    const { onIssuerChange } = await renderPicker();
    await userEvent.click(screen.getByRole("button", { name: "New" }));

    await userEvent.type(screen.getByPlaceholderText(/Issuer name/), "New Bank");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(onIssuerChange).toHaveBeenCalledWith(99, { kind: "parser", parserId: "layout_a_credit" }),
    );
  });

  it("tells the screen when an issuer was created, so pending files can be matched to it", async () => {
    const { onIssuerCreated } = await renderPicker();
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.type(screen.getByPlaceholderText(/Issuer name/), "New Bank");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(onIssuerCreated).toHaveBeenCalledWith(99));
  });

  it("shows the server's 409 inline and selects the existing issuer on request", async () => {
    const { onIssuerChange } = await renderPicker(
      {},
      jsonResponse(
        { detail: { message: "An issuer named 'Amex' already exists.", existing_issuer_id: 4 } },
        409,
      ),
    );
    await waitFor(() => expect(screen.getByRole("option", { name: "Amex" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.type(screen.getByPlaceholderText(/Issuer name/), "American Express");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect((await screen.findByRole("alert")).textContent).toBe("An issuer named 'Amex' already exists.");
    expect(onIssuerChange).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Use existing 'Amex'" }));

    expect(onIssuerChange).toHaveBeenCalledWith(4, { kind: "parser", parserId: "layout_a_credit" });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("warns before submitting when the typed name matches an existing issuer", async () => {
    const { onIssuerChange } = await renderPicker();
    await waitFor(() => expect(screen.getByRole("option", { name: "Amex" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "New" }));
    await userEvent.type(screen.getByPlaceholderText(/Issuer name/), "chase");

    expect(screen.getByText("An issuer named 'Chase' already exists.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use existing 'Chase'" }));
    expect(onIssuerChange).toHaveBeenCalledWith(3, expect.anything());
  });
});
