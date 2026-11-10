import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiFetchMock, makeCategory, makeTransaction, type AcceptBody } from "./test-support";
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
    settings?: { llm: { mode: string } };
    uncategorizedTotal?: number;
    reclassifyCount?: number;
  },
  initialEntry = "/spending/review",
) {
  vi.resetModules();
  const fetchMock = vi.fn(createApiFetchMock(state));
  vi.stubGlobal("fetch", fetchMock);

  const { default: ReviewScreen } = await import("./index");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const { Toaster } = await import("@/components/ui/toaster");
  const { WithUserSwitcher } = await import("@/test/with-user-switcher");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToastContextProvider>
          <WithUserSwitcher>
            <ReviewScreen />
          </WithUserSwitcher>
          <Toaster />
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

function lastAcceptBody(fetchMock: ReturnType<typeof vi.fn>): Promise<AcceptBody> {
  const call = [...fetchMock.mock.calls]
    .reverse()
    .find((c) => (c[0] as Request).method === "POST" && new URL((c[0] as Request).url).pathname === "/api/review/accept");
  const request = call?.[0] as Request | undefined;
  if (!request) throw new Error("no accept request was made");
  return request.clone().json() as Promise<AcceptBody>;
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

  it("a digit stages a category, and Enter accepts it (accept request + queue shrinks)", async () => {
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
      const body = await lastAcceptBody(fetchMock);
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
      const body = await lastAcceptBody(fetchMock);
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

  it("labels the rule button by what it does, naming the merchant", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    expect(await screen.findByRole("button", { name: "Always categorize “trader joes” this way (r)" })).toBeEnabled();
    expect(screen.queryByText(/Accept \+ create rule/)).not.toBeInTheDocument();
  });

  it("says Accept is for this transaction only and the other button saves a rule", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    const accept = await screen.findByRole("button", { name: "Accept (Enter)" });
    expect(accept).toHaveAttribute("title", expect.stringContaining("this transaction only"));
    const note = screen.getByText(/changes this transaction only; nothing is remembered about the merchant/);
    expect(note).toHaveTextContent(/second button also saves a rule/);

    fireEvent.click(accept);
    await waitFor(async () => expect((await lastAcceptBody(fetchMock)).create_rule).toBe(false));
  });

  it("the always-categorize button accepts with create_rule: true", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    fireEvent.click(await screen.findByRole("button", { name: "Always categorize “trader joes” this way (r)" }));
    await waitFor(async () => expect((await lastAcceptBody(fetchMock)).create_rule).toBe(true));
  });

  it("cannot make a rule for a row with no merchant name to match", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    expect(await screen.findByRole("button", { name: "Always categorize this way (r)" })).toBeDisabled();
  });

  it("offers to apply an accept to the merchant's other pending rows and says how many it covered", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [
        makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" }),
        makeTransaction({ id: 2, merchant_key: "trader joes", description_clean: "Trader Joes #2" }),
        makeTransaction({ id: 3, merchant_key: "shell", description_clean: "Shell Oil" }),
      ],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("3 need review")).toBeInTheDocument());
    expect(
      screen.getByLabelText("Also apply to 1 other pending transaction from this merchant"),
    ).toBeChecked();

    fireEvent.keyDown(document, { key: "Enter" });

    await waitFor(async () => expect((await lastAcceptBody(fetchMock)).similar_ids).toEqual([2]));
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(await screen.findByText(/Applied to 1 more from this merchant/)).toBeInTheDocument();
  });

  it("does not apply to the others when the checkbox is cleared", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [
        makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" }),
        makeTransaction({ id: 2, merchant_key: "trader joes" }),
      ],
      categories: CATEGORIES,
    });
    const checkbox = await screen.findByLabelText("Also apply to 1 other pending transaction from this merchant");
    fireEvent.click(checkbox);
    fireEvent.keyDown(document, { key: "Enter" });

    await waitFor(async () => expect((await lastAcceptBody(fetchMock)).similar_ids).toEqual([]));
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
  });

  it("undo does not show a developer note", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1, category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(screen.getByText("Nothing needs review")).toBeInTheDocument());
    fireEvent.keyDown(document, { key: "u" });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(document.body.textContent).not.toMatch(/index\.tsx|survive reload/);
  });

  it("links the empty state onward to Import and Transactions", async () => {
    await renderScreen({ transactions: [], categories: CATEGORIES });
    expect(await screen.findByText("Nothing needs review")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import statements" })).toHaveAttribute("href", "/spending/import");
    expect(screen.getByRole("link", { name: "Browse transactions" })).toHaveAttribute("href", "/spending/transactions");
  });

  it("explains that no LLM is set up when many transactions are uncategorized", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1 })],
      categories: CATEGORIES,
      settings: { llm: { mode: "none" } },
      uncategorizedTotal: 47,
    });
    const hint = await screen.findByTestId("llm-off-hint");
    expect(hint).toHaveTextContent("47 transactions could not be categorized automatically");
    expect(within(hint).getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/settings");
  });

  it("shows no LLM hint when a model is configured, or when only a few rows are uncategorized", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1 })],
      categories: CATEGORIES,
      settings: { llm: { mode: "remote" } },
      uncategorizedTotal: 47,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(screen.queryByTestId("llm-off-hint")).not.toBeInTheDocument();
  });

  it("stays quiet about the LLM when few rows are uncategorized", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1 })],
      categories: CATEGORIES,
      settings: { llm: { mode: "none" } },
      uncategorizedTotal: 3,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(screen.queryByTestId("llm-off-hint")).not.toBeInTheDocument();
  });

  it("re-classifies uncategorized rows from the header, then reloads the queue", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1 })],
      categories: CATEGORIES,
      reclassifyCount: 12,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    const queueFetches = () =>
      fetchMock.mock.calls.filter((c) => {
        const url = new URL((c[0] as Request).url);
        return url.pathname === "/api/transactions" && url.searchParams.get("needs_review") === "true";
      }).length;
    const before = queueFetches();

    fireEvent.click(screen.getByRole("button", { name: "Re-classify uncategorized" }));

    expect(await screen.findByText("Re-classified 12 transactions")).toBeInTheDocument();
    const run = fetchMock.mock.calls.find((c) => new URL((c[0] as Request).url).pathname === "/api/classify/run");
    expect(await (run?.[0] as Request).clone().json()).toMatchObject({ only_unclassified: true });
    await waitFor(() => expect(queueFetches()).toBeGreaterThan(before));
  });

  it("says so when there is nothing to re-classify", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1 })],
      categories: CATEGORIES,
      reclassifyCount: 0,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Re-classify uncategorized" }));
    expect(await screen.findByText("Nothing to re-classify")).toBeInTheDocument();
  });
});
