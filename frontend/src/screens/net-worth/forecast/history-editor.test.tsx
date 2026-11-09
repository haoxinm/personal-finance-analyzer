import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import { Wrapper } from "../test-utils";
import { HistoryEditor } from "./history-editor";
import type { HistoryRow } from "./params";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn(), PUT: vi.fn(), DELETE: vi.fn() } };
});

const row = (year: number, over: Partial<HistoryRow> = {}): HistoryRow => ({
  year,
  net_worth_minor: null,
  source: null,
  as_of: null,
  manual_net_worth_minor: null,
  manual_note: null,
  income_minor: null,
  earned_income_minor: null,
  tax_minor: null,
  income_basis: null,
  spending_minor: null,
  spending_months: 0,
  savings_minor: null,
  ...over,
});

const HISTORY: HistoryRow[] = [
  row(2024, { net_worth_minor: 5_000_000, source: "entries", as_of: "2024-12", manual_net_worth_minor: 1_000, income_minor: 9_000_000, tax_minor: 2_000_000, spending_minor: 900_000, spending_months: 3 }),
  row(2022, { net_worth_minor: 2_000_000, source: "manual", manual_net_worth_minor: 2_000_000, manual_note: "from memory" }),
  row(2021, { income_minor: 7_000_000 }),
];

const bodyOf = (call: unknown[] | undefined) => (call?.[1] as { body: unknown }).body;
const put = () => apiClient.PUT as ReturnType<typeof vi.fn>;
const del = () => apiClient.DELETE as ReturnType<typeof vi.fn>;

function renderEditor(userId: number | null, history = HISTORY) {
  put().mockResolvedValue({ data: {}, error: undefined });
  del().mockResolvedValue({ error: undefined });
  return render(
    <Wrapper>
      <HistoryEditor userId={userId} history={history} />
    </Wrapper>,
  );
}

afterEach(() => vi.resetAllMocks());

describe("HistoryEditor", () => {
  it("lists years newest first with their source, and explains that entries take precedence", () => {
    renderEditor(1);
    const rows = within(screen.getByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getByRole("rowheader").textContent)).toEqual(["2024", "2022", "2021"]);
    expect(within(rows[0]!).getByText("Balance entries")).toBeInTheDocument();
    expect(within(rows[0]!).getByText(/Balance entries take precedence \(manual \$10 ignored\)/)).toBeInTheDocument();
    expect(within(rows[0]!).getByText("3 mo")).toBeInTheDocument(); // partial-year spending is flagged
    expect(within(rows[1]!).getByText("Manual")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Manual net worth for 2024" })).not.toBeInTheDocument();
  });

  it("saves, replaces and clears a manual value for a year without entries", async () => {
    renderEditor(1);
    const input2021 = screen.getByRole("textbox", { name: "Manual net worth for 2021" });
    expect(screen.getByRole("button", { name: "Save 2021" })).toBeDisabled();
    await userEvent.type(input2021, "-5,000");
    await userEvent.click(screen.getByRole("button", { name: "Save 2021" }));
    await waitFor(() => expect(put()).toHaveBeenCalledTimes(1));
    expect(put()).toHaveBeenCalledWith("/net-worth/history/{user_id}/{year}", {
      params: { path: { user_id: 1, year: 2021 } },
      body: { amount_minor: -500_000, note: null },
    });

    // An existing manual value is prefilled; saving keeps its note, and it can be cleared.
    const input2022 = screen.getByRole("textbox", { name: "Manual net worth for 2022" });
    expect(input2022).toHaveValue("20000.00");
    await userEvent.clear(input2022);
    await userEvent.type(input2022, "25000");
    await userEvent.click(screen.getByRole("button", { name: "Save 2022" }));
    await waitFor(() => expect(put()).toHaveBeenCalledTimes(2));
    expect(bodyOf(put().mock.calls[1] as unknown[])).toEqual({ amount_minor: 2_500_000, note: "from memory" });

    await userEvent.click(screen.getByRole("button", { name: "Clear 2022" }));
    await waitFor(() => expect(del()).toHaveBeenCalledWith("/net-worth/history/{user_id}/{year}", { params: { path: { user_id: 1, year: 2022 } } }));
  });

  it("adds a past year that is not in the table, validating the year and amount", async () => {
    renderEditor(2, []);
    expect(screen.getByText("No history yet")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Add a past year" });
    await userEvent.click(within(form).getByRole("button", { name: "Add past year" }));
    expect(await within(form).findByText(/Enter the net worth at the end of that year/)).toBeInTheDocument();
    expect(put()).not.toHaveBeenCalled();

    const year = within(form).getByRole("spinbutton", { name: "Year" });
    await userEvent.clear(year);
    await userEvent.type(year, "2200");
    await userEvent.click(within(form).getByRole("button", { name: "Add past year" }));
    expect(await within(form).findByText(/Enter a year from 1900 to/)).toBeInTheDocument();

    await userEvent.clear(year);
    await userEvent.type(year, "2015");
    await userEvent.type(within(form).getByRole("textbox", { name: "Year-end net worth" }), "-1200.50");
    await userEvent.type(within(form).getByRole("textbox", { name: "Note (optional)" }), " student loan ");
    await userEvent.click(within(form).getByRole("button", { name: "Add past year" }));
    await waitFor(() => expect(put()).toHaveBeenCalledTimes(1));
    expect(put()).toHaveBeenCalledWith("/net-worth/history/{user_id}/{year}", {
      params: { path: { user_id: 2, year: 2015 } },
      body: { amount_minor: -120_050, note: "student loan" },
    });
  });

  it("is read-only for everyone combined", () => {
    renderEditor(null);
    expect(screen.getByText(/Pick a person in the user bar/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add a past year" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.getByRole("table")).toBeInTheDocument();
  });
});
