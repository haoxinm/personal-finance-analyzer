import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

  it("scopes the queue to the user in ?user=", async () => {
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

    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
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

    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
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
    expect(await screen.findByRole("button", { name: "Always categorize “trader joes” this way" })).toBeEnabled();
    expect(screen.queryByText(/Accept \+ create rule/)).not.toBeInTheDocument();
  });

  it("says Accept is for this transaction only and the other button saves a rule", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    const accept = await screen.findByRole("button", { name: "Accept" });
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
    fireEvent.click(await screen.findByRole("button", { name: "Always categorize “trader joes” this way" }));
    await waitFor(async () => expect((await lastAcceptBody(fetchMock)).create_rule).toBe(true));
  });

  it("shows shortcut keys as visible hints and announces them via aria-keyshortcuts, keeping plain names", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "trader joes", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    expect(await screen.findByRole("heading", { level: 1, name: "Review transactions" })).toBeInTheDocument();
    const accept = await screen.findByRole("button", { name: "Accept" });
    expect(accept).toHaveAttribute("aria-keyshortcuts", "Enter");
    expect(accept.querySelector("kbd")).toHaveTextContent("Enter");
    const undo = screen.getByRole("button", { name: "Undo" });
    expect(undo).toHaveAttribute("aria-keyshortcuts", "u");
    expect(undo.querySelector("kbd")).toHaveTextContent("u");
    const rule = screen.getByRole("button", { name: "Always categorize “trader joes” this way" });
    expect(rule).toHaveAttribute("aria-keyshortcuts", "r");
    expect(rule.querySelector("kbd")).toHaveTextContent("r");
  });

  it("calls the approval list \"New categories to approve\", not pending stores", async () => {
    await renderScreen({ transactions: [], categories: CATEGORIES });
    expect(await screen.findByRole("heading", { name: "New categories to approve" })).toBeInTheDocument();
    expect(screen.queryByText(/pending stores/i)).not.toBeInTheDocument();
  });

  it("cannot make a rule for a row with no merchant name to match", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1, merchant_key: "", category_key: "groceries", subcategory_key: "groceries" })],
      categories: CATEGORIES,
    });
    expect(await screen.findByRole("button", { name: "Always categorize this way" })).toBeDisabled();
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

describe("ReviewScreen suggestion chips", () => {
  const CANDIDATES: NonNullable<Transaction["candidates"]> = [
    {
      source: "user_rule",
      category_key: "dining",
      subcategory_key: "restaurants",
      kind: null,
      label: "costco",
      rule_id: 4,
      is_current: false,
      is_automatic: true,
    },
    {
      source: "builtin_rule",
      category_key: "groceries",
      subcategory_key: "groceries",
      kind: "refund",
      label: "COSTCO",
      rule_id: 9,
      is_current: false,
      is_automatic: false,
    },
  ];

  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("asks the queue for each row's candidates and shows them with the conflict explanation", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, candidates: CANDIDATES, conflict: true })],
      categories: CATEGORIES,
    });

    const group = await screen.findByRole("group", { name: "Suggested categories" });
    expect(within(group).getByRole("note")).toHaveTextContent("Sources disagree");
    expect(
      within(group).getByRole("button", { name: "Dining › Restaurants, your rule 'costco'" }),
    ).toBeInTheDocument();
    expect(
      within(group).getByRole("button", { name: "Groceries › Groceries, built-in" }),
    ).toBeInTheDocument();
    const queueRequest = fetchMock.mock.calls
      .map((c) => new URL((c[0] as Request).url))
      .find((url) => url.pathname === "/api/transactions" && url.searchParams.get("needs_review") === "true");
    expect(queueRequest?.searchParams.get("include_candidates")).toBe("true");
  });

  it("shows no chips for a row without suggestions", async () => {
    await renderScreen({
      transactions: [makeTransaction({ id: 1 })],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(screen.queryByRole("group", { name: "Suggested categories" })).not.toBeInTheDocument();
  });

  it("one click accepts that candidate for this row only (no rule, no merchant mates) and u undoes it", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [
        makeTransaction({
          id: 1,
          description_clean: "Costco One",
          merchant_key: "costco",
          candidates: CANDIDATES,
          conflict: true,
        }),
        makeTransaction({ id: 2, description_clean: "Costco Two", merchant_key: "costco" }),
      ],
      categories: CATEGORIES,
    });
    await waitFor(() => expect(screen.getByText("Costco One")).toBeInTheDocument());
    // The "apply to the merchant's other rows" box is on by default; a chip must ignore it.
    expect(screen.getByRole("checkbox")).toBeChecked();

    fireEvent.click(screen.getByRole("button", { name: "Groceries › Groceries, built-in" }));

    await waitFor(async () => {
      const body = await lastAcceptBody(fetchMock);
      expect(body).toEqual({
        transaction_id: 1,
        category_key: "groceries",
        subcategory_key: "groceries",
        create_rule: false,
        similar_ids: [],
        kind: "refund",
      });
    });
    await waitFor(() => expect(screen.getByText("1 need review")).toBeInTheDocument());
    expect(screen.getByText("Costco Two")).toBeInTheDocument();
    expect(await screen.findByText(/Applied suggestion/)).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "u" });
    await waitFor(() => expect(screen.getByText("2 need review")).toBeInTheDocument());
    const undoCall = fetchMock.mock.calls.find(
      (c) => new URL((c[0] as Request).url).pathname === "/api/review/undo",
    );
    expect(undoCall).toBeDefined();
  });

  it("chips are reachable and activatable from the keyboard", async () => {
    const { fetchMock } = await renderScreen({
      transactions: [makeTransaction({ id: 1, candidates: CANDIDATES, conflict: true })],
      categories: CATEGORIES,
    });
    const chip = await screen.findByRole("button", { name: "Dining › Restaurants, your rule 'costco'" });
    chip.focus();
    expect(chip).toHaveFocus();

    // Enter on a focused chip is that chip's click; the screen's own Enter shortcut ("accept
    // the current proposal") must stand aside, so exactly the chip's category is accepted.
    await userEvent.keyboard("{Enter}");

    await waitFor(async () => {
      expect(await lastAcceptBody(fetchMock)).toMatchObject({
        category_key: "dining",
        subcategory_key: "restaurants",
        create_rule: false,
      });
    });
    const accepts = fetchMock.mock.calls.filter(
      (c) => new URL((c[0] as Request).url).pathname === "/api/review/accept",
    );
    expect(accepts).toHaveLength(1);
  });
});
