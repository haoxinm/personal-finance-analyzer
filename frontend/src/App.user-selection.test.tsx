import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiFetchMock, jsonResponse, makeCategory, makeTransaction } from "@/screens/review/test-support";

// Settings has its own data needs and is owned elsewhere; here only its route matters.
vi.mock("@/screens/settings", () => ({
  default: () => <h1>Settings page</h1>,
  screenMeta: { path: "/settings", title: "Settings" },
}));

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const ALEX = { id: 1, name: "Alex", is_default: true };
const SAM = { id: 2, name: "Sam", is_default: false };
const EMPTY_NET_WORTH = {
  currency: "USD",
  user_id: null,
  periods: [],
  series: [],
  account_series: [],
  snapshot: null,
};

const COFFEE = makeTransaction({ id: 1, description_clean: "Coffee Shop", category_key: null, subcategory_key: null });
const GROCERY = makeTransaction({ id: 2, description_clean: "Grocery Store", category_key: null, subcategory_key: null });
const CATEGORIES = [
  makeCategory({
    key: "dining",
    name: "Dining",
    subcategories: [{ key: "dining", name: "Dining", pending: false }] as never,
  }),
];

/** Route-aware API stub: users, per-user transactions, and empty bodies elsewhere. */
function makeFetch(users: Array<typeof ALEX>) {
  const base = createApiFetchMock({ categories: CATEGORIES, transactions: [COFFEE, GROCERY], users });
  return vi.fn(async (input: Request | string | URL): Promise<Response> => {
    const request = input instanceof Request ? input : new Request(input);
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/api/users") return jsonResponse(users);
    if (request.method === "GET" && url.pathname === "/api/transactions") {
      const ids = url.searchParams.getAll("user_ids").map(Number);
      const items = ids.includes(1) ? [COFFEE] : ids.includes(2) ? [GROCERY] : [COFFEE, GROCERY];
      return jsonResponse({ items, total: items.length });
    }
    if (url.pathname.startsWith("/api/settings")) return jsonResponse({ llm: { mode: "none" } });
    if (url.pathname === "/api/net-worth/summary") return jsonResponse(EMPTY_NET_WORTH);
    if (request.method === "PATCH" || url.pathname === "/api/taxonomy") return base(request);
    return jsonResponse([]);
  });
}

function LocationProbe() {
  const { pathname, search } = useLocation();
  return <div data-testid="location">{pathname + search}</div>;
}

async function renderApp(path: string, users = [ALEX, SAM]) {
  vi.resetModules();
  const fetchMock = makeFetch(users);
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  const { App } = await import("./App");
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const utils = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { fetchMock, ...utils };
}

const bar = () => screen.getByTestId("user-bar");

describe("global user bar", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([
    "/spending",
    "/spending/import",
    "/spending/review",
    "/spending/transactions",
    "/spending/rules",
    "/spending/layout-mapper",
    "/tax",
    "/tax/documents",
    "/tax/import",
    "/net-worth",
  ])(
    "is shown on %s",
    async (path) => {
      await renderApp(path);
      expect(await within(await screen.findByTestId("user-bar")).findByText("Viewing:")).toBeInTheDocument();
      expect(within(bar()).getByTestId("user-chip")).toHaveTextContent("All users");
    },
  );

  it("is not shown on Settings", async () => {
    await renderApp("/settings");
    await screen.findByRole("heading", { name: "Settings page" });
    expect(screen.queryByTestId("user-bar")).not.toBeInTheDocument();
  });

  it("shows a static, labelled chip for a single user (no switcher, no compare)", async () => {
    await renderApp("/spending", [ALEX]);
    await waitFor(() => expect(within(bar()).getByTestId("user-chip")).toHaveTextContent("Alex"));
    expect(within(bar()).getByText("Viewing:")).toBeInTheDocument();
    expect(within(bar()).queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(within(bar()).queryByRole("button", { name: "Compare" })).not.toBeInTheDocument();
  });

  it("switching user updates the URL, the chip and the screen's queries", async () => {
    const user = userEvent.setup();
    const { fetchMock } = await renderApp("/spending/transactions");
    expect(await screen.findByText("Coffee Shop")).toBeInTheDocument();
    expect(screen.getByText("Grocery Store")).toBeInTheDocument();

    await user.click(within(bar()).getByRole("radio", { name: "Sam" }));

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("user=2"));
    expect(within(bar()).getByTestId("user-chip")).toHaveTextContent("Sam");
    await waitFor(() => expect(screen.queryByText("Coffee Shop")).not.toBeInTheDocument());
    expect(screen.getByText("Grocery Store")).toBeInTheDocument();
    const urls = fetchMock.mock.calls.map(([r]) => (r as Request).url);
    expect(urls.some((u) => u.includes("/api/transactions") && /user_ids=2/.test(u))).toBe(true);
    // One selector only: screens no longer render their own.
    expect(screen.getAllByRole("radiogroup")).toHaveLength(1);
  });

  it("says rules are global on the Rules screen", async () => {
    await renderApp("/spending/rules");
    expect(await within(await screen.findByTestId("user-bar")).findByText("Rules apply to all users.")).toBeInTheDocument();
  });

  it("disables Compare with an explanation where the screen cannot be split", async () => {
    await renderApp("/spending/rules");
    const button = await within(await screen.findByTestId("user-bar")).findByRole("button", { name: "Compare" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringContaining("Spending"));
  });
});

describe("compare mode", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("the Compare button adds ?compare= with another user and renders two panes", async () => {
    const user = userEvent.setup();
    await renderApp("/spending/transactions?user=1");
    await screen.findByText("Coffee Shop");
    await user.click(await within(bar()).findByRole("button", { name: "Compare" }));

    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("compare=2"));
    const panes = await screen.findAllByTestId("compare-pane");
    expect(panes).toHaveLength(2);
    expect(within(bar()).getByText("Filters apply to both.")).toBeInTheDocument();

    await user.click(within(bar()).getByRole("button", { name: "Stop comparing" }));
    await waitFor(() => expect(screen.queryByTestId("compare-panes")).not.toBeInTheDocument());
    expect(screen.getByTestId("location")).not.toHaveTextContent("compare");
  });

  it("renders each pane for its own user with a header chip and no duplicate ids", async () => {
    await renderApp("/net-worth?user=1&compare=2");
    const panes = await screen.findAllByTestId("compare-pane");
    expect(panes).toHaveLength(2);
    expect(within(panes[0]!).getAllByTestId("user-chip")[0]).toHaveTextContent("Alex");
    expect(within(panes[1]!).getAllByTestId("user-chip")[0]).toHaveTextContent("Sam");
    for (const pane of panes) {
      expect(await within(pane).findByTestId("net-worth-section")).toBeInTheDocument();
    }

    const ids = Array.from(document.querySelectorAll("[id]")).map((el) => el.id);
    expect(ids.length).toBeGreaterThan(0);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("each transactions pane is scoped to its own user", async () => {
    await renderApp("/spending/transactions?user=1&compare=2");
    const [left, right] = await screen.findAllByTestId("compare-pane");
    expect(await within(left!).findByText("Coffee Shop")).toBeInTheDocument();
    expect(within(left!).queryByText("Grocery Store")).not.toBeInTheDocument();
    expect(await within(right!).findByText("Grocery Store")).toBeInTheDocument();
    expect(within(right!).queryByText("Coffee Shop")).not.toBeInTheDocument();
  });

  it("choosing the other pane's user swaps the panes", async () => {
    const user = userEvent.setup();
    await renderApp("/spending/transactions?user=1&compare=2");
    await screen.findAllByTestId("compare-pane");
    const left = within(bar()).getByRole("radiogroup", { name: "Left user" });
    await user.click(within(left).getByRole("radio", { name: "Sam" }));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent(/user=2/));
    expect(screen.getByTestId("location")).toHaveTextContent(/compare=1/);
  });

  it("ignores ?compare= on screens that cannot be split", async () => {
    await renderApp("/spending/rules?user=1&compare=2");
    await within(await screen.findByTestId("user-bar")).findByText("Viewing:");
    expect(screen.queryByTestId("compare-panes")).not.toBeInTheDocument();
  });

  it("Review keyboard shortcuts act in the active pane only", async () => {
    const user = userEvent.setup();
    const { fetchMock } = await renderApp("/spending/review?user=1&compare=2");
    const [left, right] = await screen.findAllByTestId("compare-pane");
    await within(left!).findByText("Coffee Shop");
    await within(right!).findByText("Grocery Store");

    const patched = () =>
      fetchMock.mock.calls
        .map(([r]) => r as Request)
        .filter((r) => r.method === "PATCH")
        .map((r) => new URL(r.url).pathname);

    // Left pane is active by default: stage a category and accept; only transaction 1 is patched.
    fireEvent.keyDown(document, { key: "1" });
    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(patched()).toEqual(["/api/transactions/1"]));
    expect(within(right!).getByText("Grocery Store")).toBeInTheDocument();

    // Activate the right pane; now the same keys only affect transaction 2.
    await user.click(within(right!).getByText("Grocery Store"));
    expect(within(right!).getByTestId("shortcut-badge")).toHaveTextContent("active");
    fireEvent.keyDown(document, { key: "1" });
    fireEvent.keyDown(document, { key: "Enter" });
    await waitFor(() => expect(patched()).toEqual(["/api/transactions/1", "/api/transactions/2"]));
  });
});
