import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function statement(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: 42,
    user_id: 1,
    account_id: 7,
    status: "awaiting_extractor",
    period_start: "2024-01-01",
    period_end: "2024-01-31",
    txn_count: null,
    reconciliation_delta_minor: 0,
    detect_score: 0.3,
    shape_warnings: null,
    error_detail: null,
    created_at: "2024-02-01T00:00:00Z",
    ...overrides,
  };
}

// `apiClient` (`@/api/client`) captures `globalThis.fetch` as a default parameter value at
// `createClient()` time, not at request time — so the mock must be in place *before* that
// module (transitively imported by the screen) is first evaluated. `vi.resetModules()` plus a
// dynamic import after stubbing forces a fresh evaluation against the already-stubbed fetch,
// exactly like `src/api/client.test.ts` does for the client itself.
async function renderScreen(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // `ToastContextProvider` is also imported dynamically, from the same fresh module graph as
  // `LayoutMapperScreen` (which uses `useToast` deep in `SavePanel`) — a statically-imported copy
  // would be a *different* module instance (`vi.resetModules()` cleared the cache), with a
  // different `React.createContext` identity, and `useContext` would see no provider at all.
  const [{ default: LayoutMapperScreen }, { ToastContextProvider }] = await Promise.all([
    import("./index"),
    import("@/components/ui/toast-provider"),
  ]);
  return render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={queryClient}>
        <ToastContextProvider>
          <LayoutMapperScreen />
        </ToastContextProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("LayoutMapperScreen", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((input: Request | URL | string) => {
        const url = input instanceof Request ? input.url : input.toString();
        if (url.includes("/statements/42")) return Promise.resolve(jsonResponse(statement()));
        if (url.includes("/statements")) return Promise.resolve(jsonResponse([statement()]));
        if (url.includes("/issuers")) {
          return Promise.resolve(
            jsonResponse([{ id: 1, name: "Example Bank", slug: "example-bank", match_terms: [] }]),
          );
        }
        if (url.includes("/layout-specs")) return Promise.resolve(jsonResponse([]));
        return Promise.resolve(jsonResponse({ detail: "not found" }, 404));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows a picker of statements when no statement_id is given", async () => {
    await renderScreen("/spending/layout-mapper");
    const picker = await screen.findByTestId("statement-picker");
    expect(picker).toHaveTextContent("Statement #42");
    expect(picker).toHaveTextContent("Awaiting extractor");
  });

  it("loads the statement and renders the mapper once given a statement_id", async () => {
    await renderScreen("/spending/layout-mapper?statement_id=42");

    await waitFor(() => expect(screen.getByText(/Statement #42/)).toBeInTheDocument());
    expect(screen.getByText("Build a layout spec")).toBeInTheDocument();
    expect(screen.getByText("Map columns")).toBeInTheDocument();
    expect(screen.getByText("Paste a layout spec")).toBeInTheDocument();
    expect(screen.getByText("Save as a local layout spec")).toBeInTheDocument();
    expect(screen.getByText("Test this spec on the statement")).toBeInTheDocument();
  });

  it("switches to the paste-a-spec tab", async () => {
    await renderScreen("/spending/layout-mapper?statement_id=42");
    await waitFor(() => expect(screen.getByText(/Statement #42/)).toBeInTheDocument());

    screen.getByText("Paste a layout spec").click();
    expect(await screen.findByPlaceholderText(/id: user_acme_credit/)).toBeInTheDocument();
  });
});
