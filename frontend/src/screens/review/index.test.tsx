import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiFetchMock, makeCategory, makeTransaction } from "./test-support";
import type { Category, Transaction } from "./types";

/**
 * Same module-freshness concern as the hook tests: `apiClient` is a module singleton built from
 * whatever `fetch` is global at import time, so the screen itself is imported fresh, after the
 * fetch stub is in place.
 */
async function renderScreen(
  state: {
    transactions: Transaction[];
    categories: Category[];
    users?: Array<{ id: number; name: string; is_default: boolean }>;
  },
  initialEntry = "/spending/review",
) {
  vi.resetModules();
  const fetchMock = vi.fn(createApiFetchMock(state));
  vi.stubGlobal("fetch", fetchMock);

  const { default: ReviewScreen } = await import("./index");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const { WithUserSwitcher } = await import("@/test/with-user-switcher");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToastContextProvider>
          <WithUserSwitcher>
            <ReviewScreen />
          </WithUserSwitcher>
        </ToastContextProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  return { fetchMock };
}

const CATEGORIES: Category[] = [
  makeCategory({
    key: "dining",
    name: "Dining",
    subcategories: [
      { id: 1, key: "dining", name: "Dining (general)", pending: false, merged_into: null },
      { id: 2, key: "restaurants", name: "Restaurants", pending: false, merged_into: null },
    ],
  }),
  makeCategory({
    key: "groceries",
    name: "Groceries",
    subcategories: [{ id: 3, key: "groceries", name: "Groceries", pending: false, merged_into: null }],
  }),
];

function lastPatchBody(fetchMock: ReturnType<typeof vi.fn>): Promise<Record<string, unknown>> {
  const call = [...fetchMock.mock.calls].reverse().find((c) => (c[0] as Request).method === "PATCH");
  const request = call?.[0] as Request | undefined;
  if (!request) throw new Error("no PATCH request was made");
  return request.clone().json() as Promise<Record<string, unknown>>;
}

describe("ReviewScreen", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the queue and the current transaction", async () => {
    await renderScreen({
      transactions: [
        makeTransaction({ id: 1, description_clean: "Coffee Shop", category_key: null, subcategory_key: null }),
        makeTransaction({ id: 2, description_clean: "Grocery Store" }),
      ],
      categories: CATEGORIES,
    });

    await waitFor(() => expect(screen.getByText("2 need review")).toBeInTheDocument());
    expect(screen.getByText("Coffee Shop")).toBeInTheDocument();
  });

  it("j/k move between transactions", async () => {
    await renderScreen({
      transactions: [
        makeTransaction({ id: 1, description_clean: "Coffee Shop" }),
        makeTransaction({ id: 2, description_clean: "Grocery Store" }),
      ],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("Coffee Shop")).toBeInTheDocument());

    fireEvent.keyDown(document, { key: "j" });
    await waitFor(() => expect(screen.getByText("Grocery Store")).toBeInTheDocument());

    fireEvent.keyDown(document, { key: "k" });
    await waitFor(() => expect(screen.getByText("Coffee Shop")).toBeInTheDocument());
  });

  it("a digit stages a category, and Enter accepts it (PATCH + queue shrinks)", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [
        makeTransaction({ id: 1, description_clean: "Coffee Shop", category_key: null, subcategory_key: null }),
        makeTransaction({ id: 2, description_clean: "Grocery Store" }),
      ],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("Coffee Shop")).toBeInTheDocument());

    fireEvent.keyDown(document, { key: "1" }); // stages "dining"
    await waitFor(() => expect(screen.getAllByText("Dining").length).toBeGreaterThan(0));

    fireEvent.keyDown(document, { key: "Enter" });

    await waitFor(async () => {
      const body = await lastPatchBody(fetchMock);
      expect(body).toMatchObject({ category_key: "dining", subcategory_key: "dining", create_rule: false });
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(screen.getByText("Grocery Store")).toBeInTheDocument();
  });

  it("r accepts and creates a rule (create_rule: true)", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());

    fireEvent.keyDown(document, { key: "r" });

    await waitFor(async () => {
      const body = await lastPatchBody(fetchMock);
      expect(body.create_rule).toBe(true);
    });
    await waitFor(() => expect(screen.getByText("Nothing needs review")).toBeInTheDocument());
  });

  it("u undoes the most recent accept, restoring the row", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1, category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());

    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("Nothing needs review")).toBeInTheDocument());

    fireEvent.keyDown(document, { key: "u" });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
  });

  it("ignores keyboard shortcuts while a text input has focus", async () => {
    await renderScreen({
      transactions: [
        makeTransaction({ id: 1, description_clean: "Coffee Shop" }),
        makeTransaction({ id: 2, description_clean: "Grocery Store" }),
      ],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("Coffee Shop")).toBeInTheDocument());

    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    fireEvent.keyDown(input, { key: "j" });

    expect(screen.getByText("Coffee Shop")).toBeInTheDocument(); // did not move
    document.body.removeChild(input);
  });

  it("scopes the queue to the user in ?user= and labels the subtitle", async () => {
    const { fetchMock } = await renderScreen(
      {
        transactions: [makeTransaction({ id: 1, description_clean: "Coffee Shop" })],
        categories: CATEGORIES,
        users: [
          { id: 1, name: "Alex", is_default: true },
          { id: 2, name: "Sam", is_default: false },
        ],
      },
      "/spending/review?user=2",
    );

    await waitFor(() => expect(screen.getByText("1 need review (Sam)")).toBeInTheDocument());
    const txnCalls = fetchMock.mock.calls
      .map((c) => new URL((c[0] as Request).url))
      .filter((u) => u.pathname === "/api/transactions");
    expect(txnCalls.length).toBeGreaterThan(0);
    expect(txnCalls.every((u) => u.searchParams.getAll("user_ids").join() === "2")).toBe(true);
  });

  it("reloads the queue when another user chip is chosen", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, description_clean: "Coffee Shop" })],
      categories: CATEGORIES,
      users: [
        { id: 1, name: "Alex", is_default: true },
        { id: 2, name: "Sam", is_default: false },
      ],
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("radio", { name: "Sam" }));

    await waitFor(() => expect(screen.getByText("1 need review (Sam)")).toBeInTheDocument());
    const last = [...fetchMock.mock.calls]
      .map((c) => new URL((c[0] as Request).url))
      .reverse()
      .find((u) => u.pathname === "/api/transactions");
    expect(last?.searchParams.getAll("user_ids")).toEqual(["2"]);
  });
});
