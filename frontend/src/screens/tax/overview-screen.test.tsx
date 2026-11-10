import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import TaxOverviewScreen, { screenMeta } from "./overview-screen";
import { USERS, summary } from "./test-fixtures";
import { FULL_RESPONSES as FULL, ResizeObserverStub, makeWrapper } from "./test-utils";

vi.stubGlobal("ResizeObserver", ResizeObserverStub);

vi.mock("@/api/client", async () => {
  const actual =
    await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    apiClient: { GET: vi.fn(), POST: vi.fn(), DELETE: vi.fn() },
  };
});

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>;

function mockGet(responses: Record<string, unknown>) {
  get.mockImplementation((path: string) =>
    Promise.resolve({ data: responses[path] ?? [], error: undefined }),
  );
}

function renderScreen(entry = "/tax") {
  return render(<TaxOverviewScreen />, { wrapper: makeWrapper(entry) });
}

beforeEach(() => {
  window.localStorage.clear();
  get.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe("TaxOverviewScreen", () => {
  it("exposes its route metadata", () => {
    expect(screenMeta).toEqual({ path: "/tax", title: "Overview" });
  });

  it("shows the empty state with a link to the tax import", async () => {
    mockGet({ "/users": [USERS[0]] });
    renderScreen();
    expect(
      await screen.findByText(
        "Import a W-2, 1099 or 1040 to see income and tax",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Import tax forms" }),
    ).toHaveAttribute("href", "/tax/import");
    // Uploading and documents live on their own tabs now.
    expect(screen.queryByLabelText("Choose tax form PDFs")).not.toBeInTheDocument();
    expect(screen.queryByTestId("tax-documents")).not.toBeInTheDocument();
  });

  it("retries after a load error", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(
        path === "/tax/summary"
          ? { data: undefined, error: { detail: "boom" } }
          : { data: path === "/users" ? [USERS[0]] : [2024], error: undefined },
      ),
    );
    renderScreen();
    await userEvent.click(
      await screen.findByRole("button", { name: "Try again" }),
    );
    await waitFor(() =>
      expect(
        get.mock.calls.filter(([p]) => p === "/tax/summary").length,
      ).toBeGreaterThan(1),
    );
  });

  it("renders summary cards, breakdowns, payers and the year-over-year chart", async () => {
    mockGet(FULL);
    renderScreen();
    const cards = await screen.findByTestId("tax-summary-cards");
    expect(within(cards).getByText("$112,882.60")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("effective-rate-card")).getByText("13.19%"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("all-in-rate-card")).getByText("25.27%"),
    ).toBeInTheDocument();
    // The all-in card names the same federal basis as the Total tax card next to it.
    expect(screen.getByTestId("all-in-rate-card")).toHaveTextContent(
      "Total tax ÷ income (federal from return)",
    );
    expect(screen.getByTestId("federal-basis")).toHaveTextContent("from return");
    expect(screen.getByTestId("basis-banner")).toHaveTextContent(
      /differ from the 1040/,
    );
    // Memo lines are shown but the total stays at the non-memo sum.
    expect(screen.getByTestId("income-memo")).toHaveTextContent(
      /Qualified dividends/,
    );
    expect(screen.getAllByText("estimate")).toHaveLength(2);
    expect(screen.getByText(/not reported/)).toBeInTheDocument(); // NIIT
    expect(
      within(screen.getByTestId("income-by-payer")).getByText(
        "Pinecrest Logistics LLC",
      ),
    ).toBeInTheDocument();
    expect(screen.getByTestId("year-over-year")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "2023" })).toBeInTheDocument();
  });

  it("puts year-over-year right after the summary cards", async () => {
    mockGet(FULL);
    renderScreen();
    const cards = await screen.findByTestId("tax-summary-cards");
    const yoy = screen.getByTestId("year-over-year");
    const payers = screen.getByTestId("income-by-payer");
    expect(
      cards.compareDocumentPosition(yoy) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      yoy.compareDocumentPosition(payers) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("hides year-over-year with a single year and shows no banner for the return basis", async () => {
    mockGet({
      ...FULL,
      "/tax/years": [2024],
      "/tax/summary": [summary({ income_basis: "return", warnings: [] })],
    });
    renderScreen();
    await screen.findByTestId("tax-summary-cards");
    expect(screen.queryByTestId("year-over-year")).not.toBeInTheDocument();
    expect(screen.queryByTestId("basis-banner")).not.toBeInTheDocument();
  });

  it("shows the mixed basis banner", async () => {
    mockGet({
      ...FULL,
      "/tax/summary": [summary({ income_basis: "mixed", warnings: [] })],
    });
    renderScreen();
    await screen.findByTestId("tax-summary-cards");
    expect(screen.getByTestId("basis-banner")).toHaveTextContent(/Form 1040/);
  });

  it.each([
    ["return", "from return"],
    ["estimate", "estimated"],
    ["withholding", "withholding only"],
    ["mixed", "mixed"],
  ] as const)("labels a %s federal basis as %s", async (basis, label) => {
    mockGet({
      ...FULL,
      "/tax/years": [2024],
      "/tax/summary": [
        summary({
          headline: {
            federal_minor: 1000,
            federal_basis: basis,
            fica_minor: 200,
            state_minor: 300,
            total_minor: 1500,
          },
        }),
      ],
    });
    renderScreen();
    const card = await screen.findByTestId("total-tax-card");
    expect(within(card).getByText("$15.00")).toBeInTheDocument();
    expect(within(card).getByTestId("federal-basis")).toHaveTextContent(`(${label})`);
  });

  it("shows a dash instead of tax when the server has no headline", async () => {
    mockGet({
      ...FULL,
      "/tax/years": [2024],
      "/tax/summary": [summary({ headline: null, federal_effective_rate: null })],
    });
    renderScreen();
    const card = await screen.findByTestId("total-tax-card");
    expect(card).toHaveTextContent("—");
    expect(within(card).queryByTestId("federal-basis")).not.toBeInTheDocument();
  });

  it("explains the effective rate as return-based, not withholding-based, and links to the estimate", async () => {
    mockGet({
      ...FULL,
      "/tax/summary": [summary({ federal_effective_rate: null, refund_minor: null })],
    });
    renderScreen("/tax?year=2024");
    const card = await screen.findByTestId("effective-rate-card");
    expect(card).toHaveAttribute("title", expect.stringMatching(/Form 1040/));
    expect(card.getAttribute("title")).not.toMatch(/withhold/i);
    expect(card).toHaveTextContent("Needs a Form 1040");
    const links = screen.getAllByRole("link", { name: "See estimate →" });
    expect(links.length).toBeGreaterThanOrEqual(2);
    for (const link of links) expect(link).toHaveAttribute("href", "/tax/estimate?year=2024");
  });

  it("marks years whose tax is not from a return in the year-over-year chart", async () => {
    mockGet({
      ...FULL,
      "/tax/summary": [
        summary(),
        summary({
          tax_year: 2023,
          headline: {
            federal_minor: 1000,
            federal_basis: "withholding",
            fica_minor: 0,
            state_minor: 0,
            total_minor: 1000,
          },
        }),
      ],
    });
    renderScreen();
    await screen.findByTestId("tax-summary-cards");
    expect(screen.getByTestId("year-over-year")).toHaveAttribute(
      "aria-label",
      expect.stringContaining("2023 income"),
    );
    expect(screen.getByTestId("year-over-year").getAttribute("aria-label")).toContain(
      "(withholding only)",
    );
    expect(screen.getByTestId("year-over-year-note")).toHaveTextContent(/only what was withheld/);
  });

  it("has no year-over-year note when every year is from a return", async () => {
    mockGet(FULL);
    renderScreen();
    await screen.findByTestId("tax-summary-cards");
    expect(screen.queryByTestId("year-over-year-note")).not.toBeInTheDocument();
  });

  it("switches year and keeps it in the URL", async () => {
    mockGet(FULL);
    renderScreen();
    await screen.findByTestId("tax-summary-cards");
    await userEvent.click(screen.getByRole("radio", { name: "2023" }));
    expect(
      within(screen.getByTestId("tax-summary-cards")).getByText("$90,000.00"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("year=2023");
  });

  it("reads the year from ?year= and falls back to the newest for an unknown one", async () => {
    mockGet(FULL);
    const { unmount } = renderScreen("/tax?year=2023");
    expect(
      within(await screen.findByTestId("tax-summary-cards")).getByText(
        "$90,000.00",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "2023" })).toBeChecked();
    unmount();

    renderScreen("/tax?year=1999");
    expect(
      within(await screen.findByTestId("tax-summary-cards")).getByText(
        "$112,882.60",
      ),
    ).toBeInTheDocument();
  });

  it("never renders SSN-shaped text", async () => {
    mockGet(FULL);
    const { container } = renderScreen();
    await screen.findByTestId("tax-summary-cards");
    expect(container.textContent).not.toMatch(/\d{3}-\d{2}-\d{4}/);
  });

  it("filters by the selected user chip", async () => {
    mockGet(FULL);
    renderScreen();
    await screen.findByTestId("tax-summary-cards");
    await userEvent.click(screen.getByRole("radio", { name: "Sam" }));
    await waitFor(() =>
      expect(get).toHaveBeenCalledWith(
        "/tax/summary",
        expect.objectContaining({
          params: { query: { user_ids: [2], currency: "USD" } },
        }),
      ),
    );
  });
});
