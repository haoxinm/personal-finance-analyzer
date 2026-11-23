import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const STATEMENT = {
  id: 1,
  user_id: 1,
  account_id: 5,
  issuer_id: 3,
  status: "parsed",
  period_start: "2026-03-01",
  period_end: "2026-03-31",
  period_corrected: false,
  parsed_period_start: "2026-03-01",
  parsed_period_end: "2026-03-31",
  original_name: "chase-march.pdf",
  txn_count: 10,
};
const ACCOUNTS = [
  { id: 5, user_id: 1, issuer_id: 3, account_type: "credit", currency: "USD" },
  { id: 6, user_id: 1, issuer_id: 4, account_type: "checking", currency: "USD" },
  { id: 7, user_id: 2, issuer_id: 3, account_type: "credit", currency: "USD" },
];
const ISSUERS = [
  { id: 3, name: "Chase", slug: "chase", match_terms: [] },
  { id: 4, name: "Citi", slug: "citi", match_terms: [] },
  { id: 8, name: "Wells", slug: "wells", match_terms: [] },
];

interface Answer {
  status: number;
  body: unknown;
}

async function renderPanel(
  statement: Record<string, unknown> = {},
  answers: { patchStatement?: Answer; patchAccount?: Answer; count?: number } = {},
) {
  const calls: { url: string; method: string; body: unknown }[] = [];
  const fn = vi.fn<typeof fetch>(async (input) => {
    const req = input as Request;
    const text = await req.clone().text();
    calls.push({ url: req.url, method: req.method, body: text ? (JSON.parse(text) as unknown) : undefined });
    if (req.method === "PATCH" && req.url.includes("/api/accounts/")) {
      const a = answers.patchAccount ?? { status: 200, body: { ...ACCOUNTS[0], currency: "EUR" } };
      return jsonResponse(a.body, a.status);
    }
    if (req.method === "PATCH") {
      const a = answers.patchStatement ?? { status: 200, body: { ...STATEMENT, rows_moved: 0, duplicates_deleted: 0 } };
      return jsonResponse(a.body, a.status);
    }
    return jsonResponse({});
  });
  vi.resetModules();
  vi.stubGlobal("fetch", fn);
  const { StatementDetailsPanel } = await import("./statement-details-panel");
  const { Toaster } = await import("@/components/ui/toaster");
  const { ToastContextProvider } = await import("@/components/ui/toast-provider");
  const onClose = vi.fn();
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <ToastContextProvider>
        <MemoryRouter>
          <StatementDetailsPanel
            statement={{ ...STATEMENT, ...statement } as never}
            accounts={ACCOUNTS as never}
            issuers={ISSUERS as never}
            accountStatementCount={answers.count ?? 1}
            onClose={onClose}
          />
        </MemoryRouter>
        <Toaster />
      </ToastContextProvider>
    </QueryClientProvider>,
  );
  return { calls, onClose };
}

const patches = (calls: { url: string; method: string; body: unknown }[]) => calls.filter((c) => c.method === "PATCH");

describe("StatementDetailsPanel", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("saves a corrected period and explains that row dates do not change", async () => {
    const { calls } = await renderPanel();
    const period = screen.getByRole("region", { name: "Period" });
    expect(within(period).getByText(/does not change transaction dates/)).toBeInTheDocument();
    const [start] = within(period).getAllByDisplayValue(/2026-03/) ;
    const save = within(period).getByRole("button", { name: "Save period" });
    expect(save).toBeDisabled();
    await userEvent.clear(start!);
    await userEvent.type(start!, "2026-02-20");
    await userEvent.click(save);
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patches(calls)[0]!.body).toEqual({ period_start: "2026-02-20", period_end: "2026-03-31" });
    expect(await screen.findByText("Period updated")).toBeInTheDocument();
  });

  it("blocks an inverted or over-long period locally", async () => {
    const { calls } = await renderPanel();
    const period = screen.getByRole("region", { name: "Period" });
    const [start] = within(period).getAllByDisplayValue(/2026-03/) ;
    await userEvent.clear(start!);
    await userEvent.type(start!, "2026-04-15");
    expect(within(period).getByRole("alert")).toHaveTextContent("must not be after");
    expect(within(period).getByRole("button", { name: "Save period" })).toBeDisabled();
    await userEvent.clear(start!);
    await userEvent.type(start!, "2024-01-01");
    expect(within(period).getByRole("alert")).toHaveTextContent("at most 400 days");
    expect(patches(calls)).toHaveLength(0);
  });

  it("shows the server's validation message inline when the period is refused", async () => {
    const { calls } = await renderPanel(
      {},
      { patchStatement: { status: 400, body: { detail: "period must be between 1970-01-01 and a year from today" } } },
    );
    const period = screen.getByRole("region", { name: "Period" });
    const [, end] = within(period).getAllByDisplayValue(/2026-03/) ;
    await userEvent.clear(end!);
    await userEvent.type(end!, "2026-04-02");
    await userEvent.click(within(period).getByRole("button", { name: "Save period" }));
    expect(await within(period).findByRole("alert")).toHaveTextContent("period must be between 1970-01-01");
    expect(patches(calls)).toHaveLength(1);
  });

  it("offers to restore the detected period only when it was corrected, and sends revert_period", async () => {
    const { calls } = await renderPanel({
      period_corrected: true,
      period_start: "2026-02-20",
      parsed_period_start: "2026-03-01",
    });
    const period = screen.getByRole("region", { name: "Period" });
    expect(within(period).queryByText("Corrected")).not.toBeInTheDocument();
    expect(within(period).getByText(/Detected from the statement/)).toBeInTheDocument();
    await userEvent.click(within(period).getByRole("button", { name: "Restore detected period" }));
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patches(calls)[0]!.body).toEqual({ revert_period: true });
    expect(await screen.findByText("Detected period restored")).toBeInTheDocument();
  });

  it("does not offer Restore for an uncorrected period", async () => {
    await renderPanel();
    expect(screen.queryByRole("button", { name: "Restore detected period" })).not.toBeInTheDocument();
  });

  it("changes the account's currency directly for a single statement", async () => {
    const { calls } = await renderPanel();
    const section = screen.getByRole("region", { name: "Currency" });
    expect(section).toHaveTextContent("Currency of account Chase credit #5 (1 statement)");
    const input = within(section).getByRole("textbox");
    await userEvent.clear(input);
    await userEvent.type(input, "eur");
    await userEvent.click(within(section).getByRole("button", { name: "Change currency" }));
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patches(calls)[0]!.url).toContain("/api/accounts/5");
    expect(patches(calls)[0]!.body).toEqual({ currency: "EUR" });
  });

  it("asks for confirmation before relabeling an account with several statements", async () => {
    const { calls } = await renderPanel({}, { count: 3 });
    const section = screen.getByRole("region", { name: "Currency" });
    const input = within(section).getByRole("textbox");
    await userEvent.clear(input);
    await userEvent.type(input, "EUR");
    await userEvent.click(within(section).getByRole("button", { name: "Change currency" }));
    expect(patches(calls)).toHaveLength(0);
    expect(within(section).getByRole("status")).toHaveTextContent("This account has 3 statements");
    await userEvent.click(within(section).getByRole("button", { name: "Yes, change 3 statements" }));
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
  });

  it("rejects a malformed currency code and surfaces a server refusal", async () => {
    await renderPanel({}, { patchAccount: { status: 400, body: { detail: "currency must be a 3-letter code" } } });
    const section = screen.getByRole("region", { name: "Currency" });
    const input = within(section).getByRole("textbox");
    await userEvent.clear(input);
    await userEvent.type(input, "E1");
    expect(within(section).getByRole("alert")).toHaveTextContent("three-letter code");
    expect(within(section).getByRole("button", { name: "Change currency" })).toBeDisabled();
    await userEvent.clear(input);
    await userEvent.type(input, "EUR");
    await userEvent.click(within(section).getByRole("button", { name: "Change currency" }));
    expect(await within(section).findByRole("alert")).toHaveTextContent("currency must be a 3-letter code");
  });

  it("moves to another bank and reports rows moved and duplicates merged", async () => {
    const { calls } = await renderPanel(
      {},
      { patchStatement: { status: 200, body: { ...STATEMENT, issuer_id: 4, rows_moved: 10, duplicates_deleted: 2 } } },
    );
    const bank = screen.getByRole("region", { name: "Bank" });
    // The current bank is not offered.
    expect(within(bank).queryByRole("option", { name: "Chase" })).not.toBeInTheDocument();
    // Nothing is pre-picked: the button waits for a choice.
    expect(within(bank).getByRole("button", { name: "Change bank" })).toBeDisabled();
    await userEvent.selectOptions(within(bank).getByLabelText("Bank"), "Citi");
    await userEvent.click(within(bank).getByRole("button", { name: "Change bank" }));
    const confirm = within(bank).getByRole("alertdialog", { name: /Confirm move to Citi/ });
    expect(confirm).toHaveTextContent(/merged/);
    expect(confirm).toHaveTextContent(/will not restore/);
    expect(patches(calls)).toHaveLength(0);
    await userEvent.click(within(confirm).getByRole("button", { name: "Yes, move it" }));
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patches(calls)[0]!.body).toEqual({ issuer_id: 4 });
    expect(await screen.findByText("Moved 10 rows, merged 2 duplicates")).toBeInTheDocument();
    expect(within(bank).getByRole("link", { name: /Rules/ })).toHaveAttribute("href", "/spending/rules");
  });

  it("lists only the owner's other accounts for 'Move to an existing account' and sends account_id", async () => {
    const { calls } = await renderPanel();
    const bank = screen.getByRole("region", { name: "Bank" });
    const select = within(bank).getByLabelText("Move to an existing account");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Citi checking (USD)"]);
    await userEvent.click(within(bank).getByRole("button", { name: "Move statement" }));
    await userEvent.click(within(bank).getByRole("button", { name: "Yes, move it" }));
    await waitFor(() => expect(patches(calls)).toHaveLength(1));
    expect(patches(calls)[0]!.body).toEqual({ account_id: 6 });
  });

  it("shows a 409 (statement not parsed / same bank) in the bank section", async () => {
    await renderPanel({}, { patchStatement: { status: 409, body: { detail: "only imported statements can be changed" } } });
    const bank = screen.getByRole("region", { name: "Bank" });
    await userEvent.selectOptions(within(bank).getByLabelText("Bank"), "Citi");
    await userEvent.click(within(bank).getByRole("button", { name: "Change bank" }));
    await userEvent.click(within(bank).getByRole("button", { name: "Yes, move it" }));
    expect(await within(bank).findByRole("alert")).toHaveTextContent("only imported statements can be changed");
  });

  it("closes on Done", async () => {
    const { onClose } = await renderPanel();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
