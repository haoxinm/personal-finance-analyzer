import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/api/client";

import TaxEstimateScreen, { screenMeta } from "./estimate-screen";
import {
  REFUND_CALC,
  estimate,
  estimateCalc,
  householdEstimate,
  taxProfile,
} from "./estimate/test-fixtures";
import type { TaxEstimate, TaxProfile } from "./estimate/logic";
import { USERS } from "./test-fixtures";
import { makeWrapper } from "./test-utils";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return {
    ...actual,
    apiClient: { GET: vi.fn(), POST: vi.fn(), PUT: vi.fn(), DELETE: vi.fn() },
  };
});

const get = apiClient.GET as unknown as ReturnType<typeof vi.fn>;
const put = apiClient.PUT as unknown as ReturnType<typeof vi.fn>;
const del = apiClient.DELETE as unknown as ReturnType<typeof vi.fn>;

/** A failed estimate: the API's `{ detail }` body and HTTP status. */
interface Failure {
  status: number;
  detail: string;
}

interface Api {
  years?: number[];
  /** Estimate (or failure) by user id. */
  estimates?: Record<number, TaxEstimate | Failure>;
  /** Saved profile by user id; missing = 404. */
  profiles?: Record<number, TaxProfile>;
}

function mockApi({ years = [2024, 2023], estimates = {}, profiles = {} }: Api = {}) {
  get.mockImplementation((path: string, opts?: { params?: { query?: { user_id?: number }; path?: { user_id?: number } } }) => {
    const reply = (data: unknown) => Promise.resolve({ data, error: undefined, response: { status: 200 } });
    switch (path) {
      case "/users":
        return reply(USERS);
      case "/tax/years":
        return reply(years);
      case "/tax/estimate": {
        const found = estimates[opts?.params?.query?.user_id ?? -1];
        if (found && "status" in found) {
          return Promise.resolve({
            data: undefined,
            error: { detail: found.detail },
            response: { status: found.status },
          });
        }
        return reply(found);
      }
      case "/tax/profiles/{user_id}/{tax_year}": {
        const profile = profiles[opts?.params?.path?.user_id ?? -1];
        return profile
          ? reply(profile)
          : Promise.resolve({
              data: undefined,
              error: { detail: "tax profile not found" },
              response: { status: 404 },
            });
      }
      default:
        return reply([]);
    }
  });
}

function renderScreen(entry = "/tax/estimate?user=1&year=2024") {
  return render(<TaxEstimateScreen />, { wrapper: makeWrapper(entry) });
}

beforeEach(() => {
  window.localStorage.clear();
  get.mockReset();
  put.mockReset();
  del.mockReset();
  put.mockResolvedValue({ data: taxProfile(), error: undefined, response: { status: 200 } });
  del.mockResolvedValue({ error: undefined, response: { status: 204 } });
});
afterEach(() => vi.restoreAllMocks());

const line = (key: string) => screen.getByTestId(`estimate-line-${key}`);

describe("TaxEstimateScreen", () => {
  it("exposes its route metadata", () => {
    expect(screenMeta).toEqual({ path: "/tax/estimate", title: "Estimate" });
  });

  it("shows an empty state linking to the tax import when there are no forms", async () => {
    mockApi({ years: [] });
    renderScreen();
    expect(
      await screen.findByText("Import a W-2, 1099 or 1040 to estimate your tax"),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Import tax forms" })).toHaveAttribute("href", "/tax/import");
    expect(screen.queryByTestId("estimate-view")).not.toBeInTheDocument();
  });

  it("renders an amount owed, the income-to-result breakdown and the rates", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    const result = await screen.findByTestId("estimate-result-card");
    expect(result).toHaveTextContent("Estimated result");
    expect(result).toHaveTextContent("Owe $1,769.60");
    expect(within(result).getByTestId("result-amount")).toHaveAttribute("data-kind", "owe");
    expect(screen.getByTestId("estimate-tax-card")).toHaveTextContent("$16,000.00");
    expect(screen.getByTestId("estimate-effective-card")).toHaveTextContent("15.51%");
    expect(screen.getByTestId("estimate-marginal-card")).toHaveTextContent("24.00%");
    expect(screen.getByTestId("estimate-marginal-card")).toHaveTextContent(/long-term gains: 15\.00%/);

    // Gross income -> AGI -> deduction -> taxable -> tax -> payments -> result.
    expect(line("gross")).toHaveTextContent("$103,140.00");
    expect(line("agi")).toHaveTextContent("$103,140.00");
    expect(line("deduction")).toHaveTextContent("Standard deduction");
    expect(line("taxable")).toHaveTextContent("$88,540.00");
    expect(line("ordinary-tax")).toHaveTextContent("$15,730.00");
    expect(line("preferential-tax")).toHaveTextContent("$270.00");
    expect(line("total-tax")).toHaveTextContent("$16,000.00");
    expect(line("withheld")).toHaveTextContent("$14,230.40");
    expect(line("payments")).toHaveTextContent("$14,230.40");
    expect(line("result")).toHaveTextContent("Owe $1,769.60");
    // Zero optional lines are left out.
    expect(screen.queryByTestId("estimate-line-se-tax")).not.toBeInTheDocument();
    expect(screen.queryByTestId("estimate-line-nonrefundable")).not.toBeInTheDocument();
  });

  it("shows the senior deduction, NIIT, Additional Medicare Tax and the credits and payments that go with them", async () => {
    // Refundable credits ($3,000), Additional Medicare Tax withheld ($450) and an excess Social
    // Security credit ($200) are payments; they are not taken off the tax. $25,000 of nonrefundable
    // credits were entered but only the $10,000 income tax can use them. Two seniors on a joint
    // return at an AGI of $103,140 (under the $150,000 threshold): $6,000 each.
    const calc = estimateCalc({
      filing_status: "mfj",
      senior_deduction_minor: 1200000,
      income_tax_minor: 1000000,
      nonrefundable_credits_minor: 1000000,
      nonrefundable_credits_entered_minor: 2500000,
      net_investment_income_tax_minor: 76000,
      additional_medicare_tax_minor: 90000,
      other_taxes_minor: 50000,
      // 0 (income tax less the credits) + 760 + 900 + 500 = 2,160.
      total_tax_minor: 216000,
      refundable_credits_minor: 300000,
      additional_medicare_withheld_minor: 45000,
      excess_social_security_credit_minor: 20000,
      federal_withheld_minor: 100000,
      // 1,000 + 3,000 + 450 + 200 = 4,650.
      total_payments_minor: 465000,
      result_minor: 249000,
      refund_minor: 249000,
      owed_minor: 0,
    });
    mockApi({ estimates: { 1: estimate({ calc }) } });
    renderScreen();
    await screen.findByTestId("estimate-breakdown");
    const tax = within(screen.getByTestId("estimate-section-tax"));
    const payments = within(screen.getByTestId("estimate-section-payments"));
    expect(line("senior-deduction")).toHaveTextContent("Senior deduction");
    expect(line("senior-deduction")).toHaveTextContent("$12,000.00");
    expect(line("nonrefundable")).toHaveTextContent("Nonrefundable credits used");
    expect(line("nonrefundable")).toHaveTextContent("$10,000.00");
    expect(line("nonrefundable-entered")).toHaveTextContent("$25,000.00");
    expect(line("niit")).toHaveTextContent("$760.00");
    expect(line("additional-medicare")).toHaveTextContent("$900.00");
    expect(line("total-tax")).toHaveTextContent("$2,160.00");
    // Refundable credits are payments, not a subtraction from the tax.
    expect(tax.queryByText("Refundable credits")).not.toBeInTheDocument();
    expect(payments.getByText("Refundable credits")).toBeInTheDocument();
    expect(line("refundable")).toHaveTextContent("$3,000.00");
    expect(line("additional-medicare-withheld")).toHaveTextContent("$450.00");
    expect(line("excess-ss")).toHaveTextContent("$200.00");
    expect(line("payments")).toHaveTextContent("$4,650.00");
    expect(line("result")).toHaveTextContent("Refund $2,490.00");
    // The cards show the server's total tax as is (never reduced by the refundable credits).
    expect(screen.getByTestId("estimate-tax-card")).toHaveTextContent("$2,160.00");
  });

  it("leaves out the entered-credits note when the credits were all used", async () => {
    const calc = estimateCalc({ nonrefundable_credits_minor: 200000, nonrefundable_credits_entered_minor: 200000 });
    mockApi({ estimates: { 1: estimate({ calc }) } });
    renderScreen();
    await screen.findByTestId("estimate-breakdown");
    expect(line("nonrefundable")).toHaveTextContent("$2,000.00");
    expect(screen.queryByTestId("estimate-line-nonrefundable-entered")).not.toBeInTheDocument();
  });

  it("renders a refund", async () => {
    mockApi({ estimates: { 1: estimate({ calc: REFUND_CALC }) } });
    renderScreen();
    const result = await screen.findByTestId("estimate-result-card");
    expect(result).toHaveTextContent("Refund $500.00");
    expect(within(result).getByTestId("result-amount")).toHaveAttribute("data-kind", "refund");
    expect(line("result")).toHaveTextContent("Refund $500.00");
  });

  it("says whether each input line came from documents, the profile, or was computed", async () => {
    const calc = estimateCalc({
      profile_other_income_minor: 500000,
      adjustments_minor: 100000,
      nonrefundable_credits_minor: 200000,
      nonrefundable_credits_entered_minor: 200000,
      estimated_payments_minor: 300000,
      self_employment_minor: 2000000,
      se_tax_minor: 282000,
      se_tax_deduction_minor: 141000,
      deduction_mode: "itemized",
      deduction_minor: 2500000,
    });
    mockApi({ estimates: { 1: estimate({ calc }) } });
    renderScreen();
    await screen.findByTestId("estimate-breakdown");
    expect(within(line("wages")).getByText("Documents")).toBeInTheDocument();
    expect(within(line("se")).getByText("Documents")).toBeInTheDocument();
    expect(within(line("other-profile")).getByText("Profile")).toBeInTheDocument();
    expect(within(line("adjustments")).getByText("Profile")).toBeInTheDocument();
    expect(within(line("se-deduction")).getByText("Computed")).toBeInTheDocument();
    expect(within(line("deduction")).getByText("Profile")).toBeInTheDocument();
    expect(line("deduction")).toHaveTextContent("Itemized deductions");
    expect(within(line("nonrefundable")).getByText("Profile")).toBeInTheDocument();
    expect(within(line("se-tax")).getByText("Computed")).toBeInTheDocument();
    expect(within(line("estimated")).getByText("Profile")).toBeInTheDocument();
    expect(within(line("withheld")).getByText("Documents")).toBeInTheDocument();
  });

  it("shows the household calculation and this user's allocation for a joint return", async () => {
    mockApi({
      estimates: { 1: householdEstimate() },
      profiles: { 1: taxProfile({ filing_status: "mfj", joint_with_user_id: 2 }) },
    });
    renderScreen();
    const result = await screen.findByTestId("estimate-result-card");
    expect(result).toHaveTextContent("Your result (allocated)");
    expect(result).toHaveTextContent("Refund $400.00");
    expect(screen.getByTestId("estimate-tax-card")).toHaveTextContent("Household total tax");
    expect(screen.getByTestId("estimate-summary")).toHaveTextContent(/Joint return of Alex and Sam/);
    expect(screen.getByRole("heading", { name: "Household calculation" })).toBeInTheDocument();
    expect(line("result")).toHaveTextContent("Household result");
    expect(line("result")).toHaveTextContent("Refund $1,000.00");

    const alloc = screen.getByTestId("allocation-card");
    expect(alloc).toHaveTextContent("Allocated by share of income");
    expect(alloc).toHaveTextContent("not a separate liability");
    expect(within(alloc).getByTestId("allocation-share")).toHaveTextContent("60%");
    expect(alloc).toHaveTextContent("$9,600.00"); // allocated tax
    expect(within(alloc).getByTestId("allocation-result")).toHaveTextContent("Refund $400.00");
    // Joint filers' form starts with the linked spouse.
    expect(await screen.findByLabelText("Spouse")).toHaveValue("2");
  });

  it("does not show the allocation card for an individual estimate", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    await screen.findByTestId("estimate-view");
    await screen.findByTestId("estimate-breakdown");
    expect(screen.queryByTestId("allocation-card")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Spouse")).not.toBeInTheDocument();
  });

  it("compares with the filed return and shows the differences", async () => {
    mockApi({
      estimates: {
        1: estimate({
          cross_check: {
            return_filing_status: "single",
            return_total_tax_minor: 1550000,
            return_withheld_minor: 1423040,
            return_refund_minor: null,
            return_owed_minor: 126960,
            return_result_minor: -126960,
            delta_total_tax_minor: 50000,
            delta_withheld_minor: 0,
            delta_result_minor: -50000,
            household: false,
          },
        }),
      },
    });
    renderScreen();
    const card = await screen.findByTestId("cross-check-card");
    expect(within(card).getByTestId("cross-check-tax-return")).toHaveTextContent("$15,500.00");
    expect(within(card).getByTestId("cross-check-tax-estimate")).toHaveTextContent("$16,000.00");
    expect(within(card).getByTestId("cross-check-tax-delta")).toHaveTextContent("+$500.00");
    expect(within(card).getByTestId("cross-check-result-return")).toHaveTextContent("Owe $1,269.60");
    expect(within(card).getByTestId("cross-check-result-estimate")).toHaveTextContent("Owe $1,769.60");
    expect(within(card).getByTestId("cross-check-result-delta")).toHaveTextContent("-$500.00");
    expect(within(card).getByTestId("cross-check-withheld-delta")).toHaveTextContent("$0.00");
    expect(card).toHaveTextContent(/never used in the estimate/);
    expect(card).toHaveTextContent(/Filed as single/);
  });

  it("has no cross-check card without a return, and notes a joint return's comparison", async () => {
    mockApi({ estimates: { 1: estimate() } });
    const { unmount } = renderScreen();
    await screen.findByTestId("estimate-breakdown");
    expect(screen.queryByTestId("cross-check-card")).not.toBeInTheDocument();
    unmount();

    mockApi({
      estimates: {
        1: householdEstimate({
          cross_check: {
            return_filing_status: "mfj",
            return_total_tax_minor: 1600000,
            return_withheld_minor: null,
            return_refund_minor: 100000,
            return_owed_minor: null,
            return_result_minor: 100000,
            delta_total_tax_minor: 0,
            delta_withheld_minor: null,
            delta_result_minor: 0,
            household: true,
          },
        }),
      },
    });
    renderScreen();
    const card = await screen.findByTestId("cross-check-card");
    expect(card).toHaveTextContent(/joint return is compared with the household totals/);
    expect(within(card).getByTestId("cross-check-withheld-return")).toHaveTextContent("—");
  });

  it("lists warnings with names, a collapsible not-modeled list and the parameter source", async () => {
    mockApi({
      estimates: {
        1: estimate({
          warnings: ["1 form(s) of user 2 were replaced by a corrected form and are not counted."],
        }),
      },
    });
    renderScreen();
    const warnings = await screen.findByTestId("estimate-warnings");
    expect(warnings).toHaveTextContent("1 form(s) of Sam were replaced");

    const notModeled = screen.getByTestId("estimate-not-modeled");
    expect(notModeled).not.toHaveAttribute("open");
    await userEvent.click(within(notModeled).getByText(/Not modeled \(2\)/));
    expect(notModeled).toHaveAttribute("open");
    expect(notModeled).toHaveTextContent("Alternative minimum tax.");

    const footnote = screen.getByTestId("estimate-parameters");
    expect(footnote).toHaveTextContent("Rev. Proc. 2023-34");
    expect(footnote).toHaveTextContent("version 3");
    expect(footnote).toHaveTextContent("bundled with the app");
    expect(within(footnote).getByRole("link", { name: /Tax parameters/ })).toHaveAttribute("href", "/settings");
  });

  it("explains a year without tax parameters (422) and keeps the inputs available", async () => {
    mockApi({
      years: [2030, 2024],
      estimates: { 1: { status: 422, detail: "No federal tax parameters for 2030 (supported: 2022-2026)." } },
    });
    renderScreen("/tax/estimate?user=1&year=2030");
    const box = await screen.findByTestId("estimate-no-parameters");
    expect(box).toHaveTextContent("No tax parameters for 2030");
    expect(box).toHaveTextContent("supported: 2022-2026");
    expect(within(box).getByRole("link", { name: /Tax parameters/ })).toHaveAttribute("href", "/settings");
    expect(screen.queryByTestId("estimate-cards")).not.toBeInTheDocument();
    expect(await screen.findByTestId("profile-form")).toBeInTheDocument();
  });

  it("explains disagreeing spouse profiles (409) by name and offers the spouse", async () => {
    mockApi({
      estimates: {
        1: {
          status: 409,
          detail: "The profile of user 2 does not match: both spouses must be married filing jointly and name each other.",
        },
      },
      profiles: { 1: taxProfile({ filing_status: "mfj", joint_with_user_id: 2 }) },
    });
    renderScreen();
    const box = await screen.findByTestId("estimate-spouse-conflict");
    expect(box).toHaveTextContent("The spouses' tax profiles for 2024 disagree");
    expect(box).toHaveTextContent("The profile of Sam does not match");
    expect(within(box).getByRole("button", { name: "Open Sam's profile" })).toBeInTheDocument();
    // The profile can be fixed right there.
    expect(await screen.findByLabelText("Filing status")).toHaveValue("mfj");
  });

  it("shows a retryable error for other failures", async () => {
    mockApi({ estimates: { 1: { status: 400, detail: "boom" } } });
    renderScreen();
    expect(await screen.findByText("Could not compute the estimate")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("asks for a person with All users selected, one card per user, and opens one on request", async () => {
    mockApi({
      estimates: {
        1: estimate(),
        2: estimate({ user_id: 2, calc: REFUND_CALC }),
      },
    });
    renderScreen("/tax/estimate?user=all&year=2024");
    const alex = await screen.findByTestId("estimate-person-1");
    const sam = await screen.findByTestId("estimate-person-2");
    expect(screen.getByTestId("estimate-people")).toHaveTextContent(/one person at a time/);
    await waitFor(() => expect(alex).toHaveTextContent("Owe $1,769.60"));
    expect(sam).toHaveTextContent("Refund $500.00");
    expect(screen.queryByTestId("estimate-view")).not.toBeInTheDocument();
    expect(screen.queryByTestId("profile-form")).not.toBeInTheDocument();

    await userEvent.click(within(sam).getByRole("button", { name: "View Sam's estimate" }));
    expect(await screen.findByTestId("estimate-view")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("user=2");
    expect(get).toHaveBeenCalledWith(
      "/tax/estimate",
      expect.objectContaining({ params: { query: { user_id: 2, tax_year: 2024 } } }),
    );
  });

  it("labels a joint filer's card as an allocated share in the all-users view", async () => {
    mockApi({
      estimates: { 1: householdEstimate(), 2: householdEstimate({ user_id: 2 }) },
    });
    renderScreen("/tax/estimate?user=all&year=2024");
    const alex = await screen.findByTestId("estimate-person-1");
    await waitFor(() => expect(alex).toHaveTextContent("Joint: allocated share"));
    expect(alex).toHaveTextContent("Allocated tax");
    expect(alex).toHaveTextContent("Refund $400.00");
  });

  it("switches year and reads it from the URL", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen("/tax/estimate?user=1&year=2023");
    expect(await screen.findByRole("radio", { name: "2023" })).toBeChecked();
    await userEvent.click(screen.getByRole("radio", { name: "2024" }));
    expect(screen.getByTestId("location")).toHaveTextContent("year=2024");
  });
});

describe("profile form", () => {
  it("starts from the estimate's assumptions, with help about payroll deductions", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    const form = await screen.findByTestId("profile-form");
    expect(form).toHaveTextContent(/W-2 box 1 already excludes 401\(k\) and HSA payroll deductions/);
    expect(await screen.findByLabelText("Filing status")).toHaveValue("single");
    expect(screen.getByLabelText("Deduction")).toHaveValue("standard");
    expect(screen.queryByLabelText("Itemized deductions")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Adjustments to income")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Reset" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Delete profile" })).not.toBeInTheDocument();
  });

  it("saves the profile with amounts in cents (PUT replaces the whole profile)", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    await userEvent.selectOptions(await screen.findByLabelText("Filing status"), "mfj");
    await userEvent.selectOptions(screen.getByLabelText("Spouse"), "2");
    await userEvent.selectOptions(screen.getByLabelText("Deduction"), "itemized");
    await userEvent.type(screen.getByLabelText("Itemized deductions"), "$25,000");
    await userEvent.selectOptions(screen.getByLabelText("Age 65 or older / blind boxes checked"), "2");
    await userEvent.selectOptions(screen.getByLabelText(/Aged 65 or older/), "1");
    await userEvent.type(screen.getByLabelText("Adjustments to income"), "1,234.50");
    await userEvent.type(screen.getByLabelText("Other income"), "100");
    await userEvent.type(screen.getByLabelText("Capital loss carryover"), "3000.5");
    await userEvent.type(screen.getByLabelText("Nonrefundable credits"), "2000");
    await userEvent.type(screen.getByLabelText("Refundable credits"), "0.99");
    await userEvent.type(screen.getByLabelText("Other taxes"), "10");
    await userEvent.type(screen.getByLabelText("Estimated tax payments"), "4000");
    await userEvent.type(screen.getByLabelText("Other payments"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledWith("/tax/profiles/{user_id}/{tax_year}", {
      params: { path: { user_id: 1, tax_year: 2024 } },
      body: {
        filing_status: "mfj",
        joint_with_user_id: 2,
        deduction_mode: "itemized",
        itemized_deductions_minor: 2500000,
        age65_or_blind_count: 2,
        age65_count: 1,
        adjustments_minor: 123450,
        other_income_minor: 10000,
        capital_loss_carryover_minor: 300050,
        nonrefundable_credits_minor: 200000,
        refundable_credits_minor: 99,
        other_taxes_minor: 1000,
        estimated_payments_minor: 400000,
        other_payments_minor: 500,
      },
    });
    // The estimate is refetched after a save.
    await waitFor(() =>
      expect(get.mock.calls.filter(([p]) => p === "/tax/estimate").length).toBeGreaterThan(1),
    );
  });

  it("limits the age counts to one person's, for every filing status, and says each spouse enters their own", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    const boxes = await screen.findByLabelText("Age 65 or older / blind boxes checked");
    const seniors = screen.getByLabelText(/Aged 65 or older/);
    const values = (el: HTMLElement) =>
      within(el)
        .getAllByRole("option")
        .map((o) => o.textContent);
    // At most 2 boxes, and "65 or older" at most 1 (within the boxes).
    expect(values(boxes)).toEqual(["0", "1", "2"]);
    expect(values(seniors)).toEqual(["0"]);
    await userEvent.selectOptions(boxes, "2");
    expect(values(seniors)).toEqual(["0", "1"]);
    await userEvent.selectOptions(seniors, "1");

    // The same caps hold jointly: a spouse enters their own boxes on their own profile.
    await userEvent.selectOptions(screen.getByLabelText("Filing status"), "mfj");
    expect(values(boxes)).toEqual(["0", "1", "2"]);
    expect(values(seniors)).toEqual(["0", "1"]);
    expect(screen.getByTestId("profile-form")).toHaveTextContent(/A spouse enters their own on their own profile/);
    expect(boxes).toHaveValue("2");
    expect(seniors).toHaveValue("1");

    // Lowering the boxes brings "65 or older" back within them.
    await userEvent.selectOptions(boxes, "0");
    expect(seniors).toHaveValue("0");
    await userEvent.selectOptions(boxes, "2");
    await userEvent.selectOptions(seniors, "1");
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledWith(
      "/tax/profiles/{user_id}/{tax_year}",
      expect.objectContaining({
        body: expect.objectContaining({ age65_or_blind_count: 2, age65_count: 1 }) as unknown,
      }),
    );
  });

  it("sends no spouse unless the status is married filing jointly", async () => {
    mockApi({
      estimates: { 1: householdEstimate() },
      profiles: { 1: taxProfile({ filing_status: "mfj", joint_with_user_id: 2 }) },
    });
    renderScreen();
    await userEvent.selectOptions(await screen.findByLabelText("Filing status"), "single");
    expect(screen.queryByLabelText("Spouse")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect(put).toHaveBeenCalledWith(
      "/tax/profiles/{user_id}/{tax_year}",
      expect.objectContaining({
        body: expect.objectContaining({ filing_status: "single", joint_with_user_id: null }) as unknown,
      }),
    );
  });

  it("rejects an unreadable amount without calling the server", async () => {
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    await userEvent.type(await screen.findByLabelText("Adjustments to income"), "abc");
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a dollar amount of 0 or more");
    expect(put).not.toHaveBeenCalled();
  });

  it("shows the server's refusal of a save", async () => {
    put.mockResolvedValue({
      data: undefined,
      error: { detail: "the spouse's profile for this year disagrees: both must be married filing jointly and name each other" },
      response: { status: 409 },
    });
    mockApi({ estimates: { 1: estimate() } });
    renderScreen();
    await userEvent.selectOptions(await screen.findByLabelText("Filing status"), "mfj");
    await userEvent.selectOptions(screen.getByLabelText("Spouse"), "2");
    await userEvent.click(screen.getByRole("button", { name: "Save profile" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/spouse's profile for this year disagrees/);
  });

  it("resets to the saved values and deletes the profile after a confirmation", async () => {
    mockApi({
      estimates: { 1: estimate({ filing_status_source: "profile" }) },
      profiles: { 1: taxProfile({ adjustments_minor: 150000, deduction_mode: "itemized", itemized_deductions_minor: 2000000 }) },
    });
    renderScreen();
    const adjustments = await screen.findByLabelText("Adjustments to income");
    expect(adjustments).toHaveValue("1500.00");
    expect(screen.getByLabelText("Itemized deductions")).toHaveValue("20000.00");
    expect(screen.getByRole("button", { name: "Save profile" })).toBeDisabled();

    await userEvent.type(adjustments, "9");
    expect(adjustments).toHaveValue("1500.009");
    await userEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(adjustments).toHaveValue("1500.00");

    await userEvent.click(screen.getByRole("button", { name: "Delete profile" }));
    expect(del).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith("/tax/profiles/{user_id}/{tax_year}", {
        params: { path: { user_id: 1, tax_year: 2024 } },
      }),
    );
  });
});
