import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Category } from "./types";

const CATEGORIES: Category[] = [
  {
    key: "groceries",
    name: "Groceries",
    subcategories: [{ id: 1, key: "supermarket", name: "Supermarket", pending: false }],
  },
];

const TRANSACTIONS = [
  {
    id: 1,
    statement_id: 1,
    account_id: 1,
    posted_date: "2026-01-05",
    transaction_date: null,
    description_clean: "Corner Store",
    amount_minor: 1250,
    currency: "USD",
    kind: "purchase",
    category_key: null,
    subcategory_key: null,
    merchant_key: "corner-store",
    notes: null,
    needs_review: true,
  },
  {
    id: 2,
    statement_id: 1,
    account_id: 1,
    posted_date: "2026-01-06",
    transaction_date: null,
    description_clean: "Big Mart",
    amount_minor: 3400,
    currency: "USD",
    kind: "purchase",
    category_key: "groceries",
    subcategory_key: "supermarket",
    merchant_key: "big-mart",
    notes: null,
    needs_review: false,
  },
];

const USERS = [
  { id: 1, name: "Alex", is_default: true },
  { id: 2, name: "Sam", is_default: false },
];

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

/** `openapi-fetch` always calls `fetch(new Request(url, init))` — a single `Request` argument,
 *  not `fetch(url, init)` — so every mock below reads the method/url off that `Request` (see
 *  `src/api/client.test.ts` for the same pattern). */
function callsTo(fetchMock: FetchMock, method: string, urlIncludes: string): Request[] {
  return fetchMock.mock.calls
    .map(([request]) => request)
    .filter((request) => request.method === method && request.url.includes(urlIncludes));
}

type FetchMock = ReturnType<typeof vi.fn<(request: Request) => Response | Promise<Response>>>;

describe("TransactionsScreen", () => {
  beforeEach(() => {
    vi.resetModules();
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // Every test needs the *typed API client's* `globalThis.fetch` reference captured after this
  // test's own mock is installed (`openapi-fetch` binds `fetch` once, at `createClient()` time
  // — see `src/api/client.ts`). `vi.resetModules()` plus a dynamic import of the whole chain
  // (provider included, so the `ToastContext` object `useToast` reads is the same instance the
  // dynamically-imported screen was built against) gives each test a fresh, correctly-bound
  // module graph.
  async function mountWithFetch(
    fetchImpl: (request: Request) => Promise<Response> | Response,
    initialEntry = "/spending/transactions",
  ) {
    const fetchMock: FetchMock = vi.fn((request) => {
      if (request.url.includes("/api/users")) return jsonResponse(USERS);
      return fetchImpl(request);
    });
    vi.stubGlobal("fetch", fetchMock);
    const [{ default: TransactionsScreen }, { ToastContextProvider }, { Toaster }, { WithUserSwitcher }] = await Promise.all([
      import("./index"),
      import("@/components/ui/toast-provider"),
      import("@/components/ui/toaster"),
      import("@/test/with-user-switcher"),
    ]);
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter
          initialEntries={[initialEntry]}
          future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
        >
          <ToastContextProvider>
            <WithUserSwitcher>
              <TransactionsScreen />
            </WithUserSwitcher>
            <Toaster />
          </ToastContextProvider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return fetchMock;
  }

  it("lists transactions once the grid and taxonomy load", async () => {
    await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    expect(await screen.findByText("Corner Store")).toBeInTheDocument();
    expect(screen.getByText("Big Mart")).toBeInTheDocument();
    expect(screen.getByText("2 transactions")).toBeInTheDocument();
  });

  it("shows an empty state when no transaction matches", async () => {
    await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.url.includes("/api/transactions")) return jsonResponse({ items: [], total: 0 });
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    expect(await screen.findByText("No transactions match these filters")).toBeInTheDocument();
  });

  it("edits a transaction's category through the inline popover", async () => {
    const user = userEvent.setup();
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      if (request.method === "PATCH" && request.url.includes("/api/transactions/1")) {
        return jsonResponse({
          ...TRANSACTIONS[0],
          category_key: "groceries",
          subcategory_key: "supermarket",
        });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    await user.click(screen.getByRole("button", { name: "Uncategorized" }));

    const dialog = await screen.findByRole("dialog", { name: "Edit transaction 1" });
    await user.selectOptions(within(dialog).getByLabelText("Category"), "groceries");
    await user.selectOptions(within(dialog).getByLabelText("Subcategory"), "supermarket");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const patchCalls = callsTo(fetchMock, "PATCH", "/api/transactions/1");
    expect(patchCalls).toHaveLength(1);
    const body = (await patchCalls[0]!.clone().json()) as Record<string, unknown>;
    expect(body).toMatchObject({ category_key: "groceries", subcategory_key: "supermarket" });
  });

  it("opens the editor from the keyboard and returns focus to the row's Edit button on Escape", async () => {
    const user = userEvent.setup();
    await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    const edit = screen.getByRole("button", { name: "Edit Corner Store" });
    edit.focus();
    await user.keyboard("{Enter}");

    const dialog = await screen.findByRole("dialog", { name: "Edit transaction 1" });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(edit).toHaveFocus();
  });

  it("moves focus to the grid when saving drops the edited row out of the filter", async () => {
    const user = userEvent.setup();
    let saved = false;
    await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return saved
          ? jsonResponse({ items: TRANSACTIONS.slice(1), total: 1 })
          : jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      if (request.method === "PATCH" && request.url.includes("/api/transactions/1")) {
        saved = true;
        return jsonResponse({ ...TRANSACTIONS[0], notes: "x" });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    const edit = screen.getByRole("button", { name: "Edit Corner Store" });
    edit.focus();
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "Edit transaction 1" });
    await user.type(within(dialog).getByLabelText("Notes"), "x");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(screen.queryByText("Corner Store")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("transactions-grid")).toHaveFocus());
  });

  it("sends create_rule: false for a plain edit and true only when the rule box is ticked", async () => {
    const user = userEvent.setup();
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      if (request.method === "PATCH" && request.url.includes("/api/transactions/1")) {
        return jsonResponse(TRANSACTIONS[0]);
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    for (const ticked of [false, true]) {
      await user.click(screen.getByRole("button", { name: "Uncategorized" }));
      const dialog = await screen.findByRole("dialog", { name: "Edit transaction 1" });
      expect(within(dialog).getByTestId("edit-scope-note")).toHaveTextContent("this transaction only");
      await user.selectOptions(within(dialog).getByLabelText("Category"), "groceries");
      await user.selectOptions(within(dialog).getByLabelText("Subcategory"), "supermarket");
      if (ticked) await user.click(within(dialog).getByRole("checkbox", { name: /Also create a rule/ }));
      await user.click(within(dialog).getByRole("button", { name: "Save" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    }

    const patchCalls = callsTo(fetchMock, "PATCH", "/api/transactions/1");
    expect(patchCalls).toHaveLength(2);
    const bodies = await Promise.all(
      patchCalls.map((call) => call.clone().json() as Promise<Record<string, unknown>>),
    );
    expect(bodies[0]).toMatchObject({ category_key: "groceries", create_rule: false });
    expect(bodies[1]).toMatchObject({ category_key: "groceries", create_rule: true });
  });

  it("bulk-applies a label to selected rows", async () => {
    const user = userEvent.setup();
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      if (request.method === "POST" && request.url.includes("/api/transactions/bulk-update")) {
        return jsonResponse({ updated: 1 });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    await user.click(screen.getByRole("checkbox", { name: "Select transaction 1" }));

    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Bulk category"), "groceries");
    await user.selectOptions(screen.getByLabelText("Bulk subcategory"), "supermarket");
    await user.click(screen.getByRole("button", { name: "Apply to 1" }));

    await waitFor(() => expect(screen.getByText("Updated 1 transaction")).toBeInTheDocument());

    const bulkCalls = callsTo(fetchMock, "POST", "/api/transactions/bulk-update");
    expect(bulkCalls).toHaveLength(1);
    const body = (await bulkCalls[0]!.clone().json()) as Record<string, unknown>;
    expect(body).toMatchObject({ transaction_ids: [1], category_key: "groceries" });
  });

  it("scopes the grid to the user chosen in the switcher", async () => {
    const user = userEvent.setup();
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.url.includes("/api/transactions")) return jsonResponse({ items: TRANSACTIONS, total: 2 });
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });
    expect(await screen.findByText("Corner Store")).toBeInTheDocument();
    expect(callsTo(fetchMock, "GET", "/api/transactions").every((r) => !r.url.includes("user_ids"))).toBe(true);

    await user.click(screen.getByRole("radio", { name: "Sam" }));

    await waitFor(() => {
      const calls = callsTo(fetchMock, "GET", "/api/transactions");
      expect(new URL(calls.at(-1)!.url).searchParams.getAll("user_ids")).toEqual(["2"]);
    });
  });

  it("honours ?user= from the URL on first load", async () => {
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.url.includes("/api/transactions")) return jsonResponse({ items: TRANSACTIONS, total: 2 });
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    }, "/spending/transactions?user=1");
    expect(await screen.findByText("Corner Store")).toBeInTheDocument();
    const calls = callsTo(fetchMock, "GET", "/api/transactions");
    expect(calls.every((r) => new URL(r.url).searchParams.getAll("user_ids").join() === "1")).toBe(true);
    expect(screen.getByRole("radio", { name: "Alex" })).toHaveAttribute("aria-checked", "true");
  });

  it("selects every matching transaction via the ids endpoint, then confirms before applying", async () => {
    const user = userEvent.setup();
    const allIds = Array.from({ length: 1234 }, (_, i) => i + 1);
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions/ids")) {
        return jsonResponse({ ids: allIds, total: allIds.length });
      }
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: allIds.length });
      }
      if (request.method === "POST" && request.url.includes("/api/transactions/bulk-update")) {
        return jsonResponse({ updated: allIds.length });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    await user.click(screen.getByRole("checkbox", { name: "Select transaction 1" }));
    await user.click(screen.getByRole("button", { name: "Select all 1234 matching filters" }));

    expect(await screen.findByText("All 1,234 matching selected")).toBeInTheDocument();
    // The grid's list endpoint caps a page at 500 rows: the selection must come from /ids.
    expect(callsTo(fetchMock, "GET", "/api/transactions/ids")).toHaveLength(1);

    await user.selectOptions(screen.getByLabelText("Bulk category"), "groceries");
    await user.selectOptions(screen.getByLabelText("Bulk subcategory"), "supermarket");
    await user.click(screen.getByRole("button", { name: "Apply to 1234" }));

    // Applying to everything matching asks first, with the count, and sends nothing yet.
    const confirm = await screen.findByRole("alertdialog", { name: "Confirm bulk update" });
    expect(confirm).toHaveTextContent("all 1,234 matching transactions");
    expect(callsTo(fetchMock, "POST", "/api/transactions/bulk-update")).toHaveLength(0);

    await user.click(within(confirm).getByRole("button", { name: "Yes, apply to 1,234" }));
    await waitFor(() => expect(screen.getByText("Updated 1234 transactions")).toBeInTheDocument());
    const bulkCalls = callsTo(fetchMock, "POST", "/api/transactions/bulk-update");
    const body = (await bulkCalls[0]!.clone().json()) as { transaction_ids: number[] };
    expect(body.transaction_ids).toHaveLength(1234);
  });

  it("drops the selection, including select-all-matching, when a filter changes", async () => {
    const user = userEvent.setup();
    const allIds = Array.from({ length: 1234 }, (_, i) => i + 1);
    await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions/ids")) {
        return jsonResponse({ ids: allIds, total: allIds.length });
      }
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: allIds.length });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    await user.click(screen.getByRole("checkbox", { name: "Select transaction 1" }));
    await user.click(screen.getByRole("button", { name: "Select all 1234 matching filters" }));
    expect(await screen.findByText("All 1,234 matching selected")).toBeInTheDocument();

    // Sorting keeps the same matched set, so the selection survives it.
    await user.click(screen.getByRole("columnheader", { name: /Amount/ }));
    expect(screen.getByText("All 1,234 matching selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Needs review" }));
    await waitFor(() => expect(screen.queryByText("All 1,234 matching selected")).not.toBeInTheDocument());
    expect(screen.queryByText(/selected/)).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Select transaction 1" })).not.toBeChecked();

    // A hand-ticked row is dropped the same way when the filters change again.
    await user.click(screen.getByRole("checkbox", { name: "Select transaction 1" }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Needs review" }));
    await waitFor(() => expect(screen.queryByText("1 selected")).not.toBeInTheDocument());
  });

  it("does not report a partial bulk update as a success", async () => {
    const user = userEvent.setup();
    await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      if (request.method === "POST" && request.url.includes("/api/transactions/bulk-update")) {
        return jsonResponse({ updated: 1 });
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    await user.click(screen.getByRole("checkbox", { name: "Select transaction 1" }));
    await user.click(screen.getByRole("checkbox", { name: "Select transaction 2" }));
    await user.selectOptions(screen.getByLabelText("Bulk category"), "groceries");
    await user.selectOptions(screen.getByLabelText("Bulk subcategory"), "supermarket");
    await user.click(screen.getByRole("button", { name: "Apply to 2" }));

    expect(await screen.findByText("Updated 1 of 2 transactions")).toBeInTheDocument();
    expect(screen.getByText(/1 could not be updated/)).toBeInTheDocument();
  });

  it("saves a kind-only edit and a notes edit without sending a category", async () => {
    const user = userEvent.setup();
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.method === "GET" && request.url.includes("/api/transactions")) {
        return jsonResponse({ items: TRANSACTIONS, total: 2 });
      }
      if (request.method === "PATCH" && request.url.includes("/api/transactions/1")) {
        return jsonResponse(TRANSACTIONS[0]);
      }
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });

    await screen.findByText("Corner Store");
    await user.click(screen.getByRole("button", { name: "Uncategorized" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit transaction 1" });
    await user.selectOptions(within(dialog).getByLabelText("Kind"), "transfer");
    await user.type(within(dialog).getByLabelText("Notes"), "moved to savings");
    await user.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const patchCalls = callsTo(fetchMock, "PATCH", "/api/transactions/1");
    expect(patchCalls).toHaveLength(1);
    const body = (await patchCalls[0]!.clone().json()) as Record<string, unknown>;
    expect(body).toMatchObject({ kind: "transfer", notes: "moved to savings" });
    expect(body).not.toHaveProperty("category_key");
  });

  it("filters to needs-review rows and sorts by amount through the API", async () => {
    const user = userEvent.setup();
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.url.includes("/api/transactions")) return jsonResponse({ items: TRANSACTIONS, total: 2 });
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    });
    await screen.findByText("Corner Store");

    await user.click(screen.getByRole("button", { name: "Needs review" }));
    await waitFor(() => {
      const last = new URL(callsTo(fetchMock, "GET", "/api/transactions").at(-1)!.url);
      expect(last.searchParams.get("needs_review")).toBe("true");
    });

    await user.click(screen.getByRole("columnheader", { name: /Amount/ }));
    await waitFor(() => {
      const last = new URL(callsTo(fetchMock, "GET", "/api/transactions").at(-1)!.url);
      expect(last.searchParams.get("sort")).toBe("-amount_minor");
      expect(last.searchParams.get("needs_review")).toBe("true"); // the two params coexist
    });
  });

  it("opens on one statement's rows from ?statement_id=", async () => {
    const fetchMock = await mountWithFetch((request) => {
      if (request.url.includes("/api/taxonomy")) return jsonResponse(CATEGORIES);
      if (request.url.includes("/api/transactions")) return jsonResponse({ items: TRANSACTIONS, total: 2 });
      throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
    }, "/spending/transactions?statement_id=9&needs_review=true");
    expect(await screen.findByText("Corner Store")).toBeInTheDocument();
    const params = new URL(callsTo(fetchMock, "GET", "/api/transactions").at(-1)!.url).searchParams;
    expect(params.get("statement_id")).toBe("9");
    expect(params.get("needs_review")).toBe("true");
    expect(screen.getByRole("button", { name: "Remove filter: statement 9" })).toBeInTheDocument();
  });
  describe("conflicting suggestions", () => {
    const CONFLICT_CANDIDATES = {
      transaction_id: 1,
      conflict: true,
      candidates: [
        {
          source: "user_rule",
          category_key: "groceries",
          subcategory_key: "supermarket",
          kind: null,
          label: "corner",
          rule_id: 2,
          is_current: true,
        },
        {
          source: "builtin_rule",
          category_key: "fuel",
          subcategory_key: "gasoline",
          kind: "refund",
          label: "CORNER",
          rule_id: 7,
          is_current: false,
        },
      ],
    };
    const CATEGORIES_WITH_FUEL: Category[] = [
      ...CATEGORIES,
      {
        key: "fuel",
        name: "Fuel",
        subcategories: [{ id: 2, key: "gasoline", name: "Gasoline", pending: false }],
      },
    ];
    const FLAGGED = [
      { ...TRANSACTIONS[1], category_key: "groceries", subcategory_key: "supermarket", conflict: true },
      { ...TRANSACTIONS[0], category_key: "groceries", subcategory_key: "supermarket", conflict: false },
    ];

    function routes(fetchLog?: string[]) {
      return (request: Request) => {
        const url = new URL(request.url);
        fetchLog?.push(`${request.method} ${url.pathname}${url.search}`);
        if (url.pathname === "/api/taxonomy") return jsonResponse(CATEGORIES_WITH_FUEL);
        if (url.pathname === "/api/transactions/1/candidates") {
          return jsonResponse(CONFLICT_CANDIDATES);
        }
        if (request.method === "GET" && url.pathname === "/api/transactions") {
          return jsonResponse({ items: FLAGGED, total: 2 });
        }
        if (request.method === "POST" && url.pathname === "/api/review/accept") {
          return jsonResponse({
            transaction: { ...FLAGGED[1], category_key: "fuel", subcategory_key: "gasoline" },
            similar_ids: [],
            undo: { transactions: [], rule: null },
          });
        }
        if (request.method === "POST" && url.pathname === "/api/review/undo") {
          return jsonResponse({ restored: 1 });
        }
        throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
      };
    }

    it("marks only rows whose sources disagree, and asks the list for the flags", async () => {
      const fetchMock = await mountWithFetch(routes());
      await screen.findByText("Big Mart");

      expect(screen.getAllByRole("img", { name: "Conflicting suggestions" })).toHaveLength(1);
      const list = callsTo(fetchMock, "GET", "/api/transactions").at(-1)!;
      expect(new URL(list.url).searchParams.get("include_conflict")).toBe("true");
    });

    it("filters to conflicting rows through the API and the URL", async () => {
      const user = userEvent.setup();
      const fetchMock = await mountWithFetch(routes());
      await screen.findByText("Big Mart");

      await user.click(screen.getByRole("button", { name: "Conflicting suggestions" }));

      await waitFor(() => {
        const last = new URL(callsTo(fetchMock, "GET", "/api/transactions").at(-1)!.url);
        expect(last.searchParams.get("conflict")).toBe("true");
      });
      expect(screen.getByRole("button", { name: "Conflicting suggestions" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    });

    it("opens on conflicting rows from ?conflict=true", async () => {
      const fetchMock = await mountWithFetch(routes(), "/spending/transactions?conflict=true");
      await screen.findByText("Big Mart");
      const params = new URL(callsTo(fetchMock, "GET", "/api/transactions").at(-1)!.url).searchParams;
      expect(params.get("conflict")).toBe("true");
    });

    it("shows the chips for the edited row and applies one as a one-time edit", async () => {
      const user = userEvent.setup();
      const fetchMock = await mountWithFetch(routes());
      await screen.findByText("Corner Store");

      await user.click(screen.getByRole("button", { name: "Edit Corner Store" }));
      const dialog = await screen.findByRole("dialog", { name: "Edit transaction 1" });
      const group = await within(dialog).findByRole("group", { name: "Suggested categories" });
      expect(within(group).getByRole("note")).toHaveTextContent("Sources disagree");

      await user.click(
        within(group).getByRole("button", { name: "Fuel › Gasoline, built-in" }),
      );

      // One click: the dialog closes and the edit goes out as an accept (a one-time edit with an
      // undo token) for that row only, with no rule and the candidate's kind.
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      const accepts = callsTo(fetchMock, "POST", "/api/review/accept");
      expect(accepts).toHaveLength(1);
      expect(await accepts[0]!.clone().json()).toEqual({
        transaction_id: 1,
        category_key: "fuel",
        subcategory_key: "gasoline",
        create_rule: false,
        similar_ids: [],
        kind: "refund",
      });
      expect(callsTo(fetchMock, "PATCH", "/api/transactions")).toHaveLength(0);

      expect(await screen.findByText("Set to Fuel › Gasoline")).toBeInTheDocument();
      await user.click(screen.getByRole("button", { name: "Undo" }));
      await waitFor(() => expect(callsTo(fetchMock, "POST", "/api/review/undo")).toHaveLength(1));
      expect(await screen.findByText("Undone")).toBeInTheDocument();
    });
  });
});
