import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";
import { ToastContextProvider } from "@/components/ui/toast-provider";
import { Toaster } from "@/components/ui/toaster";

import ForecastScreen, { screenMeta } from "./index";

// The real chart is covered in forecast-chart.test.tsx; recharts warns on every jsdom re-render.
vi.mock("./forecast-chart", () => ({
  ForecastChart: ({ mode, rows }: { mode: string; rows: unknown[] }) => (
    <div data-testid="forecast-chart" data-mode={mode} data-points={rows.length} />
  ),
}));

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, apiClient: { GET: vi.fn(), POST: vi.fn(), PUT: vi.fn(), DELETE: vi.fn() } };
});

const ALEX = { id: 1, name: "Alex", is_default: true };
const SAM = { id: 2, name: "Sam", is_default: false };

const PARAMS = {
  horizon_years: null,
  start_net_worth_minor: null,
  annual_income_minor: null,
  income_growth_pct: 3,
  tax_rate_pct: null,
  annual_spending_minor: null,
  spending_growth_pct: 2.5,
  spending_change_minor: 0,
  return_pct: 6,
  inflation_pct: 2.5,
  retirement_year: null,
  retirement_income_minor: 0,
  events: [],
};
const builtin = (key: string, name: string, over: object) => ({ key, name, description: `${name} preset.`, params: { ...PARAMS, ...over } });
const PRESETS = {
  builtin: [
    builtin("conservative", "Conservative", { income_growth_pct: 2, return_pct: 4, spending_growth_pct: 3, inflation_pct: 3 }),
    builtin("moderate", "Moderate", {}),
    builtin("aggressive", "Aggressive", { income_growth_pct: 5, return_pct: 8, spending_growth_pct: 2 }),
  ],
  saved: [] as unknown[],
};

const row = (year: number, over: object = {}) => ({
  year,
  net_worth_minor: null,
  source: null,
  as_of: null,
  manual_net_worth_minor: null,
  manual_note: null,
  income_minor: null,
  tax_minor: null,
  income_basis: null,
  spending_minor: null,
  spending_months: 0,
  savings_minor: null,
  ...over,
});
const EMPTY_BASELINE = {
  currency: "USD",
  user_id: 1,
  history: [],
  defaults: {
    net_worth_minor: null,
    net_worth_year: null,
    annual_income_minor: null,
    income_year: null,
    effective_tax_rate_pct: null,
    annual_spending_minor: null,
    spending_year: null,
    spending_annualized: false,
    income_growth_pct: null,
    spending_growth_pct: null,
    savings_rate_pct: null,
  },
};
const BASELINE = {
  currency: "USD",
  user_id: 1,
  history: [
    row(2023, { net_worth_minor: 4_000_000, source: "entries", as_of: "2023-12", income_minor: 8_000_000, tax_minor: 1_600_000 }),
    row(2024, {
      net_worth_minor: 5_000_000,
      source: "entries",
      as_of: "2024-12",
      income_minor: 9_000_000,
      tax_minor: 2_025_000,
      spending_minor: 4_000_000,
      spending_months: 12,
      savings_minor: 2_975_000,
    }),
  ],
  defaults: {
    net_worth_minor: 5_000_000,
    net_worth_year: 2024,
    annual_income_minor: 9_000_000,
    income_year: 2024,
    effective_tax_rate_pct: 22.5,
    annual_spending_minor: 4_000_000,
    spending_year: 2024,
    spending_annualized: false,
    income_growth_pct: 12.5,
    spending_growth_pct: 3,
    savings_rate_pct: 33.06,
  },
};

type Responses = Record<string, unknown>;
function mockApi(responses: Responses) {
  const fallback: Responses = {
    "/users": [ALEX],
    "/accounts": [],
    "/net-worth/forecast/presets": PRESETS,
    "/net-worth/forecast/baseline": EMPTY_BASELINE,
  };
  (apiClient.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string) =>
    Promise.resolve({ data: { ...fallback, ...responses }[path] ?? [], error: undefined }),
  );
}
const calls = (method: "GET" | "POST" | "PUT" | "DELETE", path: string) =>
  (apiClient[method] as ReturnType<typeof vi.fn>).mock.calls.filter(([p]) => p === path);

function renderScreen(path = "/net-worth/forecast") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ToastContextProvider>
          <ForecastScreen />
          <Toaster />
        </ToastContextProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const field = (name: string | RegExp) => screen.getByRole<HTMLInputElement>("textbox", { name });
const spin = (name: string) => screen.getByRole<HTMLInputElement>("spinbutton", { name });

async function replace(input: HTMLElement, text: string) {
  await userEvent.clear(input);
  if (text) await userEvent.type(input, text);
}

beforeEach(() => {
  window.localStorage.clear();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-06-15T12:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe("ForecastScreen", () => {
  it("exposes its route metadata", () => {
    expect(screenMeta).toEqual({ path: "/net-worth/forecast", title: "Net worth forecast" });
  });

  it("states that the projection is illustrative", async () => {
    mockApi({});
    renderScreen();
    expect(await screen.findByRole("note")).toHaveTextContent(/illustrative projection, not financial/i);
  });

  it("asks for a starting net worth when there is no history, then projects from the typed value", async () => {
    mockApi({});
    renderScreen();
    expect(await screen.findByText("Enter a starting net worth")).toBeInTheDocument();
    expect(screen.queryByTestId("forecast-summary")).not.toBeInTheDocument();
    // History is empty too, but a past year can be added by hand.
    expect(screen.getByText("No history yet")).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Add a past year" })).toBeInTheDocument();

    // No income or spending: only the return acts on the balance. Zero it for an exact figure.
    await userEvent.type(field("Starting net worth"), "100000");
    await replace(spin("Investment return per year"), "0");
    expect(await screen.findByTestId("forecast-summary")).toBeInTheDocument();
    // Base year is the current year (2026) and the target is ten years later.
    expect(screen.getByText("Net worth in 2036")).toBeInTheDocument();
    expect(screen.getByTestId("forecast-nominal")).toHaveTextContent("$100,000.00");
    expect(screen.queryByText("Enter a starting net worth")).not.toBeInTheDocument();
  });

  it("projects from the history-derived defaults and shows them beside each input", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    expect(screen.getByText("Data: $50,000 (2024)")).toBeInTheDocument();
    expect(screen.getByText("Data: $90,000 (2024, gross, from tax forms)")).toBeInTheDocument();
    expect(screen.getByText("Data: 22.5% (all-in tax / gross income)")).toBeInTheDocument();
    expect(screen.getByText("Data: $40,000 (2024)")).toBeInTheDocument();
    expect(screen.getByText("Observed: 12.5%/yr")).toBeInTheDocument();
    // 2036: the later of base (2024) and the current year (2026), plus ten.
    expect(screen.getByLabelText("Forecast to year")).toHaveValue(2036);

    // Zero every rate: 2 years x (90,000 - 22.5% - 40,000 = 29,750) on top of 50,000.
    for (const name of ["Income growth per year", "Spending growth per year", "Investment return per year"]) {
      await replace(spin(name), "0");
    }
    await replace(screen.getByLabelText("Forecast to year"), "2026");
    expect(screen.getByTestId("forecast-nominal")).toHaveTextContent("$109,500.00");
    expect(screen.getByTestId("forecast-contributions")).toHaveTextContent("$59,500.00");
    expect(screen.getByTestId("forecast-growth")).toHaveTextContent("$0.00");
    expect(within(screen.getByTestId("forecast-table")).getAllByRole("row")).toHaveLength(3); // header + 2025, 2026
  });

  it("overrides a default and puts the data value back with 'Reset to data'", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    const income = field("Annual gross income");
    expect(income).toHaveValue("");
    expect(income).toHaveAttribute("placeholder", "90000.00");
    expect(screen.queryByRole("button", { name: "Reset Annual gross income to data" })).not.toBeInTheDocument();

    await userEvent.type(income, "120000");
    await userEvent.click(screen.getByRole("button", { name: "Reset Annual gross income to data" }));
    expect(income).toHaveValue("");

    // A rate resets to the observed one, and only offers the reset while it differs.
    expect(screen.getByRole("button", { name: "Reset Income growth per year to data" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset Income growth per year to data" }));
    expect(spin("Income growth per year")).toHaveValue(12.5);
    expect(screen.queryByRole("button", { name: "Reset Income growth per year to data" })).not.toBeInTheDocument();
  });

  it("recomputes as a slider moves", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    const before = screen.getByTestId("forecast-nominal").textContent;
    const slider = screen.getByRole<HTMLInputElement>("slider", { name: "Investment return per year slider" });
    expect(slider).toHaveValue("6");
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(slider, { target: { value: "10" } });
    expect(spin("Investment return per year")).toHaveValue(10);
    expect(screen.getByTestId("forecast-nominal").textContent).not.toBe(before);
  });

  it("rejects an out-of-range value without applying it", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    const input = spin("Investment return per year");
    await replace(input, "45");
    expect(await screen.findByText("Enter a number from -20 to 30.")).toBeInTheDocument();
    expect(input).toHaveAttribute("aria-invalid", "true");
    // Typing "45" went through "4" first, which is valid and applied; the invalid "45" was not.
    expect(screen.getByRole("slider", { name: "Investment return per year slider" })).toHaveValue("4");
  });

  it("switches to today's dollars", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    const nominal = screen.getByTestId("forecast-nominal").textContent;
    const realTile = screen.getByTestId("forecast-real").textContent;
    expect(realTile).not.toBe(nominal);
    expect(screen.getByRole("radio", { name: "Nominal" })).toHaveAttribute("aria-checked", "true");
    await userEvent.click(screen.getByRole("radio", { name: "Today's dollars" }));
    expect(screen.getByRole("radio", { name: "Today's dollars" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("forecast-chart")).toHaveAttribute("data-mode", "real");
    expect(screen.getByTestId("forecast-table").querySelector("caption")).toHaveTextContent("today's dollars");
    // The summary tiles always show both.
    expect(screen.getByTestId("forecast-nominal").textContent).toBe(nominal);
  });

  it("reports when a goal is reached", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    expect(screen.getByTestId("forecast-goal-result")).toHaveTextContent("Enter an amount");
    await replace(field(/Goal/), "60000");
    expect(screen.getByTestId("forecast-goal-result")).toHaveTextContent(/Reached in 2025 \(1 year from 2024\)/);
    await replace(field(/Goal/), "999999999");
    expect(screen.getByTestId("forecast-goal-result")).toHaveTextContent("Not reached by 2036.");
  });

  it("adds, edits and removes a one-off event", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    expect(screen.getByText("No events.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add event" }));
    await replace(spin("Event 1 year"), "2030");
    await replace(field("Event 1 amount"), "-100000");
    await userEvent.type(screen.getByRole("textbox", { name: "Event 1 label" }), "House");
    const table = screen.getByTestId("forecast-table");
    const rowFor2030 = within(table).getByRole("rowheader", { name: "2030" }).closest("tr")!;
    expect(rowFor2030).toHaveTextContent("-$100,000");
    await userEvent.click(screen.getByRole("button", { name: "Remove event 1" }));
    expect(screen.getByText("No events.")).toBeInTheDocument();
    expect(within(table).getByRole("rowheader", { name: "2030" }).closest("tr")).not.toHaveTextContent("-$100,000");
  });

  it("applies a built-in preset (rates only) and marks edits as modified", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    const preset = screen.getByRole("combobox", { name: "Preset" });
    expect(preset).toHaveValue("builtin:moderate");
    expect(screen.queryByText(/Modified from/)).not.toBeInTheDocument();

    await userEvent.type(field("Annual gross income"), "120000");
    await userEvent.selectOptions(preset, "builtin:aggressive");
    expect(spin("Investment return per year")).toHaveValue(8);
    expect(spin("Income growth per year")).toHaveValue(5);
    expect(spin("Spending growth per year")).toHaveValue(2);
    // The scenario's own values survive a preset change.
    expect(field("Annual gross income")).toHaveValue("120000.00");
    expect(screen.queryByText(/Modified from/)).not.toBeInTheDocument();

    await replace(spin("Investment return per year"), "7");
    expect(screen.getByText("Modified from Aggressive")).toBeInTheDocument();
  });

  it("applies a saved preset in full", async () => {
    const saved = {
      id: 7,
      user_id: 1,
      name: "FIRE",
      params: { ...PARAMS, horizon_years: 20, return_pct: 7, retirement_year: 2045, annual_income_minor: 15_000_000 },
      created_at: "",
      updated_at: "",
    };
    mockApi({ "/net-worth/forecast/baseline": BASELINE, "/net-worth/forecast/presets": { ...PRESETS, saved: [saved] } });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Preset" }), "saved:7");
    expect(spin("Investment return per year")).toHaveValue(7);
    expect(spin("Last working year")).toHaveValue(2045);
    expect(field("Annual gross income")).toHaveValue("150000.00");
    expect(spin("Forecast to year")).toHaveValue(2044);
    expect(screen.queryByText(/Modified from/)).not.toBeInTheDocument();
  });

  it("saves the current scenario as a preset for the selected user", async () => {
    mockApi({ "/net-worth/forecast/baseline": BASELINE });
    (apiClient.POST as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: { id: 9, user_id: 1, name: "My plan", params: PARAMS, created_at: "", updated_at: "" },
      error: undefined,
    });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    await replace(spin("Investment return per year"), "5.5");
    await userEvent.click(screen.getByRole("button", { name: "Save as preset…" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Preset name" }), "My plan");
    await userEvent.click(screen.getByRole("checkbox", { name: "Share with everyone" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Share with everyone" }));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls("POST", "/net-worth/forecast/presets")).toHaveLength(1));
    const [, init] = calls("POST", "/net-worth/forecast/presets")[0] as [string, { body: unknown }];
    expect(init.body).toMatchObject({
      name: "My plan",
      user_id: 1,
      params: { horizon_years: 12, return_pct: 5.5, start_net_worth_minor: null, income_growth_pct: 3 },
    });
    expect(await screen.findByText("Preset saved")).toBeInTheDocument();
  });

  it("shows an error state with retry when the history cannot be loaded", async () => {
    (apiClient.GET as ReturnType<typeof vi.fn>).mockImplementation((path: string) =>
      Promise.resolve(
        path === "/net-worth/forecast/baseline"
          ? { data: undefined, error: { detail: "boom" } }
          : { data: path === "/users" ? [ALEX] : [], error: undefined },
      ),
    );
    renderScreen();
    expect(await screen.findByText("Could not load your history")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("views everyone combined without manual editing, and saves presets as shared", async () => {
    mockApi({ "/users": [ALEX, SAM], "/net-worth/forecast/baseline": { ...BASELINE, user_id: null } });
    renderScreen();
    await screen.findByTestId("forecast-summary");
    expect(screen.getAllByText(/Showing everyone combined/).length).toBeGreaterThan(0);
    expect(screen.getByText(/Pick a person in the user bar/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Add a past year" })).not.toBeInTheDocument();
    expect(calls("GET", "/net-worth/forecast/baseline")[0]![1]).toEqual({ params: { query: { user_id: undefined } } });
    await userEvent.click(screen.getByRole("button", { name: "Save as preset…" }));
    expect(screen.getByText("Saved for all users (no one is selected).")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Share with everyone" })).not.toBeInTheDocument();
  });
});
