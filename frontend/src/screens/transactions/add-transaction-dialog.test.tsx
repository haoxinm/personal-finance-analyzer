import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Category } from "./types";
import { outsideStatementPeriod } from "./dates";

const CATEGORIES: Category[] = [
  {
    key: "groceries",
    name: "Groceries",
    subcategories: [{ id: 1, key: "supermarket", name: "Supermarket", pending: false }],
  },
];

function statement(over: Record<string, unknown>) {
  return {
    id: 1,
    user_id: 1,
    account_id: 5,
    status: "parsed",
    period_start: "2026-01-01",
    period_end: "2026-01-31",
    original_name: "jan.pdf",
    currency: "USD",
    created_at: "2026-02-01T00:00:00Z",
    ...over,
  };
}

const STATEMENTS = [
  statement({ id: 1 }),
  statement({ id: 2, original_name: "feb.pdf", period_start: "2026-02-01", period_end: "2026-02-28" }),
  statement({ id: 3, original_name: "broken.pdf", status: "error" }),
];

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

type Handler = (request: Request) => Response | Promise<Response>;

async function mount(
  opts: {
    initialStatementId?: number;
    post?: Handler;
    statements?: unknown[];
    userId?: number;
    onSwitchPerson?: (id: number) => void;
    getViewAction?: () => { label: string; onClick: () => void } | null;
  } = {},
) {
  const onClose = vi.fn();
  const fetchMock = vi.fn<(request: Request) => Response | Promise<Response>>((request) => {
    const url = new URL(request.url);
    if (url.pathname === "/api/statements") {
      const all = (opts.statements ?? STATEMENTS) as { user_id?: number }[];
      const owner = url.searchParams.get("user_id");
      return json(owner ? all.filter((st) => String(st.user_id) === owner) : all);
    }
    if (url.pathname === "/api/users") return json([{ id: 1, name: "Alex" }, { id: 2, name: "Sam" }]);
    if (url.pathname === "/api/accounts") return json([{ id: 5, user_id: 1, issuer_id: 1, account_type: "credit" }]);
    if (url.pathname === "/api/issuers") return json([{ id: 1, name: "Chase" }]);
    if (request.method === "POST" && url.pathname === "/api/transactions") {
      return (opts.post ?? (() => json({ id: 99, needs_review: false, category_key: "groceries" }, 201)))(request);
    }
    throw new Error(`unexpected fetch: ${request.method} ${request.url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  const [{ AddTransactionDialog }, { ToastContextProvider }, { Toaster }] = await Promise.all([
    import("./add-transaction-dialog"),
    import("@/components/ui/toast-provider"),
    import("@/components/ui/toaster"),
  ]);
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <ToastContextProvider>
          <Toaster />
          <AddTransactionDialog
            initialStatementId={opts.initialStatementId}
            categories={CATEGORIES}
            userId={opts.userId}
            onSwitchPerson={opts.onSwitchPerson}
            getViewAction={opts.getViewAction}
            onClose={onClose}
          />
        </ToastContextProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { fetchMock, onClose };
}

const posts = (fetchMock: ReturnType<typeof vi.fn>) =>
  (fetchMock.mock.calls as [Request][]).map(([r]) => r).filter((r) => r.method === "POST");

describe("outsideStatementPeriod", () => {
  it("allows a week of slack either side and flags anything further", () => {
    expect(outsideStatementPeriod("2026-01-25", "2026-02-01", "2026-02-28")).toBe(false);
    expect(outsideStatementPeriod("2026-01-24", "2026-02-01", "2026-02-28")).toBe(true);
    expect(outsideStatementPeriod("2026-03-07", "2026-02-01", "2026-02-28")).toBe(false);
    expect(outsideStatementPeriod("2026-03-08", "2026-02-01", "2026-02-28")).toBe(true);
    expect(outsideStatementPeriod("", "2026-02-01", "2026-02-28")).toBe(false);
    expect(outsideStatementPeriod("2020-01-01", null, null)).toBe(false);
  });
});

describe("AddTransactionDialog", () => {
  beforeEach(() => {
    vi.resetModules();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers only parsed statements, labelled name · account · period, and presets the one given", async () => {
    await mount({ initialStatementId: 2 });
    const select = await screen.findByLabelText("Statement");
    await waitFor(() => expect(select).toHaveValue("2"));
    const options = Array.from((select as HTMLSelectElement).options).map((o) => o.textContent);
    expect(options).toContain("jan.pdf · Chase · credit · 2026-01-01 to 2026-01-31");
    expect(options).toContain("feb.pdf · Chase · credit · 2026-02-01 to 2026-02-28");
    expect(options.join()).not.toContain("broken.pdf");
  });

  it("explains when there is no imported statement and links to Import", async () => {
    await mount({ statements: [] });
    expect(await screen.findByTestId("no-statements")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import a statement" })).toHaveAttribute("href", "/spending/import");
  });

  it("rejects an empty form, a zero amount, a bad date and a lone category without calling the API", async () => {
    const user = userEvent.setup();
    const { fetchMock } = await mount({ initialStatementId: 1 });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));

    expect(screen.getByLabelText("Date")).toHaveValue("2026-01-31"); // the statement's last day
    await user.clear(screen.getByLabelText("Date"));
    await user.click(screen.getByRole("button", { name: "Add transaction" }));
    expect(screen.getByText("Enter a valid date.")).toBeInTheDocument();
    expect(screen.getByText("Enter a description.")).toBeInTheDocument();
    expect(screen.getByText(/Enter an amount above 0/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Date"));

    await user.type(screen.getByLabelText("Date"), "2026-01-10");
    await user.type(screen.getByLabelText("Description"), "Coffee");
    await user.type(screen.getByLabelText(/Amount/), "0");
    await user.selectOptions(screen.getByLabelText("Category"), "groceries");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));
    expect(screen.getByText(/Enter an amount above 0/)).toBeInTheDocument();
    expect(screen.getByText(/Choose a subcategory too/)).toBeInTheDocument();
    expect(posts(fetchMock)).toHaveLength(0);
  });

  it("rejects a date outside 1970 to a year from now", async () => {
    const user = userEvent.setup();
    const { fetchMock } = await mount({ initialStatementId: 1 });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Date"), "1960-01-01");
    await user.type(screen.getByLabelText("Description"), "Old");
    await user.type(screen.getByLabelText(/Amount/), "5");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));
    expect(screen.getByText(/Enter a date between 1970/)).toBeInTheDocument();
    expect(posts(fetchMock)).toHaveLength(0);
  });

  it("warns (without blocking) when the date is outside the statement period", async () => {
    const user = userEvent.setup();
    await mount({ initialStatementId: 1 });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Date"), "2026-01-15");
    expect(screen.queryByTestId("period-warning")).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Date"), "2026-03-15");
    expect(await screen.findByTestId("period-warning")).toHaveTextContent("outside the statement");
  });

  it("sends money in as a negative amount with the typed fields, then closes", async () => {
    const user = userEvent.setup();
    const { fetchMock, onClose } = await mount({ initialStatementId: 1 });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Date"), "2026-01-10");
    await user.type(screen.getByLabelText("Description"), "  Refund at Big Mart  ");
    await user.click(screen.getByRole("radio", { name: "Money in" }));
    await user.type(screen.getByLabelText(/Amount/), "1,234.5");
    await user.selectOptions(screen.getByLabelText("Kind"), "refund");
    await user.selectOptions(screen.getByLabelText("Category"), "groceries");
    await user.selectOptions(screen.getByLabelText("Subcategory"), "supermarket");
    await user.type(screen.getByLabelText("Notes"), "from memory");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    const [request] = posts(fetchMock);
    expect(await request!.clone().json()).toEqual({
      statement_id: 1,
      posted_date: "2026-01-10",
      description: "Refund at Big Mart",
      amount_minor: -123450,
      kind: "refund",
      category_key: "groceries",
      subcategory_key: "supermarket",
      notes: "from memory",
    });
  });

  it("omits kind and category when left on Automatic", async () => {
    const user = userEvent.setup();
    const { fetchMock, onClose } = await mount({ initialStatementId: 1 });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Date"), "2026-01-10");
    await user.type(screen.getByLabelText("Description"), "Coffee");
    await user.type(screen.getByLabelText(/Amount/), "4.50");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(await posts(fetchMock)[0]!.clone().json()).toEqual({
      statement_id: 1,
      posted_date: "2026-01-10",
      description: "Coffee",
      amount_minor: 450,
    });
  });

  it("shows the server's sentence inline on a 409, stays open, and disables submit while pending", async () => {
    const user = userEvent.setup();
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { onClose } = await mount({
      initialStatementId: 1,
      post: async () => {
        await gate;
        return json({ detail: "this statement is not imported yet; finish importing it first" }, 409);
      },
    });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.clear(screen.getByLabelText("Date"));
    await user.type(screen.getByLabelText("Date"), "2026-01-10");
    await user.type(screen.getByLabelText("Description"), "Coffee");
    await user.type(screen.getByLabelText(/Amount/), "4.50");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));

    expect(await screen.findByRole("button", { name: "Adding…" })).toBeDisabled();
    release();
    expect(await screen.findByText("this statement is not imported yet; finish importing it first")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Add transaction" })).toBeEnabled();
  });

  it("says the row is waiting in Review when it needs review, even with a category set", async () => {
    const user = userEvent.setup();
    await mount({
      initialStatementId: 1,
      post: () => json({ id: 99, needs_review: true, category_key: "others", subcategory_key: "uncategorized" }, 201),
    });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.type(screen.getByLabelText("Description"), "Coffee");
    await user.type(screen.getByLabelText(/Amount/), "4.50");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));
    expect(await screen.findByText(/waiting in Review/)).toBeInTheDocument();
  });

  it("offers a View it action when the filters hide the new row", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    await mount({
      initialStatementId: 1,
      getViewAction: () => ({ label: "View it", onClick }),
    });
    await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveValue("1"));
    await user.type(screen.getByLabelText("Description"), "Coffee");
    await user.type(screen.getByLabelText(/Amount/), "4.50");
    await user.click(screen.getByRole("button", { name: "Add transaction" }));
    await user.click(await screen.findByRole("button", { name: "View it" }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("explains a preset statement that belongs to another person and offers to switch", async () => {
    const user = userEvent.setup();
    const onSwitchPerson = vi.fn();
    await mount({
      initialStatementId: 7,
      userId: 1,
      onSwitchPerson,
      statements: [statement({ id: 1 }), statement({ id: 7, user_id: 2, original_name: "sam.pdf" })],
    });
    expect(await screen.findByTestId("foreign-statement")).toHaveTextContent("sam.pdf belongs to Sam");
    await user.click(screen.getByRole("button", { name: "Switch to Sam" }));
    expect(onSwitchPerson).toHaveBeenCalledWith(2);
  });
});
